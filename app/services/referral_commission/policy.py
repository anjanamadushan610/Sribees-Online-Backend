"""What each referral level earns from a product's level-1 rate."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Rates are stored to three places: half of 2.25% is 1.125%, and rounding
# that to 1.13% would pay the upline a little more than the rule says.
RATE_PLACES = Decimal("0.001")


@dataclass(frozen=True)
class LevelPolicy:
    """
    Level 1 earns the product's rate; level 2 earns ``level2_share`` of it.

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
            return (base * Decimal(str(self.level2_share))).quantize(
                RATE_PLACES, rounding=ROUND_HALF_UP
            )
        # Two levels, fixed. An unbounded chain is a pyramid, not a referral
        # programme -- see ReferralService.accrue_for_order.
        raise ValueError(f"No referral level {level}")

    def for_level(self, level: int):
        """``rate_for`` callable for :func:`build_lines`."""
        return lambda level1_rate: self.rate(level, level1_rate)
