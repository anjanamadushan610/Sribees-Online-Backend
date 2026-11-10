"""
Festival offer product pictures.

  GET /offer/{code}/{variant}/{media path}

The product's stored catalogue picture, drawn on the offer design
(``offer_imagery``). ``variant`` is ``c`` (with the cashback ribbon) or ``n``
(without, for a 0% product). Drawn once and kept on disk, keyed by the path,
the variant and the design version, like the share cards.

Only an uploaded product picture is drawn -- the path must sit under the media
``products`` folder -- and only for a known offer. After the offer has ended,
or for anything we cannot draw, the caller is sent to the plain picture.
"""
import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse, Response
from loguru import logger

from app.config.settings import settings
from app.core.media import media_url
from app.services import festival_offer, offer_imagery
from app.services.storage import get_storage

router = APIRouter(include_in_schema=False)

# Long: the URL itself changes with the photo and the design.
_CACHE_SECONDS = 86400
_render_slots = asyncio.Semaphore(2)


def _design(variant: str) -> offer_imagery.OfferDesign:
    if variant == "n":
        return offer_imagery.OfferDesign(ribbon="")
    return offer_imagery.DEFAULT_DESIGN


def _key(code: str, variant: str, path: str) -> str:
    seed = f"{code}|{variant}|{path}|{offer_imagery.OFFER_TEMPLATE_VERSION}"
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


def _cache_dir() -> Path:
    return Path(settings.media_root) / "offer-cards"


def _valid_path(path: str) -> bool:
    prefix = f"/{settings.media_url_prefix.strip('/')}/products/"
    return path.startswith(prefix) and ".." not in path and "\\" not in path


async def offer_picture_bytes(code: str, variant: str, path: str) -> tuple[bytes | None, Path]:
    """Draw (or read from cache) one offer picture. Used by the route and the warm-up."""
    cached = _cache_dir() / f"{_key(code, variant, path)}.webp"
    if cached.is_file():
        return None, cached
    source = await run_in_threadpool(get_storage().read, path)
    if source is None:
        raise FileNotFoundError(path)
    async with _render_slots:
        if cached.is_file():
            return None, cached
        picture = await run_in_threadpool(offer_imagery.render_offer_picture, source, _design(variant))
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        partial = cached.with_suffix(f".{uuid4().hex}.part")
        partial.write_bytes(picture)
        partial.replace(cached)
    except OSError as exc:
        logger.warning(f"Offer picture cache write failed: {exc}")
    return picture, cached


@router.get("/offer/{code}/{variant}/{path:path}")
async def offer_picture(code: str, variant: str, path: str):
    path = "/" + path.lstrip("/")
    plain = media_url(path) if _valid_path(path) else None
    offer = festival_offer.offer_by_code(code)
    if variant not in ("c", "n") or not _valid_path(path):
        return Response(status_code=404)
    if offer is None or festival_offer.active_offer() != offer:
        # Ended (or switched off): the shopper still gets the product.
        return RedirectResponse(plain, status_code=302)

    headers = {"Cache-Control": f"public, max-age={_CACHE_SECONDS}"}
    try:
        picture, cached = await offer_picture_bytes(code, variant, path)
    except FileNotFoundError:
        return RedirectResponse(plain, status_code=302)
    except offer_imagery.ImageryError as exc:
        logger.warning(f"Offer picture for {path!r} not drawn: {exc}")
        return RedirectResponse(plain, status_code=302)
    if picture is None:
        return FileResponse(cached, media_type="image/webp", headers=headers)
    return Response(picture, media_type="image/webp", headers=headers)
