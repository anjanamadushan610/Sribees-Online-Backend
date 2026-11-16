"""
The money arithmetic of a per-product commission. Pure: no database, no I/O.

Everything is ``Decimal``. A float cannot hold Rs 0.10 exactly, and a ledger
that is off by a cent is a ledger nobody can reconcile.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
    rate: Decimal             # product commission pool percentage snapshot (0..100)

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
    pool_rate: Optional[Decimal] = None
    master_share: Optional[Decimal] = None


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


def build_shared_lines(
    lines: Iterable[SaleLine], master_share: Decimal,
) -> tuple[list[CommissionLine], list[CommissionLine]]:
    """Allocate each rounded product pool between its direct referrer and upline."""
    from app.services.referral_commission.policy import LevelPolicy

    master_share = Decimal(str(master_share))
    policy = LevelPolicy(master_share)
    direct, master = [], []
    for pool in build_lines(lines, lambda rate: rate):
        master_amount = money(pool.amount * master_share)
        master_rate = policy.rate(2, pool.rate)
        direct_amount = pool.amount - master_amount
        for destination, rate, amount in (
            (direct, pool.rate - master_rate, direct_amount),
            (master, master_rate, master_amount),
        ):
            if amount > 0:
                destination.append(replace(
                    pool, rate=rate, amount=amount,
                    pool_rate=pool.rate, master_share=master_share,
                ))
    return direct, master


def kept_shared_amount(
    basis: Decimal, pool_rate: Decimal, master_share: Decimal,
    level: int, quantity: int, returned_quantity: int,
) -> Decimal:
    """Reallocate the remaining pool with the original snapshotted split."""
    if level not in (1, 2):
        raise ValueError(f"No referral level {level}")
    master_share = Decimal(str(master_share))
    if not master_share.is_finite() or not Decimal("0") <= master_share <= Decimal("1"):
        raise ValueError("Master share must be between 0 and 1")
    pool = kept_amount(basis, pool_rate, quantity, returned_quantity)
    master = money(pool * master_share)
    return pool - master if level == 1 else master
