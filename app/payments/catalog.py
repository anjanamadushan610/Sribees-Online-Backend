"""
Every way a customer can pay, defined once, on the server.

The app draws checkout from ``GET /payment-methods`` and knows nothing else:
the name and the line under it come from here in the customer's language, the
icon is one of a few generic shapes (``cash``, ``wallet``, ``card``, ``bank``,
``other``), and ``kind`` says how the money moves:

  offline  paid at the door (COD)
  wallet   the in-app wallet (drawn as the wallet toggle, not a choice)
  online   through a gateway: the order waits for the gateway's word

Adding a method is an entry here (and the code in PaymentMethodEnum, which
validates orders). Moving card payments to another gateway is changing
``gateway``/``option`` on these entries. Neither needs an app release.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config.settings import settings
from app.utils.i18n import DEFAULT_LANGUAGE


@dataclass(frozen=True)
class MethodSpec:
    code: str
    kind: str
    icon: str
    names: dict
    descriptions: dict
    legacy_type: str               # the "type" apps before this release read
    gateway: Optional[str] = None  # online only: which gateway carries it
    option: Optional[str] = None   # online only: the gateway's own name for it

    @property
    def is_online(self) -> bool:
        return self.kind == "online"

    def enabled(self) -> bool:
        if self.gateway is not None:
            return _GATEWAY_SWITCHES.get(self.gateway, lambda: False)()
        return True

    def name(self, lang: str) -> str:
        return self.names.get(lang) or self.names[DEFAULT_LANGUAGE]

    def description(self, lang: str) -> str:
        return self.descriptions.get(lang) or self.descriptions[DEFAULT_LANGUAGE]


# A gateway is offered only while it is switched on.
_GATEWAY_SWITCHES = {
    "marx": lambda: settings.marx_gateway_methods_enabled,
}

_CARD = {
    "en": "Pay securely by debit or credit card",
    "si": "ඩෙබිට් හෝ ක්‍රෙඩිට් කාඩ්පතෙන් ආරක්ෂිතව ගෙවන්න",
    "ta": "டெபிட் அல்லது கிரெடிட் கார்டு மூலம் பாதுகாப்பாக செலுத்துங்கள்",
}

METHODS: tuple[MethodSpec, ...] = (
    MethodSpec(
        code="COD",
        kind="offline",
        icon="cash",
        legacy_type="OFFLINE",
        names={
            "en": "Cash on Delivery",
            "si": "භාණ්ඩ ලැබුණු විට මුදල් ගෙවීම",
            "ta": "டெலிவரியின் போது பணம்",
        },
        descriptions={
            "en": "Pay the rider in cash when your order arrives",
            "si": "ඇණවුම ලැබෙන විට රයිඩර්ට මුදලින් ගෙවන්න",
            "ta": "ஆர்டர் வரும்போது ரைடரிடம் பணமாக செலுத்துங்கள்",
        },
    ),
    MethodSpec(
        code="WALLET",
        kind="wallet",
        icon="wallet",
        legacy_type="WALLET",
        names={"en": "SRIBEES Wallet", "si": "SRIBEES පසුම්බිය", "ta": "SRIBEES வாலட்"},
        descriptions={
            "en": "Pay from your wallet balance",
            "si": "ඔබේ පසුම්බියේ ශේෂයෙන් ගෙවන්න",
            "ta": "உங்கள் வாலட் இருப்பிலிருந்து செலுத்துங்கள்",
        },
    ),
    MethodSpec(
        code="MARX_OTHER",
        kind="online",
        icon="card",
        legacy_type="GATEWAY",
        gateway="marx",
        option="OTHER",
        names={"en": "Visa / Mastercard / UnionPay"},
        descriptions=_CARD,
    ),
    MethodSpec(
        code="MARX_AMEX",
        kind="online",
        icon="card",
        legacy_type="GATEWAY",
        gateway="marx",
        option="AMEX",
        names={"en": "Amex / Diners / Discover"},
        descriptions=_CARD,
    ),
    MethodSpec(
        code="MARX_BANK",
        kind="online",
        icon="bank",
        legacy_type="GATEWAY",
        gateway="marx",
        option="PAY_BY_BANK_ACCOUNT",
        names={"en": "Bank Transfer", "si": "බැංකු මාරුව", "ta": "வங்கி பரிமாற்றம்"},
        descriptions={
            "en": "Pay straight from your bank account",
            "si": "ඔබේ බැංකු ගිණුමෙන් කෙලින්ම ගෙවන්න",
            "ta": "உங்கள் வங்கிக் கணக்கிலிருந்து நேரடியாக செலுத்துங்கள்",
        },
    ),
)

_BY_CODE = {m.code: m for m in METHODS}


def get_method(code: Optional[str]) -> Optional[MethodSpec]:
    return _BY_CODE.get((code or "").strip().upper())


def is_online(code: Optional[str]) -> bool:
    method = get_method(code)
    return method is not None and method.is_online


def offered(lang: str) -> list[dict]:
    """``GET /payment-methods``: what checkout shows, in *lang*.

    The ``id``/``type``/``is_active`` fields are what apps before this release
    read; they stay so a customer who has not updated keeps a working checkout.
    """
    return [
        {
            "id": m.code.lower(),
            "code": m.code,
            "name": m.name(lang),
            "description": m.description(lang),
            "icon": m.icon,
            "kind": m.kind,
            "type": m.legacy_type,
            "is_active": True,
        }
        for m in METHODS
        if m.enabled()
    ]
