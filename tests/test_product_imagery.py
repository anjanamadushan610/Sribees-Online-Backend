"""
Product imagery: a transparent cutout becomes the catalogue picture and the
share card. Pure image rules -- no database, no storage.
"""
import io

import pytest
from PIL import Image
from starlette.datastructures import Headers, UploadFile

from app.services import product_imagery as pi


def _encode(im: Image.Image, fmt: str = "PNG") -> bytes:
    out = io.BytesIO()
    im.save(out, fmt)
    return out.getvalue()


def _cutout(w: int, h: int, colour=(30, 90, 200)) -> bytes:
    """A product of w x h sitting in a transparent margin."""
    im = Image.new("RGBA", (w + 80, h + 60), (0, 0, 0, 0))
    im.paste(Image.new("RGBA", (w, h), (*colour, 255)), (40, 30))
    return _encode(im)


def _product_box(data: bytes) -> tuple[int, int, int, int]:
    rgb = Image.open(io.BytesIO(data)).convert("RGB")
    # Anything clearly not white is product.
    return rgb.convert("L").point(lambda v: 255 if v < 200 else 0).getbbox()


@pytest.mark.parametrize(
    "w,h",
    [(1200, 500), (300, 1100), (800, 800)],
    ids=["wide pack", "tall bottle", "square box"],
)
def test_cutout_is_centred_on_a_white_square_by_its_shape(w, h):
    out = pi.normalize_cutout(_cutout(w, h))

    im = Image.open(io.BytesIO(out))
    assert im.format == "WEBP"
    assert im.size == (pi.CATALOG_SIZE, pi.CATALOG_SIZE)
    assert im.convert("RGB").getpixel((5, 5)) == (255, 255, 255)

    x0, y0, x1, y1 = _product_box(out)
    pw, ph = x1 - x0, y1 - y0
    # The trimmed outline keeps its shape and is centred.
    assert abs(pw / ph - w / h) < 0.03
    assert abs((x0 + x1) / 2 - pi.CATALOG_SIZE / 2) <= 2
    assert abs((y0 + y1) / 2 - pi.CATALOG_SIZE / 2) <= 2
    # Never past the margin, and never lost in it either.
    assert max(pw, ph) <= pi.CATALOG_SIZE * 0.88 + 2
    assert max(pw, ph) >= pi.CATALOG_SIZE * 0.6


def test_a_square_box_does_not_outweigh_a_long_pack():
    wide = _product_box(pi.normalize_cutout(_cutout(1200, 500)))
    square = _product_box(pi.normalize_cutout(_cutout(800, 800)))
    area = lambda b: (b[2] - b[0]) * (b[3] - b[1])  # noqa: E731
    assert area(square) / area(wide) < 1.4


def test_an_opaque_photo_is_left_alone():
    photo = _encode(Image.new("RGB", (900, 600), (200, 30, 30)))
    assert pi.normalize_cutout(photo) is None


def test_a_tiny_cutout_is_refused_with_its_size():
    with pytest.raises(pi.ImageryError, match="at least 500 px"):
        pi.normalize_cutout(_cutout(200, 120))


def test_a_fully_transparent_png_is_refused():
    blank = _encode(Image.new("RGBA", (800, 800), (0, 0, 0, 0)))
    with pytest.raises(pi.ImageryError, match="completely transparent"):
        pi.normalize_cutout(blank)


def test_garbage_is_refused():
    with pytest.raises(pi.ImageryError):
        pi.normalize_cutout(b"<html>not a picture</html>")


def test_a_decompression_bomb_is_refused_before_decoding():
    huge = Image.new("L", (8000, 6000), 0)  # 48 MP, cheap to encode as L
    with pytest.raises(pi.ImageryError, match="too large"):
        pi.normalize_cutout(_encode(huge))


@pytest.mark.parametrize("source", ["cutout", "catalogue picture", "photo on white"])
def test_share_card_puts_the_product_above_the_brand_swoosh(source):
    cutout = _cutout(1200, 500, colour=(20, 160, 60))
    data = {
        "cutout": cutout,
        "catalogue picture": pi.normalize_cutout(cutout),
        "photo on white": _encode(
            Image.open(io.BytesIO(pi.normalize_cutout(cutout))).convert("RGB"), "JPEG"
        ),
    }[source]

    card = Image.open(io.BytesIO(pi.render_share_card(data)))
    assert card.format == "JPEG"
    assert card.size == (pi.SHARE_SIZE, pi.SHARE_SIZE)
    rgb = card.convert("RGB")
    s = pi.SHARE_SIZE

    # The swoosh is brand crimson in the lower right, white above it.
    r, g, b = rgb.getpixel((int(0.97 * s), int(0.86 * s)))
    assert r > 190 and g < 50 and 40 < b < 110
    assert rgb.getpixel((int(0.05 * s), int(0.95 * s))) == (255, 255, 255)

    # The product sits in the middle and stays clear of the swoosh.
    r, g, b = rgb.getpixel((s // 2, int(0.47 * s)))
    assert g > 120 and r < 60
    product_rows = [
        y for y in range(s)
        if any(rgb.getpixel((x, y))[0] < 60 and rgb.getpixel((x, y))[1] > 120 for x in (s // 3, s // 2))
    ]
    assert product_rows and max(product_rows) < 0.80 * s


@pytest.mark.asyncio
async def test_upload_lays_out_a_cutout_and_keeps_an_ordinary_photo(monkeypatch):
    from app.api.v1 import admin_catalog

    saved = []

    class Store:
        def save(self, fileobj, filename, folder, content_type=None):
            saved.append((filename, folder, content_type, fileobj.read()))
            return f"/uploads/{folder}/{filename}"

    monkeypatch.setattr(admin_catalog, "get_storage", lambda: Store())

    def upload(data, name, ctype):
        return UploadFile(
            io.BytesIO(data), filename=name, headers=Headers({"content-type": ctype})
        )

    await admin_catalog.upload_product_image(upload(_cutout(900, 400), "Cracker.png", "image/png"))
    photo = _encode(Image.new("RGB", (400, 400), (9, 9, 9)))
    await admin_catalog.upload_product_image(upload(photo, "tea.png", "image/png"))

    (name, folder, ctype, body), (name2, _, ctype2, body2) = saved
    assert (name, folder, ctype) == ("Cracker.webp", "products", "image/webp")
    assert Image.open(io.BytesIO(body)).size == (pi.CATALOG_SIZE, pi.CATALOG_SIZE)
    assert (name2, ctype2, body2) == ("tea.png", "image/png", photo)


@pytest.mark.asyncio
async def test_upload_explains_why_a_cutout_was_refused(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import admin_catalog

    small = UploadFile(
        io.BytesIO(_cutout(100, 100)), filename="x.png",
        headers=Headers({"content-type": "image/png"}),
    )
    with pytest.raises(HTTPException) as err:
        await admin_catalog.upload_product_image(small)
    assert err.value.status_code == 400
    assert "at least 500 px" in err.value.detail
