"""
11.11 festival offer: the rate rule, its fixed end, the picture URLs, and the
offer picture itself. Pure rules -- no database.
"""
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PIL import Image

from app.config.settings import settings
from app.core.media import media_path
from app.services import festival_offer as fo
from app.services import offer_imagery as oi
from app.services import product_imagery as pi

DURING = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
AFTER = fo.OFFER_1111.ends_at


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.delenv("FESTIVAL_OFFER_DISABLED", raising=False)


def test_offer_ends_at_midnight_sri_lanka_time():
    # 2026-11-11 23:59 in Colombo (UTC+05:30) is the last offer minute.
    colombo = timezone(timedelta(hours=5, minutes=30))
    last_minute = datetime(2026, 11, 11, 23, 59, 59, tzinfo=colombo)
    assert fo.active_offer(last_minute) is fo.OFFER_1111
    assert fo.active_offer(datetime(2026, 11, 12, 0, 0, tzinfo=colombo)) is None
    assert fo.active_offer(AFTER) is None
    assert fo.active_offer(AFTER - timedelta(seconds=1)) is fo.OFFER_1111


@pytest.mark.parametrize("rate", [0.5, 1, 1.5, 30, 11])
def test_every_paying_rate_becomes_exactly_11(rate):
    assert fo.apply_cashback(rate, DURING) == 11.0


@pytest.mark.parametrize("rate", [0, 0.0])
def test_zero_rate_products_stay_zero(rate):
    assert fo.apply_cashback(rate, DURING) == 0


def test_rates_untouched_after_the_offer():
    for rate in (0, 0.5, 1.5, 30):
        assert fo.apply_cashback(rate, AFTER) == rate
    assert fo.apply_cashback(None, DURING) is None


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("FESTIVAL_OFFER_DISABLED", "1")
    assert fo.active_offer(DURING) is None
    assert fo.apply_cashback(1.5, DURING) == 1.5


def test_picture_url_during_and_after(monkeypatch):
    monkeypatch.setattr(settings, "media_base_url", "https://api.example.lk")
    path = "/uploads/products/Raigam Coffee.webp"
    with_ribbon = fo.offer_image_url(path, 11, DURING)
    assert with_ribbon == "https://api.example.lk/offer/1111/c/uploads/products/Raigam%20Coffee.webp"
    assert fo.offer_image_url(path, 0, DURING).startswith("https://api.example.lk/offer/1111/n/")
    assert fo.offer_image_url(path, 11, AFTER) == "https://api.example.lk/uploads/products/Raigam Coffee.webp"
    # Not a product picture, or not ours: never redrawn.
    assert fo.offer_image_url("/uploads/banners/b.jpg", 11, DURING).endswith("/uploads/banners/b.jpg")
    assert fo.offer_image_url("https://cdn.x/p.jpg", 11, DURING) == "https://cdn.x/p.jpg"


def test_an_offer_url_is_never_stored(monkeypatch):
    monkeypatch.setattr(settings, "media_base_url", "https://api.example.lk")
    url = fo.offer_image_url("/uploads/products/Raigam Coffee.webp", 11, DURING)
    assert media_path(url) == "/uploads/products/Raigam Coffee.webp"
    # Plain URLs reduce as before.
    assert media_path("https://api.example.lk/uploads/products/a.webp") == "/uploads/products/a.webp"


def test_strip_only_touches_our_own_offer_urls(monkeypatch):
    monkeypatch.setattr(settings, "media_base_url", "https://api.example.lk")
    other = "https://cdn.other.com/offer/1111/c/uploads/products/a.webp"
    assert fo.strip_offer_url(other) == other
    assert fo.strip_offer_url("/offer/1111/n/uploads/products/a.webp") == "/uploads/products/a.webp"


def test_order_line_photo_is_kept_as_the_plain_url(monkeypatch):
    monkeypatch.setattr(settings, "media_base_url", "https://api.example.lk")
    url = fo.offer_image_url("/uploads/products/a b.webp", 11, DURING)
    assert fo.plain_image_url(url) == "https://api.example.lk/uploads/products/a b.webp"
    plain = "https://api.example.lk/uploads/products/c.webp"
    assert fo.plain_image_url(plain) == plain
    assert fo.plain_image_url(None) is None


def _catalogue_picture() -> bytes:
    """A product the way an upload stores it: a cutout on the white square."""
    im = Image.new("RGBA", (900, 700), (0, 0, 0, 0))
    im.paste(Image.new("RGBA", (700, 500), (30, 90, 200, 255)), (100, 100))
    # White inside the product (a label) must stay product, not background.
    im.paste(Image.new("RGBA", (200, 120), (255, 255, 255, 255)), (350, 290))
    out = io.BytesIO()
    im.save(out, "PNG")
    return pi.normalize_cutout(out.getvalue())


def test_offer_picture_puts_the_product_on_the_offer_background():
    pic = Image.open(io.BytesIO(oi.render_offer_picture(_catalogue_picture()))).convert("RGB")
    assert pic.size == (oi.SIZE, oi.SIZE)
    # The corner is the offer background, not white.
    r, g, b = pic.getpixel((5, oi.SIZE // 2))
    assert r > 120 and g < 80
    # The product (blue) is there, centred on the disc.
    cx, cy = round(oi._DISC_C[0] * oi.SIZE), round(oi._DISC_C[1] * oi.SIZE)
    r, g, b = pic.getpixel((cx - 150, cy - 60))
    assert b > 150 and r < 90


def test_lift_keeps_white_inside_the_product():
    src = Image.open(io.BytesIO(_catalogue_picture()))
    lifted = oi._lift_from_white(src)
    assert lifted is not None
    # The middle of the product is the white label: it must stay opaque.
    a = lifted.getchannel("A").getpixel((lifted.width // 2, lifted.height // 2))
    assert a == 255


def test_a_photo_not_on_white_is_placed_whole():
    im = Image.new("RGB", (800, 800), (40, 140, 60))
    out = io.BytesIO()
    im.save(out, "JPEG")
    assert oi._lift_from_white(Image.open(io.BytesIO(out.getvalue()))) is None
    pic = oi.render_offer_picture(out.getvalue())
    assert Image.open(io.BytesIO(pic)).size == (oi.SIZE, oi.SIZE)


def test_no_ribbon_variant_and_jpeg_for_share():
    data = _catalogue_picture()
    plain = oi.render_offer_picture(data, oi.OfferDesign(ribbon=""), "JPEG")
    assert Image.open(io.BytesIO(plain)).format == "JPEG"
    ribbon = Image.open(io.BytesIO(oi.render_offer_picture(data))).convert("RGB")
    none = Image.open(io.BytesIO(plain)).convert("RGB")
    # The ribbon is gold where the plain card is background.
    # Just inside the band's left end, above the lettering.
    y = round(0.815 * oi.SIZE) + 16
    rr, rg, _ = ribbon.getpixel((oi.SIZE // 2 - 380, y))
    nr, ng, _ = none.getpixel((oi.SIZE // 2 - 380, y))
    assert rg > 150 and ng < 150


def test_share_card_design_follows_the_product_rate(monkeypatch):
    from app.api import share_links

    monkeypatch.setattr(fo, "active_offer", lambda now=None: fo.OFFER_1111)
    assert share_links._offer_design(SimpleNamespace(cashback_percentage=1.5)).ribbon
    assert share_links._offer_design(SimpleNamespace(cashback_percentage=0)).ribbon == ""
    monkeypatch.setattr(fo, "active_offer", lambda now=None: None)
    assert share_links._offer_design(SimpleNamespace(cashback_percentage=1.5)) is None
