"""
The money arithmetic of a per-product commission. Pure: no database, no I/O.

Everything is ``Decimal``. A float cannot hold Rs 0.10 exactly, and a ledger
that is off by a cent is a ledger nobody can reconcile.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Callable, Iterable, Optional, Sequence
from uuid import UUID

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal("100")


def money(value) -> Decimal:
    """Round to the cent, half up -- the one rounding rule of the ledger."""
    return Decimal(str(value if value is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class SaleLine:
    """One order line as the commission sees it, frozen at checkout."""

    order_item_id: Optional[UUID]
    product_id: UUID
    product_name: str
    quantity: int
    gross: Decimal            # unit_price x quantity
    coupon_discount: Decimal  # this line's share of the order's coupon
    rate: Decimal             # level-1 percentage snapshot (0..100)

    @property
    def basis(self) -> Decimal:
        """What the customer actually paid for this line's goods."""
        return max(money(self.gross) - money(self.coupon_discount), ZERO)


@dataclass(frozen=True)
class CommissionLine:
    """What one order line earned one beneficiary at one level."""

    order_item_id: Optional[UUID]
    product_id: UUID
    product_name: str
    quantity: int
    basis: Decimal
    rate: Decimal
    amount: Decimal


def allocate_discount(
    gross: Sequence[Decimal],
    eligible: Sequence[bool],
    discount: Decimal,
) -> list[Decimal]:
    """
    Share an order-level discount out over its lines, to the cent.

    Only ``eligible`` lines take a share (a coupon restricted to some
    products discounted only those), in proportion to their value. Shares are
    rounded down first and the leftover cents go to the largest remainders
    (ties to the earlier line), so the shares always sum to exactly the
    discount -- a proportional split rounded line by line can drift by a cent
    either way, and that cent would then exist in no one's books.

    The discount is clipped to what the eligible lines are worth: a share can
    never make a line negative.
    """
    if len(gross) != len(eligible):
        raise ValueError("gross and eligible must be the same length")

    values = [money(g) if ok else ZERO for g, ok in zip(gross, eligible, strict=True)]
    pool = sum(values, ZERO)
    target = min(max(money(discount), ZERO), pool)
    if target <= 0 or pool <= 0:
        return [ZERO for _ in gross]

    raw = [v * target / pool for v in values]
    shares = [r.quantize(CENT, rounding=ROUND_DOWN) for r in raw]
    leftover = int(((target - sum(shares, ZERO)) / CENT).to_integral_value())

    by_remainder = sorted(
        (i for i, v in enumerate(values) if v > 0),
        key=lambda i: (-(raw[i] - shares[i]), i),
    )
    for i in by_remainder[:leftover]:
        shares[i] += CENT
    return shares


def build_lines(
    lines: Iterable[SaleLine],
    rate_for: Callable[[Decimal], Decimal],
) -> list[CommissionLine]:
    """
    What each line earns at one level.

    ``rate_for`` maps the line's level-1 rate to the rate this level earns
    (see :class:`LevelPolicy`). A line that earns nothing is left out: a
    zero row says nothing an absent row does not, and the ledger stays the
    list of things somebody was actually paid for.
    """
    built = []
    for line in lines:
        if line.quantity <= 0:
            continue
        rate = Decimal(str(rate_for(Decimal(str(line.rate)))))
        basis = line.basis
        amount = money(basis * rate / HUNDRED)
        if amount <= 0:
            continue
        built.append(
            CommissionLine(
                order_item_id=line.order_item_id,
                product_id=line.product_id,
                product_name=line.product_name,
                quantity=line.quantity,
                basis=basis,
                rate=rate,
                amount=amount,
            )
        )
    return built


def kept_amount(basis: Decimal, rate: Decimal, quantity: int, returned_quantity: int) -> Decimal:
    """
    What a line still earns after ``returned_quantity`` of its units came back.

    Recomputed from the stored basis and rate rather than subtracted from the
    stored amount, so a second look at the same return lands on the same
    number instead of rounding a little further each time.
    """
    if quantity <= 0:
        return ZERO
    kept = max(quantity - max(returned_quantity, 0), 0)
    if kept == 0:
        return ZERO
    return money(Decimal(str(basis)) * kept / quantity * Decimal(str(rate)) / HUNDRED)
