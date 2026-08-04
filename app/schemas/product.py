"""
Product Pydantic Schemas
"""
from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field

# ============================================================================
# Nested Schemas
# ============================================================================

class ProductImageSchema(BaseModel):
    """Schema for product image."""
    image_id: UUID
    image_url: str
    alt_text: Optional[str] = None
    is_primary: bool = False
    sort_order: int = 0

    model_config = {"from_attributes": True}


class ProductCategorySchema(BaseModel):
    """Embedded category info in product."""
    category_id: UUID
    name: str
    slug: str
    image_url: Optional[str] = None

    model_config = {"from_attributes": True}


class VariantTypeSchema(BaseModel):
    """Schema for variant type."""
    variant_type_id: UUID
    name: str
    display_name: str
    options: List[str] = []

    model_config = {"from_attributes": True}


class ProductVariantSchema(BaseModel):
    """Schema for product variant."""
    variant_id: UUID
    name: str
    sku: Optional[str] = None
    price: Decimal
    compare_at_price: Optional[Decimal] = None
    stock_quantity: int = 0
    image_url: Optional[str] = None
    is_active: bool = True

    model_config = {"from_attributes": True}


# ============================================================================
# Request Schemas
# ============================================================================

class ProductCreate(BaseModel):
    """Schema for creating a product."""
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    short_description: Optional[str] = Field(None, max_length=500)
    sku: Optional[str] = Field(None, max_length=100)
    price: Decimal = Field(..., ge=0)
    compare_at_price: Optional[Decimal] = Field(None, ge=0)
    cost_price: Optional[Decimal] = Field(None, ge=0)
    category_id: Optional[UUID] = None
    subcategory_id: Optional[UUID] = Field(
        None,
        description="Sub-category — must be a child of category_id",
    )
    stock_quantity: int = Field(0, ge=0)
    low_stock_threshold: int = Field(10, ge=0)
    weight: Optional[Decimal] = None
    weight_unit: str = "kg"
    is_active: bool = True
    is_featured: bool = False
    meta_title: Optional[str] = Field(None, max_length=255)
    meta_description: Optional[str] = None


class ProductUpdate(BaseModel):
    """Schema for updating a product."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    slug: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    short_description: Optional[str] = Field(None, max_length=500)
    sku: Optional[str] = Field(None, max_length=100)
    price: Optional[Decimal] = Field(None, ge=0)
    compare_at_price: Optional[Decimal] = Field(None, ge=0)
    cost_price: Optional[Decimal] = Field(None, ge=0)
    category_id: Optional[UUID] = None
    subcategory_id: Optional[UUID] = Field(
        None,
        description="Sub-category — must be a child of category_id",
    )
    stock_quantity: Optional[int] = Field(None, ge=0)
    low_stock_threshold: Optional[int] = Field(None, ge=0)
    weight: Optional[Decimal] = None
    weight_unit: Optional[str] = None
    is_active: Optional[bool] = None
    is_featured: Optional[bool] = None
    meta_title: Optional[str] = Field(None, max_length=255)
    meta_description: Optional[str] = None


class ProductImageCreate(BaseModel):
    """Schema for adding a product image."""
    image_url: str
    alt_text: Optional[str] = None
    is_primary: bool = False
    sort_order: int = 0


class ProductVariantCreate(BaseModel):
    """Schema for creating a variant."""
    name: str = Field(..., min_length=1, max_length=100)
    sku: Optional[str] = Field(None, max_length=100)
    price: Decimal = Field(..., ge=0)
    compare_at_price: Optional[Decimal] = Field(None, ge=0)
    stock_quantity: int = Field(0, ge=0)
    image_url: Optional[str] = None
    is_active: bool = True
    sort_order: int = 0


# ============================================================================
# Response Schemas
# ============================================================================

class ProductBase(BaseModel):
    """Base product response schema."""
    product_id: UUID
    name: str
    slug: str
    description: Optional[str] = None
    short_description: Optional[str] = None
    sku: Optional[str] = None
    price: Decimal
    compare_at_price: Optional[Decimal] = None
    stock_quantity: int
    is_active: bool
    is_featured: bool
    view_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ProductResponse(ProductBase):
    """Full product response with related data."""
    _id: Optional[str] = None  # Mobile app compatibility
    category: Optional[ProductCategorySchema] = None
    images: List[ProductImageSchema] = []
    has_variants: bool = False
    variants: Optional[List[ProductVariantSchema]] = None
    variant_types: Optional[List[VariantTypeSchema]] = None

    @property
    def _id(self) -> str:
        return str(self.product_id)


class ProductListItem(BaseModel):
    """Simplified product for list views."""
    product_id: UUID
    _id: Optional[str] = None
    name: str
    slug: str
    price: Decimal
    compare_at_price: Optional[Decimal] = None
    stock_quantity: int
    is_active: bool
    is_featured: bool
    category: Optional[ProductCategorySchema] = None
    images: List[ProductImageSchema] = []

    model_config = {"from_attributes": True}


class ProductsListResponse(BaseModel):
    """Response for paginated product list."""
    success: bool = True
    data: dict


class ProductDetailResponse(BaseModel):
    """Response for single product."""
    success: bool = True
    data: dict


class ProductCreateResponse(BaseModel):
    """Response after creating a product."""
    success: bool = True
    data: dict
    message: str = "Product created successfully"


class ProductUpdateResponse(BaseModel):
    """Response after updating a product."""
    success: bool = True
    data: dict
    message: str = "Product updated successfully"


class ProductDeleteResponse(BaseModel):
    """Response after deleting a product."""
    success: bool = True
    message: str = "Product deleted successfully"


# ============================================================================
# Search & Filter Schemas
# ============================================================================

class ProductSearchParams(BaseModel):
    """Parameters for product search/filter."""
    search: Optional[str] = None
    category_id: Optional[UUID] = None
    min_price: Optional[Decimal] = None
    max_price: Optional[Decimal] = None
    is_featured: Optional[bool] = None
    in_stock: Optional[bool] = None
    sort_by: str = "created_at"
    sort_order: str = "desc"
    page: int = 1
    limit: int = 20


# ============================================================================
# Marketing / Quick-Sale Schemas
# ============================================================================

class DiscountUpdateRequest(BaseModel):
    """
    Request body for Marketing Manager to set / remove a discount.

    - Set ``discount_percentage`` to apply a discount and auto-compute
      ``discount_price``.
    - Set ``is_on_sale`` to control whether the product appears in the
      Quick Sale home-page feed.
    - Send ``discount_percentage: null, is_on_sale: false`` to remove
      a product from the Quick Sale feed.
    """
    discount_percentage: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="Discount percentage (0-100). Set to null to clear.",
    )
    is_on_sale: Optional[bool] = Field(
        None,
        description="Whether this product appears in the Quick Sale feed.",
    )


class DiscountUpdateResponse(BaseModel):
    """Response after a discount update."""
    success: bool = True
    data: dict
    message: str


class QuickSaleProductItem(BaseModel):
    """Slim product representation for the Quick Sale home feed."""
    product_id: UUID
    name: str
    slug: str
    price: Decimal
    discount_percentage: Optional[float] = None
    discount_price: Optional[Decimal] = None
    branch_id: Optional[UUID] = None
    stock_quantity: int
    image_url: Optional[str] = None
    category_name: Optional[str] = None

    model_config = {"from_attributes": True}


class HomeFeedResponse(BaseModel):
    """Response for the home-page Quick Sale feed."""
    success: bool = True
    data: dict


# ============================================================================
# Branch Inventory Schemas  (Global Catalog + Branch Overrides)
# ============================================================================

class BranchInventoryUpdateRequest(BaseModel):
    """
    Marketing Manager request to update branch-specific overrides.

    Any field set to ``null`` (or omitted) means "fall back to the
    global value on the product."
    """
    branch_price: Optional[Decimal] = Field(
        None,
        ge=0,
        description="Override price for this branch. Set null to use global price.",
    )
    discount_percentage: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="Override discount %. Set null to use global discount.",
    )
    is_on_sale: Optional[bool] = Field(
        None,
        description="Whether this product appears in Quick Sale for this branch.",
    )
    is_active: Optional[bool] = Field(
        None,
        description="Set False to hide this product from this branch entirely.",
    )
    stock_quantity: Optional[int] = Field(
        None,
        ge=0,
        description="Branch-level stock quantity.",
    )


class BranchInventoryResponse(BaseModel):
    """Response showing the merged effective data for a product in a branch."""
    inventory_id: Optional[UUID] = None
    product_id: UUID
    branch_id: UUID
    global_price: Decimal
    branch_price: Optional[Decimal] = None
    effective_price: Decimal
    global_discount: Optional[float] = None
    branch_discount: Optional[float] = None
    effective_discount: Optional[float] = None
    effective_discount_price: Optional[Decimal] = None
    stock_quantity: int
    is_on_sale: bool
    is_active: bool

    model_config = {"from_attributes": True}

# Branch stock/pricing overrides are written through the BranchScope-governed
# /admin/inventory surface (app/schemas/inventory.py), not from here.
