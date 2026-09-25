"""
SRIBEESonline — the picture on an order notification.

A push can carry one image. An order has several products, so the server
draws them into one 2:1 strip (the shape Android's expanded notification
shows uncropped): up to four product photos, whole and unstretched, on the
brand's soft background. One product → its photo as-is, nothing drawn.

The strip for a given set of photos is drawn once and cached in Redis by
their URLs, so the six status updates of one order reuse it. Everything here
is best-effort: a photo that will not download is left out, and with fewer
than two left the first photo is used. No image is ever a reason for a
notification not to go.
"""
import asyncio
import hashlib
import io
from typing import List, Optional
from uuid import UUID

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.redis import get_redis_client
from app.core.media import media_url

MAX_PHOTOS = 4
_CANVAS = (1024, 512)
_GAP = 20
_BACKGROUND = (251, 244, 247)  # the app's magenta tint
_FETCH_TIMEOUT = httpx.Timeout(4.0, connect=2.0)
_MAX_BYTES = 8 * 1024 * 1024
_CACHE_TTL = 30 * 24 * 3600


def _absolute(path: Optional[str]) -> Optional[str]:
    """Only a URL FCM (and we) can fetch: absolute http(s)."""
    url = media_url(path)
    if url and url.startswith(("http://", "https://")):
        return url
    return None


async def order_images(db: AsyncSession, order_id: Optional[UUID]) -> List[str]:
    """Absolute photo URLs of an order's products, dearest line first, at most four."""
    if order_id is None:
        return []
    from app.models.order import OrderItem

    rows = (
        await db.execute(
            select(OrderItem.product_image)
            .where(OrderItem.order_id == order_id)
            .order_by(OrderItem.subtotal.desc())
        )
    ).scalars().all()
    urls: List[str] = []
    for path in rows:
        url = _absolute(path)
        if url and url not in urls:
            urls.append(url)
        if len(urls) == MAX_PHOTOS:
            break
    return urls


async def _fetch(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    try:
        response = await client.get(url)
        if response.status_code != 200:
            return None
        if not response.headers.get("content-type", "").startswith("image/"):
            return None
        if len(response.content) > _MAX_BYTES:
            return None
        return response.content
    except Exception:
        return None


def compose(photos: List[bytes]) -> bytes:
    """Lay 2–4 photos side by side, each whole in a rounded white square. JPEG."""
    from PIL import Image, ImageDraw, ImageOps

    width, height = _CANVAS
    n = len(photos)
    tile = min(height - 2 * _GAP, (width - (n + 1) * _GAP) // n)
    row = n * tile + (n - 1) * _GAP
    x = (width - row) // 2
    y = (height - tile) // 2

    canvas = Image.new("RGB", _CANVAS, _BACKGROUND)
    mask = Image.new("L", (tile, tile), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, tile - 1, tile - 1), radius=tile // 10, fill=255)
    inset = tile // 12

    for blob in photos:
        with Image.open(io.BytesIO(blob)) as src:
            src.draft("RGB", (tile * 2, tile * 2))  # cheap JPEG downscale on decode
            image = ImageOps.exif_transpose(src).convert("RGBA")
        # Transparent product cut-outs sit on white, not black.
        flat = Image.new("RGBA", image.size, (255, 255, 255, 255))
        flat.alpha_composite(image)
        card = Image.new("RGB", (tile, tile), (255, 255, 255))
        fitted = ImageOps.contain(flat.convert("RGB"), (tile - 2 * inset, tile - 2 * inset))
        card.paste(fitted, ((tile - fitted.width) // 2, (tile - fitted.height) // 2))
        canvas.paste(card, (x, y), mask)
        x += tile + _GAP

    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=85, optimize=True, progressive=True)
    return out.getvalue()


async def push_image(images: List[str]) -> Optional[str]:
    """The one image URL a push should carry for these product photos."""
    if not images:
        return None
    if len(images) == 1:
        return images[0]

    # A cache key, not a security boundary.
    digest = hashlib.sha1("|".join(images).encode(), usedforsecurity=False).hexdigest()
    key = f"notif:collage:{digest}"
    redis = get_redis_client()
    if redis is not None:
        try:
            cached = await redis.get(key)
            if cached:
                return _absolute(cached) or images[0]
        except Exception:
            pass

    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
            blobs = await asyncio.gather(*(_fetch(client, u) for u in images))
        photos = [b for b in blobs if b]
        if len(photos) < 2:
            return images[0]
        jpeg = await asyncio.to_thread(compose, photos)

        from app.services.storage import get_storage

        path = await asyncio.to_thread(
            get_storage().save, io.BytesIO(jpeg), "order.jpg", "notifications", "image/jpeg"
        )
    except Exception as exc:
        logger.warning(f"Order collage skipped, using the first photo: {exc}")
        return images[0]

    if redis is not None:
        try:
            await redis.set(key, path, ex=_CACHE_TTL)
        except Exception:
            pass
    return _absolute(path) or images[0]
