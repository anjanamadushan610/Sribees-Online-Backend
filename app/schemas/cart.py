"""
Cart Pydantic Schemas
"""
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field

# ============================================================================
# Cart Item Schemas
# ============================================================================

class CartItemBase(BaseModel):
    """Base cart item schema."""
    product_id: str
    quantity: int = Field(..., ge=1)


class AddToCartRequest(CartItemBase):
    """Request to add item to cart."""
    price: Decimal
    name: str
    image: Optional[str] = None
    sku: Optional[str] = None
    variant_id: Optional[str] = None
    category_id: Optional[str] = None
    original_price: Optional[Decimal] = None
    cashback_percentage: Optional[Decimal] = None


class UpdateCartItemRequest(BaseModel):
    """Request to update cart item quantity."""
    quantity: int = Field(..., ge=0)


class CartItem(BaseModel):
    """Cart item in response."""
    product_id: str
    quantity: int
    price: float
    name: str
    image: Optional[str] = None
    sku: Optional[str] = None
    variant_id: Optional[str] = None
    category_id: Optional[str] = None
    original_price: Optional[float] = None
    cashback_percentage: Optional[float] = None


# ============================================================================
# Cart Totals
# ============================================================================

class CartTotals(BaseModel):
    """Cart totals breakdown."""
    subtotal: float = 0
    discount: float = 0
    tax: float = 0
    shipping: float = 0
    cashback_earned: float = 0
    total: float = 0


# ============================================================================
# Coupon Schemas
# ============================================================================

class ApplyCouponRequest(BaseModel):
    """Request to apply a coupon."""
    code: str = Field(..., min_length=1)


class CartCoupon(BaseModel):
    """Applied coupon information."""
    code: str
    discount_type: str  # 'percentage' or 'fixed'
    discount_value: float
    discount_amount: float
    max_discount_amount: Optional[float] = None
    min_order_value: Optional[float] = None


# ============================================================================
# Cart Response Schemas
# ============================================================================

class Cart(BaseModel):
    """Full cart response."""
    items: List[CartItem] = []
    totals: CartTotals
    coupon: Optional[CartCoupon] = None
    updated_at: int  # Unix timestamp


class CartResponse(BaseModel):
    """Standard cart response wrapper."""
    success: bool = True
    data: Cart


class CartClearResponse(BaseModel):
    """Response after clearing cart."""
    success: bool = True
    message: str = "Cart cleared successfully"


# ============================================================================
# Wishlist Schemas
# ============================================================================

class WishlistItemAdd(BaseModel):
    """Request to add item to wishlist."""
    product_id: str
    variant_id: Optional[str] = None
    price_at_watch: Optional[float] = None


class WishlistItem(BaseModel):
    """Wishlist item response."""
    wishlist_item_id: str
    user_id: str
    product_id: str
    variant_id: Optional[str] = None
    price_at_watch: Optional[float] = None
    added_at: str
    # Extended fields
    variant_name: Optional[str] = None
    variant_sku: Optional[str] = None
    current_price: Optional[float] = None
    variant_image: Optional[str] = None
    price_drop: float = 0
    price_drop_percentage: float = 0


class WishlistResponse(BaseModel):
    """Wishlist list response."""
    success: bool = True
    data: dict


class WishlistToggleResponse(BaseModel):
    """Response after toggling wishlist item."""
    success: bool = True
    data: dict
    message: str
