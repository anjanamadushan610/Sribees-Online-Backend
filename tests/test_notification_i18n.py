"""
Every server notification in three languages, with the same placeholders.

A Sinhala template missing {order_number} would reach a customer as a message
about "your order" with no way to tell which; a Tamil one with a typo'd
placeholder would show the braces. Both are caught here, not on a phone.
"""
import string
import unicodedata

import pytest

from app.services import notification_i18n as i18n

_FIELDS = string.Formatter()


def _placeholders(text: str) -> set:
    return {name for _, name, _, _ in _FIELDS.parse(text) if name}


def _in_block(text: str, low: int, high: int) -> bool:
    """True when ``text`` is written in that script (Sinhala, Tamil)."""
    return any(low <= ord(c) <= high for c in text)


def _every_table():
    for key, template in i18n.TEMPLATES.items():
        yield f"{key}.title", template["title"]
        yield f"{key}.message", template["message"]
    for status, table in i18n._ORDER_STATUS.items():
        yield f"order.status.{status}", table
    yield "order.status.<fallback>", i18n._ORDER_STATUS_FALLBACK
    yield "order.title", i18n._ORDER_TITLE
    yield "reward", i18n._REWARD
    for kind, table in i18n._OFF.items():
        yield f"off.{kind}", table


@pytest.mark.parametrize("name,table", list(_every_table()))
def test_all_three_languages_with_the_same_placeholders(name, table):
    assert set(table) == set(i18n.LANGUAGES), name
    expected = _placeholders(table["en"])
    for lang in i18n.LANGUAGES:
        assert table[lang].strip(), f"{name}.{lang} is empty"
        assert _placeholders(table[lang]) == expected, f"{name}.{lang}"


@pytest.mark.parametrize("name,table", list(_every_table()))
def test_no_invisible_characters(name, table):
    """Zero-width joiners pasted through tooling have corrupted copy here before."""
    for lang, text in table.items():
        bad = [hex(ord(c)) for c in text if unicodedata.category(c) == "Cf"]
        assert not bad, f"{name}.{lang}: {bad}"


def test_every_order_status_names_the_order():
    """The push body is the message alone, so it must say which order."""
    from app.models.order import OrderStatus

    for status in OrderStatus:
        for lang in i18n.LANGUAGES:
            _, message = i18n.render(f"order.status.{status.value}", lang, {"order_number": "SB-1"})
            assert "#SB-1" in message, (status, lang)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "en"),
        ("", "en"),
        ("si", "si"),
        ("si-LK", "si"),
        ("ta_LK", "ta"),
        ("TA", "ta"),
        ("fr,ta;q=0.8,en;q=0.5", "ta"),
        ("de", "en"),
    ],
)
def test_language_codes_and_accept_language_headers(raw, expected):
    assert i18n.normalize_lang(raw) == expected


def test_render_in_each_language():
    en = i18n.render("order.status.out_for_delivery", "en", {"order_number": "SB-9"})
    si = i18n.render("order.status.out_for_delivery", "si", {"order_number": "SB-9"})
    ta = i18n.render("order.status.out_for_delivery", "ta", {"order_number": "SB-9"})
    assert en == ("Order #SB-9", "Your order #SB-9 is out for delivery.")
    assert si[0].endswith("#SB-9") and "#SB-9" in si[1] and _in_block(si[1], 0x0D80, 0x0DFF)
    assert ta[0].endswith("#SB-9") and _in_block(ta[1], 0x0B80, 0x0BFF)


def test_an_unknown_status_still_reads_like_a_sentence():
    _, message = i18n.render("order.status.teleported", "en", {"order_number": "SB-2"})
    assert message == "Your order #SB-2 has been updated."


def test_a_missing_parameter_is_left_visible_not_a_crash():
    _, message = i18n.render("order.placed", "en", {})
    assert "{order_number}" in message


def test_per_language_parameters():
    texts = i18n.render_all(
        "promo.gift",
        {"code": "WIN-AB12", "days": 7},
        lambda lang: {"off": i18n.coupon_value("percentage", 15, lang)},
    )
    assert texts["en"][1] == "15% off with code WIN-AB12. Valid for 7 days."
    assert "15% වට්ටමක්" in texts["si"][1]
    assert "15% தள்ளுபடி" in texts["ta"][1]
    assert i18n.coupon_value("fixed", 350, "en") == "Rs. 350.00 off"


def test_reward_text():
    assert (
        i18n.reward_text("fixed", 500, "GOLD-1", 30, "en")
        == "Rs. 500.00 off with code GOLD-1, valid 30 days"
    )
    assert "GOLD-1" in i18n.reward_text("fixed", 500, "GOLD-1", 30, "si")


def test_push_body_puts_the_headline_first_unless_the_message_says_it_all():
    assert i18n.push_body("Order #1", "Your order #1 shipped.", True) == "Your order #1 shipped."
    assert i18n.push_body("Weekend sale", "20% off fruit", False) == "Weekend sale\n20% off fruit"
    assert i18n.push_body("", "Only text", False) == "Only text"


def test_campaign_copy_falls_back_to_english_per_field():
    texts = i18n.localized_all(
        {"en": "Sale", "si": "අලෙවිය", "ta": ""},
        {"en": "20% off", "si": "", "ta": None},
    )
    assert texts["si"] == ("අලෙවිය", "20% off")
    assert texts["ta"] == ("Sale", "20% off")


def test_brand_title_matches_the_app_name():
    """The push title is the name under the app icon (AndroidManifest label)."""
    assert i18n.BRAND_TITLE == "Sribees Online"
