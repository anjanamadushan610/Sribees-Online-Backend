"""
Product Pydantic Schemas
"""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, List, Literal, Optional
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field

from app.services.search_text import normalize_keywords

# Stored canonical: one ", "-separated list, de-duplicated, NFC. Admins paste
# from anywhere and the AI suggestion is appended to what is already there,
# so without this the same term piles up.
SearchKeywords = Annotated[Optional[str], AfterValidator(normalize_keywords)]

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

class ProductImageCreate(BaseModel):
    """Schema for adding a product image."""
    image_url: str
    alt_text: Optional[str] = None
    is_primary: bool = False
    sort_order: int = 0


class ProductCreate(BaseModel):
    """
    Schema for creating a product.

    ``name``/``description``/``short_description`` are the ENGLISH copy and are
    what every other language falls back to, so English stays required while
    the ``_si``/``_ta`` translations are optional — a product can ship before
    it has been translated.
    """
    name: str = Field(..., min_length=1, max_length=255)
    name_si: Optional[str] = Field(None, max_length=255, description="Sinhala name")
    name_ta: Optional[str] = Field(None, max_length=255, description="Tamil name")
    slug: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    description_si: Optional[str] = None
    description_ta: Optional[str] = None
    short_description: Optional[str] = Field(None, max_length=500)
    short_description_si: Optional[str] = Field(None, max_length=500)
    short_description_ta: Optional[str] = Field(None, max_length=500)
    search_keywords: SearchKeywords = Field(
        None,
        description="Alternate names for search: Sinhala/Tamil/Singlish, common misspellings.",
    )
    sku: str = Field(..., min_length=1, max_length=100)
    price: Optional[Decimal] = Field(
        Decimal("0.00"),
        ge=0,
        description="Global base catalog price (defaults to 0.00 if omitted)",
    )
    compare_at_price: Optional[Decimal] = Field(None, ge=0)
    cost_price: Optional[Decimal] = Field(None, ge=0)
    cashback_percentage: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description=(
            "Bonus cashback % for this product across all branches. "
            "Null falls back to the platform default rate; a branch may "
            "still override this per product."
        ),
    )
    # Required: an uncategorised product is invisible to storefront/mobile
    # category filtering and inventory sync silently disagrees on what
    # "this product" even is without a SKU.
    category_id: UUID
    subcategory_id: Optional[UUID] = Field(
        None,
        description="Sub-category — must be a child of category_id",
    )
    low_stock_threshold: int = Field(10, ge=0)
    weight: Optional[Decimal] = None
    weight_unit: str = "kg"
    is_active: bool = True
    is_featured: bool = False
    is_perishable: bool = Field(
        False,
        description="Short shelf life — the stock Quick Sale exists to clear before it spoils",
    )
    is_returnable: bool = Field(
        True,
        description="Whether customers may return this product after delivery",
    )
    meta_title: Optional[str] = Field(None, max_length=255)
    meta_description: Optional[str] = None
    images: Optional[List[ProductImageCreate]] = Field(
        None,
        description="Product images (at least one thumbnail image required on create)",
    )


class ProductUpdate(BaseModel):
    """Schema for updating a product."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    name_si: Optional[str] = Field(None, max_length=255, description="Sinhala name")
    name_ta: Optional[str] = Field(None, max_length=255, description="Tamil name")
    slug: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    description_si: Optional[str] = None
    description_ta: Optional[str] = None
    short_description: Optional[str] = Field(None, max_length=500)
    short_description_si: Optional[str] = Field(None, max_length=500)
    short_description_ta: Optional[str] = Field(None, max_length=500)
    search_keywords: SearchKeywords = Field(
        None,
        description="Alternate names for search: Sinhala/Tamil/Singlish, common misspellings.",
    )
    sku: Optional[str] = Field(None, max_length=100)
    price: Optional[Decimal] = Field(
        None,
        ge=0,
        description="Global base catalog price",
    )
    compare_at_price: Optional[Decimal] = Field(None, ge=0)
    cost_price: Optional[Decimal] = Field(None, ge=0)
    cashback_percentage: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description=(
            "Bonus cashback % for this product across all branches. "
            "Null falls back to the platform default rate; a branch may "
            "still override this per product."
        ),
    )
    category_id: Optional[UUID] = None
    subcategory_id: Optional[UUID] = Field(
        None,
        description="Sub-category — must be a child of category_id",
    )
    low_stock_threshold: Optional[int] = Field(None, ge=0)
    weight: Optional[Decimal] = None
    weight_unit: Optional[str] = None
    is_active: Optional[bool] = None
    is_featured: Optional[bool] = None
    is_perishable: Optional[bool] = Field(
        None,
        description="Short shelf life — the stock Quick Sale exists to clear before it spoils",
    )
    is_returnable: Optional[bool] = Field(
        None,
        description="Whether customers may return this product after delivery",
    )
    meta_title: Optional[str] = Field(None, max_length=255)
    meta_description: Optional[str] = None


class SuggestKeywordsRequest(BaseModel):
    """Draft product fields sent to the AI keyword-suggestion endpoint."""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    short_description: Optional[str] = None
    category: Optional[str] = None


class SearchMissAttachRequest(BaseModel):
    """Attach a missed search to a product as a search keyword."""
    query_key: str = Field(..., min_length=1, max_length=200)
    product_id: UUID


class SearchMissStatusRequest(BaseModel):
    """Ignore a missed search, or put it back in the inbox."""
    query_key: str = Field(..., min_length=1, max_length=200)
    status: Literal["open", "ignored"]


class SuggestSkuRequest(BaseModel):
    """Draft product fields sent to the SKU suggestion endpoint."""
    name: Optional[str] = None
    category_id: Optional[UUID] = None


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
    cashback_percentage: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description=(
            "Override cashback % for this branch. Set null to fall back to the "
            "product's global cashback, and then to the platform rate."
        ),
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
