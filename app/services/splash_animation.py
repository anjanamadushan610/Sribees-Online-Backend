"""
SRIBEESonline - Remote splash animation (Lottie JSON)

A Super Admin uploads a Lottie animation and every installed copy of the
mobile app plays it on its next launch -- no Play Store release.

How it reaches a phone:

  1. The upload is validated here, stored through the StorageProvider, and
     recorded in ``app_settings`` under ``splash_animation`` as a JSON blob
     (path + what the app needs to know about it).
  2. ``GET /app/app-config`` -- which every copy already calls at launch and
     on resume -- carries a ``splash`` object: the URL, a version (the file's
     SHA-256) and the backdrop colour.
  3. The app downloads the file only when the version changes, keeps it on
     disk, and plays it from disk at the next cold start. A splash cannot wait
     on the network, so it never plays an animation it has not already got.

Validation is strict on purpose. The file is played by every customer at the
very first frame of the app; a file that parses here but not on a phone would
fall back to the built-in splash (the app checks too), but it is far better to
tell the admin at upload time than to find out from a support call.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_settings import AppSetting
from app.services.app_settings_service import AppSettingsService

SPLASH_ANIMATION_KEY = "splash_animation"

# Downloaded by every installed copy, often on mobile data. A good splash
# animation is well under 500 KB; 2 MB leaves room without letting a 30 MB
# export with embedded photographs through.
MAX_BYTES = 2 * 1024 * 1024

# Longer than this and the splash stops being a splash. The app also caps how
# long it waits, but a 20-second file is a mistake worth catching at upload.
MAX_DURATION_MS = 10_000

DEFAULT_BACKGROUND = "#D2114E"  # the built-in splash's brand pink

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


class SplashAnimationError(ValueError):
    """An upload the app could not play. ``code`` is for the API response."""

    def __init__(self, message: str, code: str = "INVALID_LOTTIE"):
        super().__init__(message)
        self.code = code


def normalise_hex_color(value: Optional[str]) -> str:
    """``d2114e`` / ``#D2114E`` -> ``#D2114E``. Anything else is rejected."""
    if value is None or not value.strip():
        return DEFAULT_BACKGROUND
    match = _HEX.match(value.strip())
    if not match:
        raise SplashAnimationError(
            "Background colour must be a hex colour like #D2114E.",
            code="INVALID_COLOR",
        )
    return f"#{match.group(1).upper()}"


def _number(doc: dict, key: str) -> float:
    value = doc.get(key)
    # bool is an int in Python; a Lottie with "fr": true is not a frame rate.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SplashAnimationError(
            f"This is not a Lottie animation: '{key}' is missing or not a number."
        )
    return float(value)


def inspect_lottie(raw: bytes) -> dict[str, Any]:
    """
    Check that ``raw`` is a Lottie animation a phone can play from a single
    file, and describe it. Raises :class:`SplashAnimationError` otherwise.
    """
    if not raw:
        raise SplashAnimationError("The file is empty.", code="EMPTY_FILE")
    if len(raw) > MAX_BYTES:
        raise SplashAnimationError(
            f"File is {len(raw) / 1024 / 1024:.1f} MB; the limit is "
            f"{MAX_BYTES // 1024 // 1024} MB. Export without embedded photos, "
            "or simplify the animation.",
            code="FILE_TOO_LARGE",
        )

    # A .lottie (dotLottie) file is a zip archive, not JSON.
    if raw[:2] == b"PK":
        raise SplashAnimationError(
            "This is a .lottie (zip) file. Export or download the animation as "
            "Lottie JSON (.json) instead.",
            code="DOTLOTTIE_NOT_SUPPORTED",
        )

    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise SplashAnimationError("The file is not valid JSON.", code="INVALID_JSON")

    if not isinstance(doc, dict):
        raise SplashAnimationError("This is not a Lottie animation.")

    frame_rate = _number(doc, "fr")
    in_point = _number(doc, "ip")
    out_point = _number(doc, "op")
    width = _number(doc, "w")
    height = _number(doc, "h")

    if frame_rate <= 0 or width <= 0 or height <= 0 or out_point <= in_point:
        raise SplashAnimationError(
            "This Lottie has no playable frames (check its size, frame rate "
            "and in/out points)."
        )

    layers = doc.get("layers")
    if not isinstance(layers, list) or not layers:
        raise SplashAnimationError("This Lottie has no layers, so nothing would be shown.")

    duration_ms = round((out_point - in_point) / frame_rate * 1000)
    if duration_ms > MAX_DURATION_MS:
        raise SplashAnimationError(
            f"The animation runs {duration_ms / 1000:.1f}s; a splash may run at "
            f"most {MAX_DURATION_MS // 1000}s.",
            code="TOO_LONG",
        )

    # Images must travel inside the file. A phone downloads exactly one file,
    # so an animation pointing at "images/img_0.png" would render with holes.
    for asset in doc.get("assets") or []:
        if not isinstance(asset, dict) or "p" not in asset:
            continue  # precomps and the like carry no file
        path = str(asset.get("p") or "")
        embedded = asset.get("e") == 1 or path.startswith("data:")
        if path and not embedded:
            raise SplashAnimationError(
                f"The animation refers to an external image ('{path}'). Export "
                "it with images embedded (in After Effects / Bodymovin: "
                "'Include in json').",
                code="EXTERNAL_ASSETS",
            )

    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "width": int(width),
        "height": int(height),
        "frame_rate": frame_rate,
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# Persistence -- one app_settings row holding a JSON description
# ---------------------------------------------------------------------------


def _decode(setting: Optional[AppSetting]) -> Optional[dict[str, Any]]:
    if setting is None or not setting.value:
        return None
    try:
        value = json.loads(setting.value)
    except ValueError:
        logger.warning("splash_animation setting is not valid JSON - ignoring it")
        return None
    if not isinstance(value, dict) or not value.get("path"):
        return None
    return value


async def get_splash_animation(db: AsyncSession) -> tuple[Optional[dict], Optional[AppSetting]]:
    """The stored description (or None) and the row it came from."""
    setting = await AppSettingsService.get_by_key(db, SPLASH_ANIMATION_KEY)
    return _decode(setting), setting


async def save_splash_animation(
    db: AsyncSession, value: dict[str, Any], *, is_active: bool
) -> AppSetting:
    return await AppSettingsService.upsert(
        db,
        key=SPLASH_ANIMATION_KEY,
        value=json.dumps(value),
        description="Lottie animation the mobile app plays as its splash screen",
        is_active=is_active,
    )


def build_value(
    path: str, meta: dict[str, Any], *, filename: Optional[str], background_color: str
) -> dict[str, Any]:
    return {
        "path": path,
        **meta,
        "background_color": background_color,
        "filename": filename,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }


def public_splash(value: Optional[dict], is_active: bool) -> Optional[dict[str, Any]]:
    """
    What ``/app/app-config`` tells the phone. ``path`` stays relative here and
    is resolved per request, so the cached payload never holds one client's
    URL (the emulator rewrite) for another.
    """
    if not value or not is_active:
        return None
    return {
        "path": value["path"],
        "version": value.get("sha256"),
        "background_color": value.get("background_color") or DEFAULT_BACKGROUND,
        "duration_ms": value.get("duration_ms"),
    }
