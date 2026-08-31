"""
Coupon Pydantic Schemas
"""
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

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
