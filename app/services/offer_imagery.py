"""
Festival offer pictures: the product's own catalogue photo, redrawn on an
offer background (11.11 and the like) while the offer is live.

Nothing stored is changed. The catalogue photo stays the white-square WebP the
upload made; while an offer is running the API hands the shopper a URL that
draws this picture from it instead (see ``app.api.offer_images``), and when the
offer ends the plain URL comes back on its own.

The catalogue photo is the product on pure white (``normalize_cutout``), so the
product is lifted off the white it sits on: the white joined to the border is
background, everything else is product. A photo that is not on white (an older
upload) is placed whole in a rounded tile instead.

Like ``product_imagery``, nothing here touches the database or storage.
"""
from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from app.services.product_imagery import ImageryError, _fit, _open, _resize

# Bump on any change to the look: it is part of every cached picture's key.
OFFER_TEMPLATE_VERSION = "1"

SIZE = 1200

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_FONT_HEAVY = _ASSETS / "offer" / "Poppins-ExtraBold.ttf"
_FONT_SEMI = _ASSETS / "offer" / "Poppins-SemiBold.ttf"

# Palette: the brand crimson deepening to plum, with gold for the offer.
_CRIMSON = (222, 10, 71)
_PLUM = (78, 6, 58)
_GOLD_LIGHT = (255, 226, 122)
_GOLD = (255, 193, 59)
_GOLD_DEEP = (214, 140, 18)
_WHITE = (255, 255, 255)
_INK = (120, 0, 36)

# The spotlight disc the product stands on.
_DISC_C = (0.5, 0.535)
_DISC_R = 0.375
# Where the product may sit, as fractions of the canvas (centred on the disc).
_PRODUCT_BOX = 0.60


@dataclass(frozen=True)
class OfferDesign:
    """The words on the picture. English only (owner, 2026-11-10)."""
    headline: str = "11.11"
    tag: str = "SALE"
    ribbon: str = "11% CASHBACK"


DEFAULT_DESIGN = OfferDesign()


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def _linear_gradient(size: tuple[int, int], top, bottom) -> Image.Image:
    """Vertical gradient, top colour to bottom colour."""
    w, h = size
    strip = Image.new("RGB", (1, h))
    for y in range(h):
        strip.putpixel((0, y), _lerp(top, bottom, y / max(1, h - 1)))
    return strip.resize((w, h))


def _radial(size: int, center, radius: float, inner, outer) -> Image.Image:
    """Radial gradient drawn small and scaled up (smooth, and fast enough)."""
    small = 300
    k = small / size
    im = Image.new("RGB", (small, small))
    cx, cy = center[0] * small, center[1] * small
    r = radius * k
    px = im.load()
    for y in range(small):
        for x in range(small):
            t = min(1.0, math.hypot(x - cx, y - cy) / r)
            px[x, y] = _lerp(inner, outer, t * t)
    return im.resize((size, size), Image.Resampling.BICUBIC)


def _text_mask(text: str, font: ImageFont.FreeTypeFont, tracking: int = 0) -> Image.Image:
    """The text as an L mask cropped to its ink, with optional letter spacing."""
    if not tracking:
        left, t, r, b = font.getbbox(text)
        mask = Image.new("L", (r - left + 4, b - t + 4), 0)
        ImageDraw.Draw(mask).text((2 - left, 2 - t), text, font=font, fill=255)
        return mask
    widths = [font.getlength(ch) for ch in text]
    total = round(sum(widths) + tracking * (len(text) - 1)) + 8
    _, t, _, b = font.getbbox(text)
    mask = Image.new("L", (total, b - t + 4), 0)
    draw = ImageDraw.Draw(mask)
    x = 4.0
    for ch, w in zip(text, widths, strict=True):
        draw.text((x, 2 - t), ch, font=font, fill=255)
        x += w + tracking
    return mask.crop(mask.getbbox() or (0, 0, total, b - t + 4))


def _paint(canvas: Image.Image, mask: Image.Image, xy, fill) -> None:
    """Fill *mask* at *xy* with a colour or an image of the mask's size."""
    layer = fill if isinstance(fill, Image.Image) else Image.new("RGBA", mask.size, (*fill, 255))
    canvas.paste(layer.convert("RGBA"), xy, mask)


def _shadow(canvas: Image.Image, mask: Image.Image, xy, offset, blur, color, alpha) -> None:
    pad = blur * 3
    big = Image.new("L", (mask.width + 2 * pad, mask.height + 2 * pad), 0)
    big.paste(mask, (pad, pad))
    big = big.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: v * alpha // 255)
    _paint(canvas, big, (xy[0] - pad + offset[0], xy[1] - pad + offset[1]), color)


def _confetti(canvas: Image.Image, keep_out) -> None:
    """Gold, white and pink confetti scattered around the edges, never over
    the product or the words."""
    rng = random.Random(1111)  # fixed: every product gets the same scatter
    draw = ImageDraw.Draw(canvas, "RGBA")
    colors = [(*_GOLD, 235), (*_GOLD_LIGHT, 220), (255, 255, 255, 210), (255, 128, 171, 215)]
    placed = 0
    tries = 0
    while placed < 70 and tries < 3000:
        tries += 1
        x, y = rng.uniform(0, SIZE), rng.uniform(0, SIZE)
        if any(f(x, y) for f in keep_out):
            continue
        color = rng.choice(colors)
        kind = rng.random()
        if kind < 0.45:  # streamer piece: a small rotated rectangle
            w, h = rng.uniform(10, 22), rng.uniform(4, 8)
            a = rng.uniform(0, math.pi)
            pts = [
                (x + dx * math.cos(a) - dy * math.sin(a), y + dx * math.sin(a) + dy * math.cos(a))
                for dx, dy in ((-w, -h), (w, -h), (w, h), (-w, h))
            ]
            draw.polygon(pts, fill=color)
        elif kind < 0.8:  # dot
            r = rng.uniform(4, 9)
            draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
        else:  # four-point sparkle
            r = rng.uniform(10, 18)
            q = r * 0.28
            draw.polygon(
                [(x, y - r), (x + q, y - q), (x + r, y), (x + q, y + q),
                 (x, y + r), (x - q, y + q), (x - r, y), (x - q, y - q)],
                fill=color,
            )
        placed += 1


@lru_cache(maxsize=4)
def _frame(design: OfferDesign) -> Image.Image:
    """Everything except the product: background, disc, words, ribbon."""
    S = SIZE
    canvas = Image.new("RGBA", (S, S))

    # Background: crimson to plum, lit from behind the disc.
    bg = _linear_gradient((S, S), _CRIMSON, _PLUM)
    glow = _radial(S, _DISC_C, 0.62, (255, 92, 120), (0, 0, 0))
    bg = ImageChops.add(bg, glow.point(lambda v: v * 55 // 255))
    canvas.paste(bg, (0, 0))

    cx, cy = _DISC_C[0] * S, _DISC_C[1] * S
    r = _DISC_R * S

    # Soft light rays fanning out from the disc.
    rays = Image.new("L", (S, S), 0)
    rdraw = ImageDraw.Draw(rays)
    n = 18
    for i in range(n):
        a0 = (i / n) * 2 * math.pi
        a1 = a0 + math.pi / n
        far = S * 1.2
        rdraw.polygon(
            [(cx, cy), (cx + far * math.cos(a0), cy + far * math.sin(a0)),
             (cx + far * math.cos(a1), cy + far * math.sin(a1))],
            fill=22,
        )
    _paint(canvas, rays, (0, 0), _WHITE)

    # Disc: a soft shadow, a gold ring, then the white spotlight.
    ss = 4
    def circle_mask(radius):
        big = Image.new("L", (S * ss, S * ss), 0)
        ImageDraw.Draw(big).ellipse(
            ((cx - radius) * ss, (cy - radius) * ss, (cx + radius) * ss, (cy + radius) * ss), fill=255
        )
        return big.resize((S, S), Image.Resampling.LANCZOS)

    shadow = circle_mask(r + 16).filter(ImageFilter.GaussianBlur(26)).point(lambda v: v * 120 // 255)
    _paint(canvas, shadow, (0, 14), (40, 0, 20))
    ring = _radial(S, (0.42, 0.40), 0.75, _GOLD_LIGHT, _GOLD_DEEP)
    _paint(canvas, circle_mask(r + 16), (0, 0), ring)
    disc = _radial(S, _DISC_C, _DISC_R, (255, 255, 255), (255, 244, 226))
    _paint(canvas, circle_mask(r), (0, 0), disc)

    # Headline: "11.11" in gold with "SALE" beside it.
    head_font = _font(_FONT_HEAVY, 168)
    tag_font = _font(_FONT_HEAVY, 92)
    head = _text_mask(design.headline, head_font)
    tag = _text_mask(design.tag, tag_font, tracking=6)
    gap = 26
    total_w = head.width + gap + tag.width
    hx = round((S - total_w) / 2)
    hy = round(0.028 * S)
    gold = _linear_gradient(head.size, _GOLD_LIGHT, _GOLD_DEEP)
    _shadow(canvas, head, (hx, hy), (0, 8), 10, (50, 0, 25), 170)
    _paint(canvas, head, (hx, hy), gold)
    tx = hx + head.width + gap
    ty = hy + head.height - tag.height  # sit on the headline's baseline
    _shadow(canvas, tag, (tx, ty), (0, 6), 8, (50, 0, 25), 150)
    _paint(canvas, tag, (tx, ty), _WHITE)

    # Ribbon across the foot of the disc: "11% CASHBACK". A 0% product gets
    # no ribbon (an empty ``design.ribbon``): it must not promise cashback.
    rib_box = (0, 0, -1, -1)
    if design.ribbon:
        rib_font = _font(_FONT_HEAVY, 88)
        words = _text_mask(design.ribbon, rib_font, tracking=2)
        rw = words.width + 120
        rh = 132
        rx0 = round((S - rw) / 2)
        ry0 = round(0.815 * S)
        rdraw = ImageDraw.Draw(canvas)
        tail = 70
        fold = 22
        # Back tails with a notch, darker, tucked behind the band.
        for side in (-1, 1):
            x_in = rx0 + (40 if side < 0 else rw - 40)
            x_out = x_in + side * (tail + 40)
            y0, y1 = ry0 + fold, ry0 + rh + fold
            rdraw.polygon(
                [(x_in, y0), (x_out, y0), (x_out - side * 34, (y0 + y1) / 2), (x_out, y1), (x_in, y1)],
                fill=_GOLD_DEEP,
            )
        band_shadow = Image.new("L", (rw, rh), 255)
        _shadow(canvas, band_shadow, (rx0, ry0), (0, 10), 14, (40, 0, 20), 140)
        band = _linear_gradient((rw, rh), _GOLD_LIGHT, _GOLD)
        canvas.paste(band, (rx0, ry0))
        # Thin highlight and edge lines give the band some depth.
        rdraw.rectangle((rx0, ry0, rx0 + rw, ry0 + 5), fill=(255, 240, 180))
        rdraw.rectangle((rx0, ry0 + rh - 6, rx0 + rw, ry0 + rh), fill=_GOLD_DEEP)
        _paint(canvas, words, (rx0 + (rw - words.width) // 2, ry0 + (rh - words.height) // 2), _INK)
        rib_box = (rx0 - tail - 60, ry0 - 20, rx0 + rw + tail + 60, ry0 + rh + fold + 30)

    head_box = (hx - 30, hy - 10, hx + total_w + 30, hy + head.height + 30)

    def inside(box):
        return lambda x, y: box[0] <= x <= box[2] and box[1] <= y <= box[3]

    _confetti(
        canvas,
        [
            lambda x, y: math.hypot(x - cx, y - cy) < r + 50,
            inside(head_box),
            inside(rib_box),
        ],
    )
    return canvas


def _lift_from_white(im: Image.Image) -> Image.Image | None:
    """
    The product off the white it was laid on, as RGBA.

    Background is the near-white joined to the picture's border; white inside
    the product (a label, a bottle cap) is not touched. Returns None for a
    picture whose border is not white -- an ordinary photo.
    """
    rgb = im.convert("RGB")
    w, h = rgb.size
    near_white = rgb.convert("L").point(lambda v: 255 if v >= 244 else 0)
    # Also require low colour: a pale yellow is not background.
    spread = ImageChops.subtract(
        ImageChops.lighter(ImageChops.lighter(*rgb.split()[:2]), rgb.split()[2]),
        ImageChops.darker(ImageChops.darker(*rgb.split()[:2]), rgb.split()[2]),
    ).point(lambda v: 255 if v <= 10 else 0)
    candidate = ImageChops.multiply(near_white, spread)

    edges = [(0, 0, w, 1), (0, h - 1, w, h), (0, 0, 1, h), (w - 1, 0, w, h)]
    white = sum(candidate.crop(box).histogram()[255] for box in edges)
    if white < 0.97 * (2 * w + 2 * h):
        return None

    # Flood the white from every white border pixel (one fill reaches the
    # rest, as the border is one connected ring).
    fill = candidate.copy()
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    for seed in seeds:
        if fill.getpixel(seed) == 255:
            ImageDraw.floodfill(fill, seed, 128, thresh=0)
    background = fill.point(lambda v: 255 if v == 128 else 0)
    # Grow the background by a pixel and feather it, so the anti-aliased
    # white fringe goes with it rather than ringing the product.
    background = background.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(1.2))
    alpha = ImageChops.invert(background)
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    box = alpha.point(lambda v: 255 if v > 24 else 0).getbbox()
    if box is None:
        return None
    return out.crop(box)


def _rounded_tile(im: Image.Image, radius: int) -> Image.Image:
    tile = im.convert("RGBA")
    mask = Image.new("L", tile.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, tile.width - 1, tile.height - 1), radius, fill=255)
    tile.putalpha(mask)
    return tile


def render_offer_picture(data: bytes, design: OfferDesign = DEFAULT_DESIGN, fmt: str = "WEBP") -> bytes:
    """The catalogue photo *data* on the offer background, SIZE x SIZE."""
    im = _open(data)
    if im.mode in ("RGBA", "LA", "P"):
        flat = Image.new("RGB", im.size, _WHITE)
        rgba = im.convert("RGBA")
        flat.paste(rgba, (0, 0), rgba)
        im = flat

    product = _lift_from_white(im)
    box = _PRODUCT_BOX * SIZE
    if product is None:
        # Not on white: the photo itself, as a rounded tile on the disc.
        product = _rounded_tile(_resize(im.convert("RGB"), _fit(im.size, box * 0.82, box * 0.82, SIZE)), 28)
    else:
        product = _resize(product, _fit(product.size, box, box, SIZE))

    canvas = _frame(design).copy()
    cx, cy = _DISC_C[0] * SIZE, _DISC_C[1] * SIZE
    left = round(cx - product.width / 2)
    # Centred on the disc, nudged up so the ribbon never covers the product.
    top = round(min(cy - product.height / 2, 0.80 * SIZE - product.height))
    # Contact shadow under the product so it stands on the disc.
    a = product.getchannel("A").point(lambda v: v * 70 // 255)
    _shadow(canvas, a, (left, top), (0, 10), 12, (90, 40, 30), 255)
    canvas.alpha_composite(product, (left, top))

    out = io.BytesIO()
    if fmt == "JPEG":
        canvas.convert("RGB").save(out, "JPEG", quality=88, optimize=True, progressive=True)
    else:
        canvas.convert("RGB").save(out, "WEBP", quality=86, method=4)
    return out.getvalue()


__all__ = ["OFFER_TEMPLATE_VERSION", "OfferDesign", "DEFAULT_DESIGN", "render_offer_picture", "ImageryError"]
