"""
Pricing Service - Single Authoritative Source for Order Totals

Both the order-quote preview (POST /orders/quote) and the actual order
creation (POST /orders) compute their financial breakdown here, so the number
the customer previews is guaranteed to equal the number they are charged.

All money is computed with ``Decimal`` and quantized to 2 places.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from app.config.settings import settings

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

    def as_dict(self) -> Dict[str, float]:
        """JSON-friendly float representation for API responses."""
        return {
            "subtotal": float(self.subtotal),
            "delivery_fee": float(self.delivery_fee),
            "discount": float(self.discount),
            "tax": float(self.tax),
            "wallet_deduction": float(self.wallet_deduction),
            "cashback_earned": float(self.cashback_earned),
            "total": float(self.total),
        }


class PricingService:
    """Computes order totals from cart items, coupon and wallet state."""

    @staticmethod
    def _delivery_fee(items: List[dict], subtotal: Decimal, flat_fee: Decimal) -> Decimal:
        """
        Resolve the delivery fee.

        Currently a flat fee for any non-empty cart. Kept as its own method so
        a future dynamic model (per-branch, weight, distance, free-over-X) can
        drop in here without touching the rest of the pricing pipeline.
        """
        if not items:
            return Decimal("0")
        return _money(flat_fee)

    @staticmethod
    def _discount(subtotal: Decimal, coupon: Optional[Dict[str, Any]]) -> Decimal:
        """Discount from an applied coupon (percentage or fixed)."""
        if not coupon:
            return Decimal("0")
        discount_type = coupon.get("discount_type")
        value = Decimal(str(coupon.get("discount_value", 0)))
        if discount_type == "percentage":
            discount = subtotal * (value / Decimal("100"))
        else:  # fixed
            discount = min(value, subtotal)
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
    def quote(
        items: List[dict],
        coupon: Optional[Dict[str, Any]] = None,
        use_wallet: bool = False,
        wallet_balance: Decimal = Decimal("0"),
        delivery_fee: Optional[Decimal] = None,
        tax_rate: Optional[Decimal] = None,
    ) -> PriceBreakdown:
        """
        Build the authoritative price breakdown.

        Args:
            items: Cart items (each with ``price`` and ``quantity``, and
                optionally ``cashback_percentage`` — the branch-resolved rate
                for that product).
            coupon: Applied coupon dict (``discount_type``/``discount_value``).
            use_wallet: Whether to apply the wallet balance to the total.
            wallet_balance: Available wallet balance (ignored if not use_wallet).
            delivery_fee: Flat delivery fee override (from platform settings).
                Falls back to the static config default when None.
            tax_rate: Tax rate as a fraction override (from platform settings).
                Falls back to the static config default when None.
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
        delivery_fee = PricingService._delivery_fee(items, subtotal, effective_fee)

        # Gross payable before any wallet balance is applied.
        gross = _money(taxable + tax + delivery_fee)

        # Cashback is earned per line at that product's own rate, then summed —
        # a 25% item and a 5% item in the same cart must not be averaged into
        # one blended rate. Independent of the wallet, as before.
        cashback_earned = PricingService._cashback(items)

        wallet_deduction = Decimal("0")
        if use_wallet and wallet_balance and wallet_balance > 0:
            wallet_deduction = _money(min(_money(wallet_balance), gross))

        total = _money(gross - wallet_deduction)

        return PriceBreakdown(
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            discount=discount,
            tax=tax,
            wallet_deduction=wallet_deduction,
            cashback_earned=cashback_earned,
            total=total,
        )
