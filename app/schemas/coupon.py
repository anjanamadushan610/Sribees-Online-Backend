"""
Coupon Pydantic Schemas
"""
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class CouponDiscountType(str, Enum):
    PERCENTAGE = "percentage"
    FIXED = "fixed"


def _validate_discount(discount_type: CouponDiscountType, value: Decimal) -> None:
    if value <= 0:
        raise ValueError("discount_value must be greater than 0")
    if discount_type == CouponDiscountType.PERCENTAGE and value > 100:
        raise ValueError("Percentage discount cannot exceed 100")


class CouponCreate(BaseModel):
    """Create a coupon."""
    code: str = Field(..., min_length=2, max_length=50)
    description: Optional[str] = None
    discount_type: CouponDiscountType = CouponDiscountType.PERCENTAGE
    discount_value: Decimal = Field(..., gt=0)
    min_order_value: Decimal = Field(default=Decimal("0"), ge=0)
    max_discount_amount: Optional[Decimal] = Field(default=None, ge=0)
    usage_limit: Optional[int] = Field(default=None, ge=1, description="NULL = unlimited")
    valid_from: datetime
    valid_until: datetime
    is_active: bool = True
    # Owning branch. Ignored for a branch-scoped creator (they are pinned to
    # their own branch); a Super Admin sending null creates a network-wide
    # coupon valid at every branch.
    branch_id: Optional[UUID] = None

    # ------------------------------------------------------------------
    # Financial protection
    # ------------------------------------------------------------------
    # usage_limit caps how many times a code is used; this caps what it costs.
    # NULL = uncapped.
    budget_cap: Optional[Decimal] = Field(default=None, gt=0)
    auto_stop_on_budget: bool = True
    # Clearance lines count toward neither the discount base nor min_order_value.
    exclude_quick_sale: bool = True

    # ------------------------------------------------------------------
    # Eligibility and targeting
    # ------------------------------------------------------------------
    first_order_only: bool = False
    per_user_limit: Optional[int] = Field(default=None, ge=1)
    is_public: bool = True
    # Empty/omitted = the entire catalog. Naming either narrows the coupon to
    # the union of the two.
    category_ids: Optional[list[UUID]] = None
    product_ids: Optional[list[UUID]] = None

    @field_validator("code")
    @classmethod
    def normalize_code(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def _check(self) -> "CouponCreate":
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        _validate_discount(self.discount_type, self.discount_value)
        if self.discount_type == CouponDiscountType.FIXED and self.min_order_value < self.discount_value:
            raise ValueError("min_order_value must be greater than or equal to discount_value for fixed discounts")
        # A fixed-discount campaign whose whole budget is smaller than one
        # redemption can never pay out even once. Catching it here saves a
        # manager launching a code that refuses every customer who tries it.
        if (
            self.budget_cap is not None
            and self.discount_type == CouponDiscountType.FIXED
            and self.budget_cap < self.discount_value
        ):
            raise ValueError(
                "Total campaign budget must be at least one discount "
                f"({self.discount_value}) or no customer can ever redeem it"
            )
        return self


class CouponUpdate(BaseModel):
    """Update a coupon — all fields optional (partial update)."""
    code: Optional[str] = Field(None, min_length=2, max_length=50)
    description: Optional[str] = None
    discount_type: Optional[CouponDiscountType] = None
    discount_value: Optional[Decimal] = Field(None, gt=0)
    min_order_value: Optional[Decimal] = Field(None, ge=0)
    max_discount_amount: Optional[Decimal] = Field(None, ge=0)
    usage_limit: Optional[int] = Field(None, ge=1)
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    is_active: Optional[bool] = None

    budget_cap: Optional[Decimal] = Field(default=None, gt=0)
    auto_stop_on_budget: Optional[bool] = None
    exclude_quick_sale: Optional[bool] = None
    first_order_only: Optional[bool] = None
    per_user_limit: Optional[int] = Field(default=None, ge=1)
    is_public: Optional[bool] = None
    # Sending either replaces that eligibility set entirely; omitting it leaves
    # the set alone. An explicit empty list widens the coupon back to the whole
    # catalog, which is the only way to undo a restriction.
    category_ids: Optional[list[UUID]] = None
    product_ids: Optional[list[UUID]] = None

    @field_validator("code")
    @classmethod
    def normalize_code(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().upper() if v is not None else v

    @model_validator(mode="after")
    def _check(self) -> "CouponUpdate":
        # Only validate the date order when both are supplied together; a
        # single-sided update is validated against the stored value in the router.
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("valid_until must be after valid_from")
        if self.discount_type is not None and self.discount_value is not None:
            _validate_discount(self.discount_type, self.discount_value)
        return self
