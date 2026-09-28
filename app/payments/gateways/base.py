"""
What every payment gateway must do, so the rest of the app never names one.

A hosted-page gateway takes the customer to its own page and brings them back
to our return URL. Four calls cover it:

  start   open a checkout for one try; returns where to send the customer
  parse_return  read OUR reference and ITS reference out of the return URL
  settle  after the return: ask the gateway what happened (and capture, if
          the gateway needs that step). Server to server -- the return URL
          went through the customer's browser and proves nothing.
  status  the same question later, for a try whose customer never came back

Adding or swapping a gateway is a new subclass registered in
``app/payments/gateways/__init__.py`` plus its methods in the catalog. The app
does not change: it opens ``redirect_url``, watches for ``return_url`` and
hands the URL it landed on back to us.
"""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


class Outcome(str, enum.Enum):
    PAID = "paid"
    FAILED = "failed"
    PENDING = "pending"


@dataclass(frozen=True)
class GatewayAnswer:
    outcome: Outcome
    code: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Checkout:
    redirect_url: str
    gateway_ref: Optional[str]


@dataclass(frozen=True)
class Payer:
    email: Optional[str]
    phone: Optional[str]
    name: Optional[str]


class GatewayError(Exception):
    """The gateway could not be reached or refused the call."""


class PayerDetailsMissing(ValueError):
    """The gateway needs something from the customer's profile first."""


class PaymentGateway(ABC):
    name: str

    @property
    @abstractmethod
    def return_url(self) -> str:
        """Where the gateway sends the customer back to. The app watches for it."""

    @abstractmethod
    async def start(
        self,
        *,
        reference: str,
        amount: Decimal,
        option: str,
        payer: Payer,
        description: str,
    ) -> Checkout: ...

    @abstractmethod
    def parse_return(self, params: dict) -> Optional[tuple[str, str]]:
        """(our reference, gateway reference) from the return URL's query, or None."""

    @abstractmethod
    async def settle(self, *, reference: str, gateway_ref: str) -> GatewayAnswer: ...

    @abstractmethod
    async def status(self, *, reference: str, gateway_ref: str) -> GatewayAnswer: ...
