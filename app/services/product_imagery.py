"""
Product imagery: one transparent cutout in, every picture the shop needs out.

An admin or branch manager uploads only the product itself -- a PNG (or WebP)
whose background is transparent. Everything else is done here, so a bottle, a
cracker pack and a rice bag all come out looking like one catalogue:

  * ``normalize_cutout`` -- the catalogue picture the app and the web show: the
    product trimmed to its own outline and sized by its shape on a square white
    canvas. A wide pack fills the width, a tall bottle fills the height, a box
    is held back so it does not look twice as big as everything around it.
  * ``render_share_card`` -- the picture that travels on WhatsApp, Facebook and
    Telegram: the same product above the brand swoosh and logo.

Nothing here touches the database or storage; callers hand in bytes and get
bytes back, which keeps the rules testable without a server.
"""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, UnidentifiedImageError

# Bump when the share card's look changes: it is part of every cached card's
# key, so old cards stop being served the moment a new design ships.
SHARE_TEMPLATE_VERSION = "1"

CATALOG_SIZE = 1200
SHARE_SIZE = 1200

# A cutout is upscaled at most this far; anything smaller than this on its long
# side would be visibly soft in the product page hero.
MIN_CUTOUT_LONG_SIDE = 500
# Pillow refuses to open larger images at all -- a decompression bomb guard
# well below its own default, since no product photo needs 40 megapixels.
MAX_PIXELS = 40_000_000

# Share of the canvas a product's outline aims to cover. A plain "fit the box"
# makes a square tin look far heavier than a long cracker pack; aiming for the
# same area and then capping each side keeps them visually even.
_TARGET_AREA = 0.45
_MAX_SIDE = 0.88

_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "share"
_SWOOSH = (222, 10, 71)

# The swoosh's upper edge, left tip to right edge, as fractions of the card.
_SWOOSH_EDGE = (
    (0.343, 0.973), (0.399, 0.954), (0.447, 0.927), (0.494, 0.898),
    (0.542, 0.875), (0.590, 0.860), (0.638, 0.852), (0.734, 0.841),
    (0.829, 0.832), (0.925, 0.816), (1.000, 0.797),
)
_SWOOSH_BOTTOM = 0.973
# Where the product may sit on a share card: clear of the swoosh below it.
_SHARE_BOX = (0.06, 0.06, 0.94, 0.80)


class ImageryError(ValueError):
    """The upload cannot be turned into a catalogue picture; the message says why."""


def _open(data: bytes) -> Image.Image:
    try:
        im = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise ImageryError("That image is too large. Please keep it under 40 megapixels.") from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageryError("That file is not an image we can read.") from exc
    if im.width * im.height > MAX_PIXELS:
        raise ImageryError("That image is too large. Please keep it under 40 megapixels.")
    try:
        im.load()
    except OSError as exc:
        raise ImageryError("That image file is damaged.") from exc
    return im


def _alpha(im: Image.Image) -> Optional[Image.Image]:
    """The image's alpha channel, or None if nothing in it is see-through."""
    if im.mode == "P" and "transparency" in im.info:
        im = im.convert("RGBA")
    if im.mode not in ("RGBA", "LA", "RGBa", "La"):
        return None
    alpha = im.getchannel("A")
    low, _ = alpha.getextrema()
    return alpha if low < 250 else None


def _trim_cutout(im: Image.Image) -> Image.Image:
    rgba = im.convert("RGBA")
    # Ignore near-invisible fringe pixels, or a stray haze sets the outline.
    box = rgba.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    if box is None:
        raise ImageryError("That PNG is completely transparent.")
    return rgba.crop(box)


def _trim_on_white(im: Image.Image) -> Image.Image:
    """Crop a photo on a white background down to what is not white."""
    rgb = im.convert("RGB")
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, (255, 255, 255)))
    box = diff.convert("L").point(lambda v: 255 if v > 18 else 0).getbbox()
    return rgb.crop(box) if box else rgb


def _fit(size: tuple[int, int], box_w: float, box_h: float, canvas: int) -> tuple[int, int]:
    """Scale an outline by its shape: equal visual weight, never past the box."""
    w, h = size
    scale = min(
        ((_TARGET_AREA * canvas * canvas) / (w * h)) ** 0.5,
        box_w / w,
        box_h / h,
    )
    return max(1, round(w * scale)), max(1, round(h * scale))


def _resize(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    if im.mode == "RGBA":
        # Resample premultiplied, or the transparent pixels' colour bleeds
        # into the product's edge as a dark or light halo.
        return im.convert("RGBa").resize(size, Image.Resampling.LANCZOS).convert("RGBA")
    return im.resize(size, Image.Resampling.LANCZOS)


def normalize_cutout(data: bytes) -> Optional[bytes]:
    """
    Turn a transparent cutout into the catalogue picture: WebP, square, white.

    Returns None for an image with nothing transparent in it -- an ordinary
    photo, which the caller stores as uploaded. Raises ``ImageryError`` for a
    file that is not a readable image, or a cutout too small to use.
    """
    im = _open(data)
    if _alpha(im) is None:
        return None
    product = _trim_cutout(im)
    if max(product.size) < MIN_CUTOUT_LONG_SIDE:
        raise ImageryError(
            f"The product in that PNG is only {max(product.size)} px across. "
            f"Please upload one at least {MIN_CUTOUT_LONG_SIDE} px on its long side."
        )

    edge = _MAX_SIDE * CATALOG_SIZE
    product = _resize(product, _fit(product.size, edge, edge, CATALOG_SIZE))
    canvas = Image.new("RGBA", (CATALOG_SIZE, CATALOG_SIZE), (255, 255, 255, 255))
    canvas.alpha_composite(
        product,
        ((CATALOG_SIZE - product.width) // 2, (CATALOG_SIZE - product.height) // 2),
    )
    out = io.BytesIO()
    canvas.convert("RGB").save(out, "WEBP", quality=90, method=6)
    return out.getvalue()


def _catmull_rom(points, steps: int = 24):
    pts = [points[0], *points, points[-1]]
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        for s in range(steps):
            t = s / steps
            t2, t3 = t * t, t * t * t
            yield tuple(
                0.5 * (2 * p1[k] + (-p0[k] + p2[k]) * t
                       + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                       + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3)
                for k in (0, 1)
            )
    yield points[-1]


@lru_cache(maxsize=2)
def _share_frame(size: int) -> Image.Image:
    """White card with the swoosh and logo; the product goes on top of the white."""
    ss = 4  # drawn large and shrunk, so the curve's edge is smooth
    big = size * ss
    mask = Image.new("L", (big, big), 0)
    edge = [(x * big, y * big) for x, y in _catmull_rom(_SWOOSH_EDGE)]
    ImageDraw.Draw(mask).polygon(
        [*edge, (big, _SWOOSH_BOTTOM * big), (_SWOOSH_EDGE[0][0] * big, _SWOOSH_BOTTOM * big)],
        fill=255,
    )
    mask = mask.resize((size, size), Image.Resampling.LANCZOS)

    frame = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    frame.paste(Image.new("RGBA", (size, size), (*_SWOOSH, 255)), (0, 0), mask)

    bag = Image.open(_ASSETS / "bag.png").convert("RGBA")
    bag_h = round(0.082 * size)
    bag = _resize(bag, (round(bag.width * bag_h / bag.height), bag_h))
    frame.alpha_composite(bag, (round(0.582 * size), round(0.871 * size)))

    mark = Image.open(_ASSETS / "wordmark.png").convert("RGBA")
    mark_w = round(0.306 * size)
    mark = _resize(mark, (mark_w, round(mark.height * mark_w / mark.width)))
    frame.alpha_composite(mark, (round(0.668 * size), round(0.919 * size - mark.height / 2)))
    return frame


def render_share_card(data: bytes) -> bytes:
    """
    The picture shared to social apps: the product over the brand swoosh. JPEG,
    because every link-preview crawler reads it and it stays small.

    Takes any product picture: a cutout keeps its outline; a photo on white
    (the catalogue picture, or an older upload) is trimmed to what is not white.
    """
    im = _open(data)
    product = _trim_cutout(im) if _alpha(im) is not None else _trim_on_white(im)

    x0, y0, x1, y1 = (v * SHARE_SIZE for v in _SHARE_BOX)
    product = _resize(product, _fit(product.size, x1 - x0, y1 - y0, SHARE_SIZE))

    card = _share_frame(SHARE_SIZE).copy()
    left = round((SHARE_SIZE - product.width) / 2)
    # Sit just above the middle like the design, but never into the swoosh.
    top = round(min(max(0.47 * SHARE_SIZE - product.height / 2, y0), y1 - product.height))
    if product.mode == "RGBA":
        card.alpha_composite(product, (left, top))
    else:
        card.paste(product, (left, top))

    out = io.BytesIO()
    card.convert("RGB").save(out, "JPEG", quality=86, optimize=True, progressive=True)
    return out.getvalue()
