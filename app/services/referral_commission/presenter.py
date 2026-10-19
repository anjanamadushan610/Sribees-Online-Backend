"""
The one sentence that tells a referrer what they can earn, in en/si/ta.

Server-written, like the coupon cards: the rate is per product now, so the
client can no longer build "X% of every order" itself -- that sentence became
untrue the moment rates stopped being flat.
"""
from __future__ import annotations

from decimal import Decimal

from app.utils.i18n import resolve_language

_COPY = {
    "en": {
        "friend.offer": "Earn up to {rate}% on selected products your friends buy.",
        "friend.none": "Commission is paid on selected products. None are on offer right now.",
        "partner.offer": "Earn up to {rate}% on selected products your customers buy.",
        "partner.none": "Commission is paid on selected products. None are on offer right now.",
    },
    "si": {
        "friend.offer": "ඔබේ යාළුවන් මිලදී ගන්නා තෝරාගත් භාණ්ඩ වලින් {rate}% දක්වා උපයන්න.",
        "friend.none": "කොමිස් මුදල ලැබෙන්නේ තෝරාගත් භාණ්ඩ වලට පමණි. දැනට එවැනි භාණ්ඩ නොමැත.",
        "partner.offer": "ඔබේ ගනුදෙනුකරුවන් මිලදී ගන්නා තෝරාගත් භාණ්ඩ වලින් {rate}% දක්වා උපයන්න.",
        "partner.none": "කොමිස් මුදල ලැබෙන්නේ තෝරාගත් භාණ්ඩ වලට පමණි. දැනට එවැනි භාණ්ඩ නොමැත.",
    },
    "ta": {
        "friend.offer": "உங்கள் நண்பர்கள் வாங்கும் தேர்ந்தெடுக்கப்பட்ட பொருட்களில் {rate}% வரை சம்பாதியுங்கள்.",
        "friend.none": "தேர்ந்தெடுக்கப்பட்ட பொருட்களுக்கு மட்டுமே கமிஷன் வழங்கப்படும். தற்போது அத்தகைய பொருட்கள் இல்லை.",
        "partner.offer": "உங்கள் வாடிக்கையாளர்கள் வாங்கும் தேர்ந்தெடுக்கப்பட்ட பொருட்களில் {rate}% வரை சம்பாதியுங்கள்.",
        "partner.none": "தேர்ந்தெடுக்கப்பட்ட பொருட்களுக்கு மட்டுமே கமிஷன் வழங்கப்படும். தற்போது அத்தகைய பொருட்கள் இல்லை.",
    },
}


def _rate(value: Decimal) -> str:
    """2.50 -> "2.5", 5.00 -> "5"."""
    text = f"{Decimal(str(value)).quantize(Decimal('0.01')).normalize():f}"
    return text


def offer_text(highest_rate: Decimal, *, audience: str, accept_language: str | None) -> str:
    """``audience`` is "friend" (customer invites) or "partner"."""
    lang = resolve_language(accept_language)
    table = _COPY.get(lang) or _COPY["en"]
    if Decimal(str(highest_rate)) > 0:
        return table[f"{audience}.offer"].format(rate=_rate(highest_rate))
    return table[f"{audience}.none"]


def line_payload(line) -> dict:
    """A commission line as a beneficiary sees it. ``basis`` stays private:
    they see what they earned on a product, never what the customer paid."""
    return {
        "product_id": str(line.product_id),
        "product_name": line.product_name,
        "quantity": int(line.quantity),
        "returned_quantity": int(line.returned_quantity or 0),
        "rate_percentage": float(line.rate_percentage),
        "amount": float(line.amount),
    }
