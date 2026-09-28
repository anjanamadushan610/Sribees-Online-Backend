"""
Every way a customer can pay, defined once, on the server.

The app draws checkout from ``GET /payment-methods`` and knows nothing else:
the name and the line under it come from here -- in English whatever the
app's language, as the owner wants (2026-09-28) -- the icon is one of a few
generic shapes (``cash``, ``wallet``, ``card``, ``bank``,
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


@dataclass(frozen=True)
class MethodSpec:
    code: str
    kind: str
    icon: str
    name: str
    description: str
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


# A gateway is offered only while it is switched on.
_GATEWAY_SWITCHES = {
    "marx": lambda: settings.marx_gateway_methods_enabled,
}

_CARD = "Pay securely by debit or credit card"

METHODS: tuple[MethodSpec, ...] = (
    MethodSpec(
        code="COD",
        kind="offline",
        icon="cash",
        legacy_type="OFFLINE",
        name="Cash on Delivery",
        description="Pay the rider in cash when your order arrives",
    ),
    MethodSpec(
        code="WALLET",
        kind="wallet",
        icon="wallet",
        legacy_type="WALLET",
        name="SRIBEES Wallet",
        description="Pay from your wallet balance",
    ),
    MethodSpec(
        code="MARX_OTHER",
        kind="online",
        icon="card",
        legacy_type="GATEWAY",
        gateway="marx",
        option="OTHER",
        name="Visa / Mastercard / UnionPay",
        description=_CARD,
    ),
    MethodSpec(
        code="MARX_AMEX",
        kind="online",
        icon="card",
        legacy_type="GATEWAY",
        gateway="marx",
        option="AMEX",
        name="Amex / Diners / Discover",
        description=_CARD,
    ),
    MethodSpec(
        code="MARX_BANK",
        kind="online",
        icon="bank",
        legacy_type="GATEWAY",
        gateway="marx",
        option="PAY_BY_BANK_ACCOUNT",
        name="Bank Transfer",
        description="Pay straight from your bank account",
    ),
)

_BY_CODE = {m.code: m for m in METHODS}


def get_method(code: Optional[str]) -> Optional[MethodSpec]:
    return _BY_CODE.get((code or "").strip().upper())


def is_online(code: Optional[str]) -> bool:
    method = get_method(code)
    return method is not None and method.is_online


def offered() -> list[dict]:
    """``GET /payment-methods``: what checkout shows.

    The ``id``/``type``/``is_active`` fields are what apps before this release
    read; they stay so a customer who has not updated keeps a working checkout.
    """
    return [
        {
            "id": m.code.lower(),
            "code": m.code,
            "name": m.name,
            "description": m.description,
            "icon": m.icon,
            "kind": m.kind,
            "type": m.legacy_type,
            "is_active": True,
        }
        for m in METHODS
        if m.enabled()
    ]
