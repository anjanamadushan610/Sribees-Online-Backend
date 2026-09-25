"""
SRIBEESonline — what the server says to customers, in their language.

Every notification the backend writes goes through one of the templates below
and is rendered in English, Sinhala and Tamil. The in-app row is stored in the
customer's ``preferred_language``; each push is rendered for the device it is
going to (``push_tokens.language``), so the words match the app they open.

Pushes carry one title: :data:`BRAND_TITLE`. What the notification is about
goes in the body. A template marked ``standalone`` already says everything in
its message ("Your order #SB-123 is out for delivery"), so the body is just
that; otherwise the body is the headline on its own line, then the message.

Adding a template: all three languages, same ``{placeholders}`` in each.
``tests/test_notification_i18n.py`` fails otherwise.
"""
from typing import Dict, Optional, Tuple

# The app's name as it appears under the icon (AndroidManifest label).
BRAND_TITLE = "Sribees Online"

LANGUAGES: Tuple[str, ...] = ("en", "si", "ta")
DEFAULT_LANGUAGE = "en"


def normalize_lang(value: Optional[str]) -> str:
    """
    'si', 'si-LK', 'ta,en;q=0.8', 'EN' → a supported code; anything else → 'en'.

    Accepts a bare code or a whole ``Accept-Language`` header and takes the
    first language in it we support.
    """
    if not value:
        return DEFAULT_LANGUAGE
    for part in str(value).split(","):
        code = part.split(";")[0].strip().lower().split("-")[0].split("_")[0]
        if code in LANGUAGES:
            return code
    return DEFAULT_LANGUAGE


def money(amount) -> str:
    return f"Rs. {float(amount):,.2f}"


# Order status → message. Every one names the order, so the push body can be
# the message alone. The title in the app's feed is "Order #…".
_ORDER_STATUS: Dict[str, Dict[str, str]] = {
    "confirmed": {
        "en": "Your order #{order_number} has been confirmed.",
        "si": "ඔබේ ඇණවුම #{order_number} තහවුරු කළා.",
        "ta": "உங்கள் ஆர்டர் #{order_number} உறுதிப்படுத்தப்பட்டது.",
    },
    "processing": {
        "en": "We're preparing your order #{order_number}.",
        "si": "ඔබේ ඇණවුම #{order_number} සූදානම් කරමින් පවතිනවා.",
        "ta": "உங்கள் ஆர்டர் #{order_number} தயாராகிறது.",
    },
    "packed": {
        "en": "Your order #{order_number} is packed and ready to ship.",
        "si": "ඔබේ ඇණවුම #{order_number} ඇසුරුම් කර යැවීමට සූදානම්.",
        "ta": "உங்கள் ஆர்டர் #{order_number} பேக் செய்யப்பட்டு அனுப்பத் தயாராக உள்ளது.",
    },
    "ready_for_pickup": {
        "en": "Your order #{order_number} is packed and the courier is on the way to collect it.",
        "si": "ඔබේ ඇණවුම #{order_number} ඇසුරුම් කළා. එය රැගෙන යාමට කුරියර් පැමිණෙමින් සිටිනවා.",
        "ta": "உங்கள் ஆர்டர் #{order_number} பேக் செய்யப்பட்டது; அதை எடுக்க கூரியர் வருகிறார்.",
    },
    "handed_to_courier": {
        "en": "Your order #{order_number} has been handed to the courier.",
        "si": "ඔබේ ඇණවුම #{order_number} කුරියර් සේවයට භාර දුන්නා.",
        "ta": "உங்கள் ஆர்டர் #{order_number} கூரியரிடம் ஒப்படைக்கப்பட்டது.",
    },
    "shipped": {
        "en": "Your order #{order_number} is on its way.",
        "si": "ඔබේ ඇණවුම #{order_number} ඔබ වෙත එමින් පවතිනවා.",
        "ta": "உங்கள் ஆர்டர் #{order_number} உங்களை நோக்கி வருகிறது.",
    },
    "out_for_delivery": {
        "en": "Your order #{order_number} is out for delivery.",
        "si": "ඔබේ ඇණවුම #{order_number} බෙදාහැරීමට පිටත් වුණා.",
        "ta": "உங்கள் ஆர்டர் #{order_number} டெலிவரிக்காகப் புறப்பட்டுள்ளது.",
    },
    "delivered": {
        "en": "Your order #{order_number} has been delivered. Enjoy!",
        "si": "ඔබේ ඇණවුම #{order_number} ඔබට භාර දුන්නා. රසවිඳින්න!",
        "ta": "உங்கள் ஆர்டர் #{order_number} டெலிவரி செய்யப்பட்டது. மகிழுங்கள்!",
    },
    "delivery_failed": {
        "en": "We couldn't deliver your order #{order_number}. Our team will be in touch.",
        "si": "ඔබේ ඇණවුම #{order_number} බෙදාහැරීමට නොහැකි වුණා. අපේ කණ්ඩායම ඔබව සම්බන්ධ කරගනීවි.",
        "ta": "உங்கள் ஆர்டர் #{order_number} ஐ டெலிவரி செய்ய முடியவில்லை. எங்கள் குழு உங்களைத் தொடர்பு கொள்ளும்.",
    },
    "rto_initiated": {
        "en": "Your order #{order_number} is being returned to our store.",
        "si": "ඔබේ ඇණවුම #{order_number} අපේ ශාඛාවට ආපසු යවමින් පවතිනවා.",
        "ta": "உங்கள் ஆர்டர் #{order_number} எங்கள் கிளைக்குத் திருப்பி அனுப்பப்படுகிறது.",
    },
    "cancelled": {
        "en": "Your order #{order_number} has been cancelled.",
        "si": "ඔබේ ඇණවුම #{order_number} අවලංගු කළා.",
        "ta": "உங்கள் ஆர்டர் #{order_number} ரத்து செய்யப்பட்டது.",
    },
    "return_requested": {
        "en": "We've received your return request for order #{order_number}.",
        "si": "ඇණවුම #{order_number} සඳහා ඔබේ ආපසු දීමේ ඉල්ලීම ලැබුණා.",
        "ta": "ஆர்டர் #{order_number} க்கான உங்கள் திருப்பி அளிக்கும் கோரிக்கை பெறப்பட்டது.",
    },
    "return_approved": {
        "en": "Your return for order #{order_number} has been approved.",
        "si": "ඇණවුම #{order_number} සඳහා ඔබේ ආපසු දීම අනුමත කළා.",
        "ta": "ஆர்டர் #{order_number} க்கான உங்கள் திருப்பி அளிப்பு அங்கீகரிக்கப்பட்டது.",
    },
    "refunded": {
        "en": "Your refund for order #{order_number} has been processed.",
        "si": "ඇණවුම #{order_number} සඳහා ඔබේ මුදල් ආපසු ගෙවා අවසන්.",
        "ta": "ஆர்டர் #{order_number} க்கான உங்கள் பணம் திருப்பி அளிக்கப்பட்டது.",
    },
}
# Legacy / synonymous states say the same as their modern twin.
_ORDER_STATUS["packing"] = _ORDER_STATUS["processing"]

_ORDER_STATUS_FALLBACK = {
    "en": "Your order #{order_number} has been updated.",
    "si": "ඔබේ ඇණවුම #{order_number} යාවත්කාලීන කළා.",
    "ta": "உங்கள் ஆர்டர் #{order_number} புதுப்பிக்கப்பட்டது.",
}

_ORDER_TITLE = {
    "en": "Order #{order_number}",
    "si": "ඇණවුම #{order_number}",
    "ta": "ஆர்டர் #{order_number}",
}


# key → {"title": {lang: str}, "message": {lang: str}, "standalone": bool}
TEMPLATES: Dict[str, dict] = {
    "order.placed": {
        "standalone": True,
        "title": {
            "en": "Order placed",
            "si": "ඇණවුම ලැබුණා",
            "ta": "ஆர்டர் பெறப்பட்டது",
        },
        "message": {
            "en": "Your order #{order_number} has been placed successfully.",
            "si": "ඔබේ ඇණවුම #{order_number} සාර්ථකව ලැබුණා.",
            "ta": "உங்கள் ஆர்டர் #{order_number} வெற்றிகரமாகப் பெறப்பட்டது.",
        },
    },
    "payment.success": {
        "standalone": True,
        "title": {
            "en": "Payment successful",
            "si": "ගෙවීම සාර්ථකයි",
            "ta": "கட்டணம் வெற்றிகரமாகச் செலுத்தப்பட்டது",
        },
        "message": {
            "en": "Payment of {amount} received for order #{order_number}.",
            "si": "ඇණවුම #{order_number} සඳහා {amount} ක ගෙවීම ලැබුණා.",
            "ta": "ஆர்டர் #{order_number} க்கான {amount} கட்டணம் பெறப்பட்டது.",
        },
    },
    "cashback.settled.one": {
        "standalone": True,
        "title": {
            "en": "Cashback added to your wallet",
            "si": "Cashback ඔබේ Wallet එකට එකතු විය",
            "ta": "கேஷ்பேக் உங்கள் வாலட்டில் சேர்ந்தது",
        },
        "message": {
            "en": "{amount} cashback from order #{order_number} is now in your wallet.",
            "si": "ඇණවුම #{order_number} න් {amount} Cashback දැන් ඔබේ Wallet එකේ.",
            "ta": "ஆர்டர் #{order_number} இலிருந்து {amount} கேஷ்பேக் இப்போது உங்கள் வாலட்டில் உள்ளது.",
        },
    },
    "cashback.settled.many": {
        "standalone": True,
        "title": {
            "en": "Cashback added to your wallet",
            "si": "Cashback ඔබේ Wallet එකට එකතු විය",
            "ta": "கேஷ்பேக் உங்கள் வாலட்டில் சேர்ந்தது",
        },
        "message": {
            "en": "{amount} cashback from {count} orders is now in your wallet.",
            "si": "ඇණවුම් {count} කින් {amount} Cashback දැන් ඔබේ Wallet එකේ.",
            "ta": "{count} ஆர்டர்களிலிருந்து {amount} கேஷ்பேக் இப்போது உங்கள் வாலட்டில் உள்ளது.",
        },
    },
    "loyalty.level.reward": {
        "standalone": True,
        "title": {
            "en": "Welcome to {tier}",
            "si": "{tier} මට්ටමට සාදරයෙන් පිළිගනිමු",
            "ta": "{tier} நிலைக்கு வரவேற்கிறோம்",
        },
        "message": {
            "en": "You've reached {tier}! Your reward: {reward}.",
            "si": "ඔබ {tier} මට්ටමට ළඟා වුණා! ඔබේ තෑග්ග: {reward}.",
            "ta": "நீங்கள் {tier} நிலையை அடைந்துள்ளீர்கள்! உங்கள் வெகுமதி: {reward}.",
        },
    },
    "loyalty.level.plain": {
        "standalone": True,
        "title": {
            "en": "Welcome to {tier}",
            "si": "{tier} මට්ටමට සාදරයෙන් පිළිගනිමු",
            "ta": "{tier} நிலைக்கு வரவேற்கிறோம்",
        },
        "message": {
            "en": "You've reached {tier} with {points} points.",
            "si": "ලකුණු {points} ක් සමඟ ඔබ {tier} මට්ටමට ළඟා වුණා.",
            "ta": "{points} புள்ளிகளுடன் நீங்கள் {tier} நிலையை அடைந்துள்ளீர்கள்.",
        },
    },
    "referral.ready.one": {
        "standalone": False,
        "title": {
            "en": "Your referral earnings are ready",
            "si": "ඔබේ යොමු කිරීම් ආදායම සූදානම්",
            "ta": "உங்கள் பரிந்துரை வருமானம் தயார்",
        },
        "message": {
            "en": "{amount} is ready to move into your wallet.",
            "si": "{amount} ඔබේ Wallet එකට මාරු කිරීමට සූදානම්.",
            "ta": "{amount} உங்கள் வாலட்டுக்கு மாற்றத் தயாராக உள்ளது.",
        },
    },
    "referral.ready.many": {
        "standalone": False,
        "title": {
            "en": "Your referral earnings are ready",
            "si": "ඔබේ යොමු කිරීම් ආදායම සූදානම්",
            "ta": "உங்கள் பரிந்துரை வருமானம் தயார்",
        },
        "message": {
            "en": "{amount} from {orders} orders is ready to move into your wallet.",
            "si": "ඇණවුම් {orders} කින් {amount} ඔබේ Wallet එකට මාරු කිරීමට සූදානම්.",
            "ta": "{orders} ஆர்டர்களிலிருந்து {amount} உங்கள் வாலட்டுக்கு மாற்றத் தயாராக உள்ளது.",
        },
    },
    "promo.gift": {
        "standalone": False,
        "title": {
            "en": "A gift from your branch 🎁",
            "si": "ඔබේ ශාඛාවෙන් තෑග්ගක් 🎁",
            "ta": "உங்கள் கிளையிலிருந்து ஒரு பரிசு 🎁",
        },
        "message": {
            "en": "{off} with code {code}. Valid for {days} days.",
            "si": "{code} කේතය සමඟ {off}. දින {days} ක් වලංගුයි.",
            "ta": "{code} குறியீட்டுடன் {off}. {days} நாட்களுக்குச் செல்லுபடியாகும்.",
        },
    },
    "price.drop": {
        "standalone": True,
        "title": {
            "en": "Price drop! 🎉",
            "si": "මිල අඩු වුණා! 🎉",
            "ta": "விலை குறைந்தது! 🎉",
        },
        "message": {
            "en": "{product} is now {price} ({percent}% off).",
            "si": "{product} දැන් {price} යි ({percent}% අඩුවෙන්).",
            "ta": "{product} இப்போது {price} ({percent}% தள்ளுபடி).",
        },
    },
}

# A coupon handed over as a reward, inside a loyalty message.
_REWARD = {
    "en": "{off} with code {code}, valid {days} days",
    "si": "{code} කේතය සමඟ {off}, දින {days} ක් වලංගුයි",
    "ta": "{code} குறியீட்டுடன் {off}, {days} நாட்கள் செல்லுபடியாகும்",
}

_OFF = {
    "percentage": {"en": "{value}% off", "si": "{value}% වට්ටමක්", "ta": "{value}% தள்ளுபடி"},
    "fixed": {"en": "{value} off", "si": "{value} ක වට්ටමක්", "ta": "{value} தள்ளுபடி"},
}


class _Keep(dict):
    """format_map that leaves an unknown {placeholder} as-is instead of raising."""

    def __missing__(self, key):
        return "{" + key + "}"


def _fill(text: str, params: dict) -> str:
    return text.format_map(_Keep(params))


def _pick(table: Dict[str, str], lang: str) -> str:
    return table.get(lang) or table[DEFAULT_LANGUAGE]


def coupon_value(discount_type: str, value, lang: str) -> str:
    """'15% off' / 'Rs. 350.00 off', in ``lang``."""
    kind = "percentage" if discount_type == "percentage" else "fixed"
    shown = f"{float(value):g}" if kind == "percentage" else money(value)
    return _fill(_pick(_OFF[kind], normalize_lang(lang)), {"value": shown})


def reward_text(discount_type: str, value, code: str, days: int, lang: str) -> str:
    """"15% off with code GOLD-AB12, valid 30 days", in ``lang``."""
    lang = normalize_lang(lang)
    off = coupon_value(discount_type, value, lang)
    return _fill(_pick(_REWARD, lang), {"off": off, "code": code, "days": days})


def is_standalone(key: str) -> bool:
    if key.startswith("order.status."):
        return True
    return bool(TEMPLATES[key].get("standalone"))


def render(key: str, lang: str, params: Optional[dict] = None) -> Tuple[str, str]:
    """(title, message) for template ``key`` in ``lang``."""
    lang = normalize_lang(lang)
    params = params or {}
    if key.startswith("order.status."):
        status = key[len("order.status."):]
        message = _pick(_ORDER_STATUS.get(status, _ORDER_STATUS_FALLBACK), lang)
        return _fill(_pick(_ORDER_TITLE, lang), params), _fill(message, params)
    template = TEMPLATES[key]
    return (
        _fill(_pick(template["title"], lang), params),
        _fill(_pick(template["message"], lang), params),
    )


def render_all(key: str, params: Optional[dict] = None, per_lang_params=None) -> Dict[str, Tuple[str, str]]:
    """
    (title, message) for every language.

    ``per_lang_params(lang)`` may add parameters that are themselves
    language-specific, such as a coupon's "15% off".
    """
    out = {}
    for lang in LANGUAGES:
        merged = dict(params or {})
        if per_lang_params is not None:
            merged.update(per_lang_params(lang))
        out[lang] = render(key, lang, merged)
    return out


def literal_all(title: str, message: str) -> Dict[str, Tuple[str, str]]:
    """The same text for every language — for copy that arrives already written."""
    return {lang: (title, message) for lang in LANGUAGES}


def localized_all(title: Dict[str, str], message: Dict[str, str]) -> Dict[str, Tuple[str, str]]:
    """Per-language copy a person wrote (a campaign); blanks fall back to English."""
    out = {}
    for lang in LANGUAGES:
        t = (title.get(lang) or "").strip() or (title.get(DEFAULT_LANGUAGE) or "").strip()
        m = (message.get(lang) or "").strip() or (message.get(DEFAULT_LANGUAGE) or "").strip()
        out[lang] = (t, m)
    return out


def push_body(title: str, message: str, standalone: bool) -> str:
    """The body under :data:`BRAND_TITLE`: the message, headed by the title when it needs one."""
    if standalone or not title:
        return message
    if not message:
        return title
    return f"{title}\n{message}"
