"""
Media URL resolution.

The database stores an origin-free relative path (``/uploads/categories/x.jpg``).
This module turns it into an absolute URL at *serialization* time, by prepending
``MEDIA_BASE_URL``.

Keeping the origin out of the stored value is what makes the storage migration
free: move to S3 or R2, point ``MEDIA_BASE_URL`` at the new host, and every
existing row resolves to the new location with no rewrite and no migration
script. Baking ``https://api.sribees.com`` into the rows — which is what the old
S3 service did — would have made the same move a data migration over every
product image, category and setting.
"""
from typing import Optional

from app.config.settings import settings


def media_url(path: Optional[str]) -> Optional[str]:
    """
    Resolve a stored media path to the URL a client should fetch.

    * ``None`` / empty  → ``None``
    * already absolute  → returned untouched. This covers rows written by the
      old S3 service (which stored full URLs) and any genuinely external asset,
      so the refactor needs no backfill to keep working.
    * relative path     → ``MEDIA_BASE_URL`` + path.

    With ``MEDIA_BASE_URL`` unset the path is returned as-is: a same-origin
    relative URL, which is exactly right for the admin dashboard talking to its
    own API, and is why it is safe to leave unset in development.
    """
    if not path:
        return None

    cleaned = path.strip()
    if not cleaned:
        return None

    if cleaned.startswith(("http://", "https://", "//")):
        return cleaned

    base = (settings.media_base_url or "").rstrip("/")
    if not base:
        return f"/{cleaned.lstrip('/')}"

    return f"{base}/{cleaned.lstrip('/')}"


def media_url_for_client(
    path: Optional[str],
    client_platform: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve a media path for a specific client.

    Same as :func:`media_url`, with one carve-out for local development: the
    Android emulator cannot reach the host's ``localhost`` (it has its own
    loopback) and must use ``10.0.2.2``. When a client sends
    ``X-Client-Platform: android-emulator`` and ``S3_EMULATOR_URL_PREFIX`` is
    configured, the base origin is swapped for it.

    In production that setting is unset, so this is exactly :func:`media_url`.
    """
    resolved = media_url(path)
    if resolved is None:
        return None

    emulator_prefix = settings.s3_emulator_url_prefix
    if not emulator_prefix:
        return resolved

    is_emulator = (
        client_platform
        and client_platform.lower() in ("android-emulator", "android_emulator")
    )
    if not is_emulator:
        return resolved

    base = (settings.media_base_url or "").rstrip("/")
    if base and resolved.startswith(base):
        return resolved.replace(base, emulator_prefix.rstrip("/"), 1)

    # Same-origin relative URL (no MEDIA_BASE_URL): give the emulator an origin
    # it can actually reach.
    if resolved.startswith("/"):
        return f"{emulator_prefix.rstrip('/')}{resolved}"

    return resolved


def media_path(url: Optional[str]) -> Optional[str]:
    """
    The inverse of :func:`media_url` — normalize a client-supplied value to the
    relative path that gets stored.

    The upload endpoints hand the admin a resolved absolute URL (so it can
    preview the file), and the admin posts that same value back when saving the
    category or product. Storing it verbatim would put the origin straight back
    into the database and undo the whole point of this refactor, so any URL
    pointing at *our* media is reduced to its path on the way in.

    A URL on some other host is left alone: that is a genuinely external asset
    (or a legacy S3 row being re-saved), and rewriting it would break it.
    """
    if not url:
        return None

    cleaned = url.strip()
    if not cleaned:
        return None

    base = (settings.media_base_url or "").rstrip("/")
    if base and cleaned.startswith(base):
        cleaned = cleaned[len(base):] or "/"

    prefix = f"/{settings.media_url_prefix.strip('/')}/"
    if cleaned.startswith(prefix):
        return cleaned

    # Not ours — an external URL, or a legacy absolute S3 URL. Keep it intact;
    # media_url() passes absolute values through untouched.
    return cleaned
