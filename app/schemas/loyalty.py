"""
Loyalty level schemas (Super Admin).
"""
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

_HEX = r"^#[0-9A-Fa-f]{6}$"


class _TierRules(BaseModel):
    @model_validator(mode="after")
    def _reward_is_coherent(self):
        kind = getattr(self, "reward_discount_type", None)
        if kind is None:
            return self
        value = self.reward_discount_value
        if value is None or self.reward_valid_days is None:
            raise ValueError("A level reward needs a discount value and how many days it lasts")
        if kind == "percentage" and value > 100:
            raise ValueError("A percentage reward cannot exceed 100")
        # The Assign Promo rule: a fixed discount bigger than the order it
        # applies to would hand out free money.
        if kind == "fixed" and (self.reward_min_order_value or 0) < value:
            raise ValueError("Minimum order must be at least the fixed discount amount")
        return self


class LoyaltyTierCreate(_TierRules):
    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1, max_length=50)
    name_si: Optional[str] = Field(None, max_length=50)
    name_ta: Optional[str] = Field(None, max_length=50)
    min_points: int = Field(..., ge=0, le=10_000_000)
    color: Optional[str] = Field(None, pattern=_HEX)
    is_active: bool = True

    reward_discount_type: Optional[Literal["percentage", "fixed"]] = None
    reward_discount_value: Optional[Decimal] = Field(None, gt=0, le=1_000_000)
    reward_min_order_value: Decimal = Field(Decimal("0"), ge=0, le=10_000_000)
    reward_max_discount: Optional[Decimal] = Field(None, gt=0, le=1_000_000)
    reward_valid_days: Optional[int] = Field(None, ge=1, le=365)


class LoyaltyTierUpdate(BaseModel):
    """
    Partial update. The reward rules are checked on the level as it will be
    after the change (the router re-validates the merged result as a
    LoyaltyTierCreate), since half a reward is only wrong in context.
    """

    model_config = {"extra": "forbid"}

    name: Optional[str] = Field(None, min_length=1, max_length=50)
    name_si: Optional[str] = Field(None, max_length=50)
    name_ta: Optional[str] = Field(None, max_length=50)
    min_points: Optional[int] = Field(None, ge=0, le=10_000_000)
    color: Optional[str] = Field(None, pattern=_HEX)
    is_active: Optional[bool] = None

    reward_discount_type: Optional[Literal["percentage", "fixed"]] = None
    reward_discount_value: Optional[Decimal] = Field(None, gt=0, le=1_000_000)
    reward_min_order_value: Optional[Decimal] = Field(None, ge=0, le=10_000_000)
    reward_max_discount: Optional[Decimal] = Field(None, gt=0, le=1_000_000)
    reward_valid_days: Optional[int] = Field(None, ge=1, le=365)
