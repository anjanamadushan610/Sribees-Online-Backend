"""
Pricing Service - Single Authoritative Source for Order Totals

The cart (GET /cart), the checkout preview (POST /orders/quote) and order
creation (POST /orders) all compute their financial breakdown here, so the
number the customer previews is the number they are charged.

**The delivery fee is SribeesExpress's quote, and nothing else.** There used
to be a flat platform fee an administrator could set, used whenever the courier
could not be asked. It is gone (2026-09-13): a fee nobody priced is a fee the
merchant either over-charges or silently pays the difference on. When the
courier cannot price a parcel — no address yet, a postal city they do not
serve, the courier unreachable — the breakdown says so in ``delivery_status``,
carries no delivery fee, and checkout refuses to place the order.

All money is computed with ``Decimal`` and quantized to 2 places.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from loguru import logger

from app.config.settings import settings
from app.services import courier_credentials
from app.services.courier_service import (
    CourierError,
    CourierService,
    CourierUnavailableError,
)

_CENTS = Decimal("0.01")

#: Why a breakdown does or does not carry a delivery fee.
DELIVERY_QUOTED = "quoted"
DELIVERY_EMPTY_CART = "empty_cart"
DELIVERY_NO_ADDRESS = "no_address"
DELIVERY_NOT_SERVICEABLE = "not_serviceable"
DELIVERY_UNAVAILABLE = "unavailable"

#: The customer-facing sentence for each non-quoted state. Short enough for a
#: cart row; the mobile app shows it in place of the fee.
DELIVERY_MESSAGES = {
    DELIVERY_NO_ADDRESS: "Add a delivery address to see the delivery charge.",
    DELIVERY_NOT_SERVICEABLE: "We don't deliver to this postal city yet.",
    DELIVERY_UNAVAILABLE: "The delivery charge can't be calculated right now. Please try again shortly.",
}


def _money(value) -> Decimal:
    """Coerce to a 2dp Decimal (bankers-safe half-up rounding)."""
    return Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP)


def cart_weight_kg(items: List[dict]) -> Decimal:
    """The parcel weight a quote is priced on: each line's DB weight times its quantity."""
    return sum(
        (Decimal(str(i.get("weight_kg") or 0)) * Decimal(str(i["quantity"])) for i in items),
        Decimal("0"),
    )


@dataclass
class DeliveryQuote:
    """The delivery half of a price: a courier quote, or the reason there is none."""

    status: str
    fee: Optional[Decimal] = None
    quote_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    reason: Optional[str] = None
    original_fee: Optional[Decimal] = None
    promotion: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.status == DELIVERY_QUOTED

    @property
    def message(self) -> Optional[str]:
        return None if self.available else DELIVERY_MESSAGES.get(self.status)


@dataclass
class PriceBreakdown:
    """Authoritative financial breakdown for an order/quote."""
    subtotal: Decimal
    delivery_fee: Decimal
    discount: Decimal
    tax: Decimal
    wallet_deduction: Decimal
    cashback_earned: Decimal
    total: Decimal
    # quoted | empty_cart | no_address | not_serviceable | unavailable. Only
    # `quoted` (or an empty cart) may be checked out; see OrderService.
    delivery_status: str = DELIVERY_QUOTED
    # Set when delivery_fee came from a live courier quote, so booking can
    # reference the exact quote that produced it.
    courier_quote_id: Optional[str] = None
    courier_quote_expires_at: Optional[datetime] = None

    @property
    def delivery_available(self) -> bool:
        return self.delivery_status == DELIVERY_QUOTED

    def as_dict(self) -> Dict[str, Any]:
        """JSON-friendly float representation for API responses."""
        return {
            "subtotal": float(self.subtotal),
            "delivery_fee": float(self.delivery_fee),
            "discount": float(self.discount),
            "tax": float(self.tax),
            "wallet_deduction": float(self.wallet_deduction),
            "cashback_earned": float(self.cashback_earned),
            "total": float(self.total),
            "delivery_status": self.delivery_status,
            "delivery_available": self.delivery_available,
            "delivery_message": DELIVERY_MESSAGES.get(self.delivery_status),
            "courier_quote_id": self.courier_quote_id,
        }


class PricingService:
    """Computes order totals from cart items, coupon, wallet state and a courier quote."""

    @staticmethod
    async def quote_delivery(
        items: List[dict],
        *,
        postal_city_id: Optional[int],
        payment_method: str = "cod",
        cod_amount: Decimal = Decimal("0"),
        outlet_id: Optional[int] = None,
    ) -> DeliveryQuote:
        """
        Ask SribeesExpress what delivering this cart costs.

        Calls ``POST /ecommerce/rates`` (contract section 6) inside the caller's
        courier credential scope — the fulfilling branch's, which is what makes
        the quote priced from that branch. Never raises: every way a quote can
        fail to exist is a ``DeliveryQuote`` status the caller renders or
        refuses on.
        """
        if not items:
            return DeliveryQuote(status=DELIVERY_EMPTY_CART, fee=Decimal("0.00"))
        if postal_city_id is None:
            return DeliveryQuote(status=DELIVERY_NO_ADDRESS)
        if courier_credentials.active_target() is None:
            # No credential scope means nobody has entered a key for the branch
            # (optional_for_branch yielded nothing). To a shopper that is the
            # same as the courier being down, and it must not look like a price.
            return DeliveryQuote(status=DELIVERY_UNAVAILABLE, reason="courier not configured")

        try:
            quote = await CourierService.get_rate(
                postal_city_id=postal_city_id,
                weight_kg=cart_weight_kg(items),
                payment_method=payment_method,
                cod_amount=cod_amount,
                outlet_id=outlet_id,
            )
        except (CourierUnavailableError, CourierError) as exc:
            logger.warning(f"Courier /rates failed for postal_city_id={postal_city_id}: {exc}")
            return DeliveryQuote(status=DELIVERY_UNAVAILABLE, reason=str(exc)[:200])

        if not quote.serviceable:
            logger.info(f"Courier reports postal_city_id={postal_city_id} not serviceable: {quote.reason}")
            return DeliveryQuote(status=DELIVERY_NOT_SERVICEABLE, reason=quote.reason)

        return DeliveryQuote(
            status=DELIVERY_QUOTED,
            fee=_money(quote.amount_lkr),
            quote_id=quote.quote_id,
            expires_at=quote.expires_at,
        )

    @staticmethod
    def _discount(subtotal: Decimal, coupon: Optional[Dict[str, Any]]) -> Decimal:
        """
        Discount from an applied coupon (percentage or fixed), capped by
        max_discount_amount.

        The discount is computed against ``eligible_subtotal`` when the caller
        supplies one — the part of the cart the coupon is actually allowed to
        touch, after clearance lines and any category/product restriction have
        been removed (see ``CouponService.eligible_subtotal``). Without it a
        "10% off" on a cart that is mostly Quick Sale stock would discount the
        clearance items too, which is the double-discounting the exclusion
        exists to prevent. Falls back to the full subtotal so an older caller
        keeps the previous behaviour rather than silently discounting nothing.
        """
        if not coupon:
            return Decimal("0")
        eligible = coupon.get("eligible_subtotal")
        base = Decimal(str(eligible)) if eligible is not None else subtotal
        if base <= 0:
            return Decimal("0")
        discount_type = coupon.get("discount_type")
        value = Decimal(str(coupon.get("discount_value", 0)))
        if discount_type == "percentage":
            discount = base * (value / Decimal("100"))
        else:  # fixed
            # A fixed discount can never exceed the eligible goods either —
            # otherwise Rs 500 off would still come off a cart holding Rs 200
            # of eligible items.
            discount = min(value, base)
        # A percentage coupon has no natural ceiling — this is what stops a
        # "50% off" promo from handing out an unbounded discount on a large
        # cart. Admin-configured; None means uncapped.
        max_discount = coupon.get("max_discount_amount")
        if max_discount is not None:
            discount = min(discount, Decimal(str(max_discount)))
        return _money(discount)

    @staticmethod
    def _cashback(items: List[dict]) -> Decimal:
        """
        Sum the cashback each line earns at its own effective rate.

        ``cashback_percentage`` is the branch-resolved rate the caller attached
        (branch override -> product default -> platform rate). A line without
        one falls back to the platform rate, so an older caller that has not
        been updated keeps the previous behaviour rather than earning zero.
        """
        if not items:
            return Decimal("0.00")

        default_percent = Decimal(str(settings.cashback_rate)) * Decimal("100")
        total = Decimal("0")

        for item in items:
            rate = item.get("cashback_percentage")
            percent = default_percent if rate is None else Decimal(str(rate))
            line = Decimal(str(item["price"])) * Decimal(str(item["quantity"]))
            total += line * percent / Decimal("100")

        return _money(total)

    @staticmethod
    async def quote(
        items: List[dict],
        coupon: Optional[Dict[str, Any]] = None,
        use_wallet: bool = False,
        wallet_balance: Decimal = Decimal("0"),
        tax_rate: Optional[Decimal] = None,
        *,
        postal_city_id: Optional[int] = None,
        payment_method: str = "cod",
        wallet_amount: Optional[Decimal] = None,
        outlet_id: Optional[int] = None,
        delivery: Optional[DeliveryQuote] = None,
    ) -> PriceBreakdown:
        """
        Build the authoritative price breakdown.

        Args:
            items: Cart items (each with ``price`` and ``quantity``, and
                optionally ``cashback_percentage``/``weight_kg`` — the
                branch-resolved rate and DB weight for that product).
            coupon: Applied coupon dict (``discount_type``/``discount_value``).
            use_wallet: Whether to apply the wallet balance to the total.
            wallet_balance: Available wallet balance (ignored if not use_wallet).
            wallet_amount: Customer-chosen amount to deduct instead of the full
                balance (ignored if not use_wallet). The server clamps this to
                ``[0, min(wallet_balance, gross)]`` regardless of what the
                client sends.
            tax_rate: Tax rate as a fraction override (from platform settings).
                Falls back to the static config default when None.
            postal_city_id: The delivery address's SribeesExpress postal city
                id. None prices no delivery (``no_address``).
            payment_method: ``"cod"`` or ``"prepaid"`` — sent to SribeesExpress,
                currently inert for pricing.
            outlet_id: The SribeesExpress outlet the parcel leaves from, for a
                branch booking under the shared account key.
            delivery: A delivery quote the caller already holds (the cart's
                cached one). Skips the courier call when given.
        """
        effective_tax_rate = (
            tax_rate if tax_rate is not None else Decimal(str(settings.order_tax_rate))
        )

        subtotal = _money(
            sum(Decimal(str(i["price"])) * Decimal(str(i["quantity"])) for i in items)
        ) if items else Decimal("0.00")

        discount = PricingService._discount(subtotal, coupon)
        taxable = subtotal - discount
        tax = _money(taxable * effective_tax_rate)

        if delivery is None:
            # COD collects the full order total at the door. The gross is not
            # known until the delivery fee lands, so the courier is told the
            # pre-delivery payable -- it is inert for pricing today.
            delivery = await PricingService.quote_delivery(
                items,
                postal_city_id=postal_city_id,
                payment_method=payment_method,
                cod_amount=(taxable + tax) if payment_method == "cod" else Decimal("0"),
                outlet_id=outlet_id,
            )
        delivery_fee = delivery.fee if delivery.fee is not None else Decimal("0.00")

        gross = _money(taxable + tax + delivery_fee)

        cashback_earned = PricingService._cashback(items)

        wallet_deduction = Decimal("0")
        if use_wallet and wallet_balance and wallet_balance > 0:
            max_deduction = _money(min(_money(wallet_balance), gross))
            if wallet_amount is not None:
                wallet_deduction = _money(max(Decimal("0"), min(Decimal(str(wallet_amount)), max_deduction)))
            else:
                wallet_deduction = max_deduction

        total = _money(gross - wallet_deduction)

        return PriceBreakdown(
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            discount=discount,
            tax=tax,
            wallet_deduction=wallet_deduction,
            cashback_earned=cashback_earned,
            total=total,
            delivery_status=delivery.status,
            courier_quote_id=delivery.quote_id,
            courier_quote_expires_at=delivery.expires_at,
        )
