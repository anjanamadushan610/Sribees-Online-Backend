"""
The remote Lottie splash.

Every installed copy plays this file at its very first frame, so the upload
check is the last point where a bad file can be turned away with a message the
admin can act on. After that it is a support call.
"""
import json

import pytest

from app.api.v1.app_public import _for_client
from app.services.splash_animation import (
    DEFAULT_BACKGROUND,
    MAX_BYTES,
    SplashAnimationError,
    inspect_lottie,
    normalise_hex_color,
    public_splash,
)


def _lottie(**overrides) -> bytes:
    doc = {
        "v": "5.7.4", "fr": 30, "ip": 0, "op": 90, "w": 512, "h": 512,
        "layers": [{"ty": 4, "nm": "shape"}],
        "assets": [],
    }
    doc.update(overrides)
    return json.dumps(doc).encode()


def _code(raw: bytes) -> str:
    with pytest.raises(SplashAnimationError) as exc:
        inspect_lottie(raw)
    return exc.value.code


def test_a_plain_lottie_is_described():
    meta = inspect_lottie(_lottie())
    assert meta["width"] == 512 and meta["height"] == 512
    assert meta["duration_ms"] == 3000
    assert len(meta["sha256"]) == 64


def test_the_version_changes_with_the_file():
    assert inspect_lottie(_lottie())["sha256"] != inspect_lottie(_lottie(op=60))["sha256"]


def test_a_byte_order_mark_is_tolerated():
    # Windows editors save one; it must not read as "not JSON".
    assert inspect_lottie(b"\xef\xbb\xbf" + _lottie())["duration_ms"] == 3000


@pytest.mark.parametrize(
    "raw, code",
    [
        (b"", "EMPTY_FILE"),
        (b"{not json", "INVALID_JSON"),
        (b"PK\x03\x04zipped", "DOTLOTTIE_NOT_SUPPORTED"),
        (b"[1, 2]", "INVALID_LOTTIE"),
        (json.dumps({"hello": "world"}).encode(), "INVALID_LOTTIE"),
    ],
)
def test_things_that_are_not_a_lottie_are_refused(raw, code):
    assert _code(raw) == code


def test_a_lottie_with_no_frames_is_refused():
    assert _code(_lottie(op=0)) == "INVALID_LOTTIE"
    assert _code(_lottie(fr=0)) == "INVALID_LOTTIE"


def test_a_boolean_is_not_a_frame_rate():
    assert _code(_lottie(fr=True)) == "INVALID_LOTTIE"


def test_a_lottie_with_no_layers_is_refused():
    assert _code(_lottie(layers=[])) == "INVALID_LOTTIE"


def test_a_splash_longer_than_ten_seconds_is_refused():
    assert _code(_lottie(op=30 * 11)) == "TOO_LONG"


def test_an_oversized_file_is_refused():
    assert _code(b" " * (MAX_BYTES + 1)) == "FILE_TOO_LARGE"


def test_an_external_image_is_refused():
    raw = _lottie(assets=[{"id": "image_0", "u": "images/", "p": "img_0.png", "e": 0}])
    assert _code(raw) == "EXTERNAL_ASSETS"


def test_embedded_images_and_precomps_are_fine():
    raw = _lottie(assets=[
        {"id": "image_0", "u": "", "p": "data:image/png;base64,iVBORw0KGgo=", "e": 1},
        {"id": "comp_0", "layers": []},
    ])
    assert inspect_lottie(raw)["duration_ms"] == 3000


@pytest.mark.parametrize(
    "given, stored",
    [("#d2114e", "#D2114E"), ("D2114E", "#D2114E"), (None, DEFAULT_BACKGROUND), ("  ", DEFAULT_BACKGROUND)],
)
def test_colours_are_normalised(given, stored):
    assert normalise_hex_color(given) == stored


@pytest.mark.parametrize("bad", ["red", "#FFF", "#GGGGGG", "#D2114E00"])
def test_colours_that_are_not_six_digit_hex_are_refused(bad):
    with pytest.raises(SplashAnimationError):
        normalise_hex_color(bad)


def test_a_switched_off_animation_is_not_published():
    value = {"path": "/uploads/splash/a.json", "sha256": "abc", "background_color": "#000000"}
    assert public_splash(value, is_active=False) is None
    assert public_splash(None, is_active=True) is None
    assert public_splash(value, is_active=True)["version"] == "abc"


def test_the_phone_gets_a_url_never_the_stored_path():
    data = {"flags": {}, "splash": {"path": "/uploads/splash/a.json", "version": "abc"}}
    out = _for_client(data, None)
    assert "path" not in out["splash"]
    assert out["splash"]["animation_url"].endswith("/uploads/splash/a.json")
    # The cached dict is shared between requests and must not be rewritten.
    assert data["splash"]["path"] == "/uploads/splash/a.json"


def test_a_config_cached_before_splash_existed_still_answers():
    assert _for_client({"flags": {}}, None)["splash"] is None
