"""Rates used when allocating a product commission pool."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Rates are stored to three places: half of 2.25% is 1.125%, and rounding
# that to 1.13% would pay the upline a little more than the rule says.
RATE_PLACES = Decimal("0.001")
CUSTOMER_REFERRAL_SHARE = Decimal("0.5")


@dataclass(frozen=True)
class LevelPolicy:
    """
    The product rate defines the total pool. ``level2_share`` is the
    Master Partner's fraction of the direct Partner's commission. For example,
    a 25% setting means the Partner gets 80% of the pool and the Master gets
    20% of the pool, which is 25% of the Partner's amount.

    One field per product, by the owner's decision: a Branch Manager sets a
    single number and the upline's cut follows from it. A product at 0% is
    therefore 0% for both levels -- the point of the field is that some goods
    carry no commission for anyone.
    """

    level2_share: Decimal

    def __post_init__(self):
        if not (Decimal("0") <= Decimal(str(self.level2_share)) <= Decimal("1")):
            raise ValueError("level2_share must be between 0 and 1")

    def rate(self, level: int, level1_rate: Decimal) -> Decimal:
        base = Decimal(str(level1_rate))
        if level == 1:
            return base
        if level == 2:
            partner_relative_share = Decimal(str(self.level2_share))
            pool_share = partner_relative_share / (Decimal("1") + partner_relative_share)
            return (base * pool_share).quantize(
                RATE_PLACES, rounding=ROUND_HALF_UP
            )
        # Two levels, fixed. An unbounded chain is a pyramid, not a referral
        # programme -- see ReferralService.accrue_for_order.
        raise ValueError(f"No referral level {level}")

    def for_level(self, level: int):
        """``rate_for`` callable for :func:`build_lines`."""
        return lambda level1_rate: self.rate(level, level1_rate)

    @staticmethod
    def for_customer():
        """Customers earn half of a product's advertised referral pool."""
        def customer_rate(product_rate: Decimal) -> Decimal:
            return (Decimal(str(product_rate)) * CUSTOMER_REFERRAL_SHARE).quantize(
                RATE_PLACES, rounding=ROUND_HALF_UP
            )

        return customer_rate
