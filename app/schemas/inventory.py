"""
Admin Inventory Schemas

A ``branch_inventory`` row is the Branch-Override side of the Global-Catalog
pattern. Nullable override fields (``branch_price``, ``discount_percentage``)
follow one rule throughout: **NULL means "fall back to the global product"**,
so clearing an override and never setting one are the same state.
"""
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class InventoryUpdateRequest(BaseModel):
    """
    Partial update for an existing branch_inventory row.

    Only the fields present in the request body are changed. A present key with
    a ``null`` value is a meaningful instruction here (clear the override), so
    routes must pass ``model_dump(exclude_unset=True)`` — ``exclude_none``
    would silently drop the "revert to the global price" case.
    """

    branch_price: Optional[Decimal] = Field(
        None,
        ge=0,
        description="Local price for this branch. Send null to clear the override "
                    "and fall back to the global product price.",
    )
    stock_quantity: Optional[int] = Field(None, ge=0, description="Units on hand")
    reserved_quantity: Optional[int] = Field(None, ge=0, description="Units reserved by orders")
    low_stock_threshold: Optional[int] = Field(None, ge=0, description="Low-stock alert threshold")
    discount_percentage: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="Local discount %. Null clears the override.",
    )
    is_on_sale: Optional[bool] = Field(None, description="Quick Sale flag for this branch")
    is_active: Optional[bool] = Field(
        None,
        description="False hides the product in this branch even if globally active",
    )

    @model_validator(mode="after")
    def _at_least_one(self) -> "InventoryUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")
        return self


class BranchOverrideCreate(BaseModel):
    """
    Stock a global-catalog product in a branch, with optional local overrides.

    Creating this row is what makes the product visible to customers in the
    branch — the public listing joins on branch_inventory, so a product with no
    row here does not exist for that branch's shoppers.
    """

    product_id: UUID
    branch_id: Optional[UUID] = Field(
        None,
        description="Super Admin only. Scoped admins always write to their own "
                    "branch; this field is ignored for them.",
    )
    branch_price: Optional[Decimal] = Field(
        None,
        ge=0,
        description="Local price. Omit to inherit the global product price.",
    )
    stock_quantity: int = Field(0, ge=0)
    low_stock_threshold: int = Field(10, ge=0)
    discount_percentage: Optional[float] = Field(None, ge=0, le=100)
    is_on_sale: bool = False
    is_active: bool = True
