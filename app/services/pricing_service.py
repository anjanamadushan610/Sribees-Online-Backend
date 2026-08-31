"""
Pricing Service - Single Authoritative Source for Order Totals

Both the order-quote preview (POST /orders/quote) and the actual order
creation (POST /orders) compute their financial breakdown here, so the number
the customer previews is guaranteed to equal the number they are charged.

All money is computed with ``Decimal`` and quantized to 2 places.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.config.settings import settings
from app.services.courier_service import CourierService, CourierUnavailableError

_CENTS = Decimal("0.01")


def _money(value) -> Decimal:
    """Coerce to a 2dp Decimal (bankers-safe half-up rounding)."""
    return Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP)


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
    # Set when delivery_fee came from a live courier quote (see
    # CourierService), so booking can reference the exact quote that produced
    # it. None on the flat-fee fallback path.
    courier_quote_id: Optional[str] = None
    courier_quote_expires_at: Optional[datetime] = None

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
            "courier_quote_id": self.courier_quote_id,
        }


class PricingService:
    """Computes order totals from cart items, coupon and wallet state."""

    @staticmethod
    async def _delivery_fee(
        items: List[dict],
        subtotal: Decimal,
        flat_fee: Decimal,
        *,
        origin: Optional[dict] = None,
        destination: Optional[dict] = None,
        payment_method: str = "cod",
    ) -> Tuple[Decimal, Optional[str], Optional[datetime]]:
        """
        Resolve the delivery fee: (fee, courier_quote_id, courier_quote_expires_at).

        Calls the courier's ``POST /v1/rates`` (docs/COURIER_API.md section 4)
        when both an ``origin`` (fulfilling branch) and a ``destination``
        (delivery address) are resolvable. Falls back to the flat platform fee
        — with no ``courier_quote_id`` — when: the cart is empty, there is no
        origin/destination yet (e.g. the very first quote call before an
        address is chosen), the courier is unreachable
        (``CourierUnavailableError``, after its own one retry), or the
        destination reports ``serviceable: false``. This fallback is required
        by the contract itself (section 8): "we will not block a sale on a
        courier outage."
        """
        if not items:
            return Decimal("0"), None, None
        if origin is None or destination is None:
            return _money(flat_fee), None, None

        weight_kg = sum(
            Decimal(str(i.get("weight_kg") or 0)) * Decimal(str(i["quantity"])) for i in items
        )
        item_count = sum(i["quantity"] for i in items)

        try:
            quote = await CourierService.get_rate(
                origin=origin,
                destination=destination,
                weight_kg=weight_kg,
                item_count=item_count,
                order_value_lkr=subtotal,
                payment_method=payment_method,
            )
        except CourierUnavailableError as exc:
            logger.warning(f"Courier /v1/rates unavailable, falling back to flat fee: {exc}")
            return _money(flat_fee), None, None

        if not quote.serviceable:
            # A place the courier does not deliver to is a normal answer, not
            # an error (section 4) -- we still owe *some* fee today, so fall
            # back to the flat fee rather than blocking checkout.
            logger.warning(f"Courier reports destination not serviceable: {quote.reason}")
            return _money(flat_fee), None, None

        return _money(quote.amount_lkr), quote.quote_id, quote.expires_at

    @staticmethod
    def _discount(subtotal: Decimal, coupon: Optional[Dict[str, Any]]) -> Decimal:
        """Discount from an applied coupon (percentage or fixed), capped by max_discount_amount."""
        if not coupon:
            return Decimal("0")
        discount_type = coupon.get("discount_type")
        value = Decimal(str(coupon.get("discount_value", 0)))
        if discount_type == "percentage":
            discount = subtotal * (value / Decimal("100"))
        else:  # fixed
            discount = min(value, subtotal)
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
        delivery_fee: Optional[Decimal] = None,
        tax_rate: Optional[Decimal] = None,
        *,
        origin: Optional[dict] = None,
        destination: Optional[dict] = None,
        payment_method: str = "cod",
        wallet_amount: Optional[Decimal] = None,
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
                client sends — a negative or over-balance value never reaches
                the order, closing the gap a tampered request could exploit.
            delivery_fee: Flat delivery fee override (from platform settings),
                used as the fallback when the courier is unavailable or no
                origin/destination is resolvable yet. Falls back to the static
                config default when None.
            tax_rate: Tax rate as a fraction override (from platform settings).
                Falls back to the static config default when None.
            origin: Fulfilling branch, formatted via
                ``branch_service.to_courier_origin`` (None skips the courier
                call, e.g. no delivery address chosen yet).
            destination: Delivery address, formatted per
                docs/COURIER_API.md section 4 (None skips the courier call).
            payment_method: ``"cod"`` or ``"prepaid"`` — priced into the
                courier's COD-collection fee, if any.
        """
        # Dynamic platform settings win; otherwise use the static config default.
        effective_fee = (
            delivery_fee if delivery_fee is not None else Decimal(str(settings.flat_delivery_fee))
        )
        effective_tax_rate = (
            tax_rate if tax_rate is not None else Decimal(str(settings.order_tax_rate))
        )

        subtotal = _money(
            sum(Decimal(str(i["price"])) * Decimal(str(i["quantity"])) for i in items)
        ) if items else Decimal("0.00")

        discount = PricingService._discount(subtotal, coupon)
        taxable = subtotal - discount
        tax = _money(taxable * effective_tax_rate)
        delivery_fee, courier_quote_id, courier_quote_expires_at = await PricingService._delivery_fee(
            items,
            subtotal,
            effective_fee,
            origin=origin,
            destination=destination,
            payment_method=payment_method,
        )

        # Gross payable before any wallet balance is applied.
        gross = _money(taxable + tax + delivery_fee)

        # Cashback is earned per line at that product's own rate, then summed —
        # a 25% item and a 5% item in the same cart must not be averaged into
        # one blended rate. Independent of the wallet, as before.
        cashback_earned = PricingService._cashback(items)

        wallet_deduction = Decimal("0")
        if use_wallet and wallet_balance and wallet_balance > 0:
            max_deduction = _money(min(_money(wallet_balance), gross))
            if wallet_amount is not None:
                # Clamp, never trust: a client-supplied amount outside
                # [0, max_deduction] is capped rather than rejected, so a
                # stale quote (balance dropped since the field was typed)
                # still produces a sane, safe order instead of an error.
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
            courier_quote_id=courier_quote_id,
            courier_quote_expires_at=courier_quote_expires_at,
        )
