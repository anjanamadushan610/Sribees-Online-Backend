"""
Order Pydantic Schemas
"""
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

# ============================================================================
# Enums
# ============================================================================


class PaymentMethodEnum(str, Enum):
    """Payment methods supported at checkout."""
    COD = "COD"
    WALLET = "WALLET"
    MARX_OTHER = "MARX_OTHER"   # Visa / MasterCard / UnionPay via Marx IPG, LKR
    MARX_AMEX = "MARX_AMEX"     # Amex / Diners / Discover via Marx IPG, LKR
    MARX_BANK = "MARX_BANK"     # PAY_BY_BANK_ACCOUNT via Marx IPG


# Payment methods that route through the Marx IPG hosted gateway rather than
# being settled immediately (COD) or from an internal ledger (WALLET).
GATEWAY_PAYMENT_METHODS = {
    PaymentMethodEnum.MARX_OTHER.value,
    PaymentMethodEnum.MARX_AMEX.value,
    PaymentMethodEnum.MARX_BANK.value,
}

# Our enum -> Marx's own `paymentMethod` request value (API doc section 5.1).
# LKR-only variants by design -- OTHER_USD/AMEX_USD are intentionally absent.
MARX_PAYMENT_METHOD_MAP = {
    PaymentMethodEnum.MARX_OTHER.value: "OTHER",
    PaymentMethodEnum.MARX_AMEX.value: "AMEX",
    PaymentMethodEnum.MARX_BANK.value: "PAY_BY_BANK_ACCOUNT",
}


def to_courier_payment_method(payment_method: Optional[str]) -> str:
    """
    Map our payment_method to the courier contract's own vocabulary
    (docs/COURIER_API.md section 4: ``"cod"`` or ``"prepaid"`` — COD usually
    carries a collection fee that must be priced into the quote).
    """
    normalized = (payment_method or "").strip().upper()
    return "cod" if normalized == PaymentMethodEnum.COD.value else "prepaid"

class OrderStatusEnum(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    PACKING = "packing"
    PACKED = "packed"
    HANDED_TO_COURIER = "handed_to_courier"
    SHIPPED = "shipped"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    DELIVERY_FAILED = "delivery_failed"
    RTO_INITIATED = "rto_initiated"
    CANCELLED = "cancelled"
    RETURN_REQUESTED = "return_requested"
    RETURN_APPROVED = "return_approved"
    REFUNDED = "refunded"


class PaymentStatusEnum(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


# ============================================================================
# Order Item Schemas
# ============================================================================

class OrderItemCreate(BaseModel):
    """Schema for order item during creation."""
    product_id: str
    variant_id: Optional[str] = None
    product_name: str
    product_sku: Optional[str] = None
    product_image: Optional[str] = None
    quantity: int = Field(..., ge=1)
    unit_price: Decimal


class OrderItemResponse(BaseModel):
    """Order item in response."""
    order_item_id: str
    product_id: str
    variant_id: Optional[str] = None
    product_name: str
    product_sku: Optional[str] = None
    product_image: Optional[str] = None
    quantity: int
    unit_price: float
    subtotal: float
    tax_amount: float = 0

    model_config = {"from_attributes": True}


# ============================================================================
# Address Schemas
# ============================================================================

class DeliveryAddressSchema(BaseModel):
    """Delivery address in order response."""
    address_id: str
    address_line1: str
    address_line2: Optional[str] = None
    post_office: str
    district: str
    postal_code: str
    province: str


class OrderStatusStep(BaseModel):
    """A single step in the order status timeline."""
    status: str
    label: str
    timestamp: Optional[datetime] = None
    completed: bool = False
    current: bool = False


# ============================================================================
# Order Request Schemas
# ============================================================================

class CreateOrderRequest(BaseModel):
    """Request to create an order."""
    delivery_address_id: str
    delivery_slot_date: Optional[datetime] = None
    delivery_slot_time: Optional[str] = None
    # COD, WALLET, or one of the Marx IPG gateway methods (MARX_OTHER/MARX_AMEX/MARX_BANK).
    payment_method: str = "COD"
    coupon_code: Optional[str] = None
    # When true, the wallet is applied to reduce the total. Defaults to the
    # full available balance (capped at the order total); wallet_amount
    # narrows that to a smaller customer-chosen amount — see
    # PricingService.compute_breakdown for the clamping/validation.
    use_wallet: bool = False
    wallet_amount: Optional[Decimal] = Field(
        None,
        ge=0,
        description=(
            "Custom amount to deduct from the wallet, ignored unless "
            "use_wallet is true. Omit to use the full available balance."
        ),
    )
    notes: Optional[str] = None

    @field_validator("payment_method")
    @classmethod
    def validate_payment_method(cls, v: str) -> str:
        """Accept only a known PaymentMethodEnum value (case-insensitive)."""
        normalized = (v or "").strip().upper()
        allowed = {m.value for m in PaymentMethodEnum}
        if normalized not in allowed:
            raise ValueError(
                f"payment_method must be one of {sorted(allowed)} (got '{v}')"
            )
        return normalized


class QuoteOrderRequest(BaseModel):
    """
    Request to preview order totals without persisting anything.

    Mirrors the pricing-relevant fields of CreateOrderRequest. The coupon and
    cart contents are read from the user's server-side cart; `coupon_code` is
    accepted for API symmetry/forward-compat but the applied cart coupon is
    authoritative.
    """
    delivery_address_id: Optional[str] = None
    use_wallet: bool = False
    wallet_amount: Optional[Decimal] = Field(
        None,
        ge=0,
        description="Custom wallet amount to preview; omit to preview the full available balance.",
    )
    coupon_code: Optional[str] = None
    # Selected payment method, so the courier's COD-collection fee (if any)
    # prices correctly into the preview. Defaults to COD when omitted.
    payment_method: Optional[str] = None


class ReturnItemSchema(BaseModel):
    """A single order item being returned."""
    order_item_id: str
    quantity: int = Field(..., ge=1)


class ReturnRequest(BaseModel):
    """Customer request to return a delivered order (Module 5.5)."""
    reason: str = Field(..., min_length=1, max_length=255)
    comments: Optional[str] = None
    # Optional item-level selection; empty means a full-order return.
    items: List[ReturnItemSchema] = Field(default_factory=list)


class UpdateOrderStatusRequest(BaseModel):
    """Standard status transition — must be a move the fulfilment state machine
    (app/services/order_workflow.py) allows for the caller's role."""
    status: OrderStatusEnum


class OverrideOrderStatusRequest(BaseModel):
    """Super-Admin emergency override — bypasses the state machine, mandatory
    justification, permanently logged to the audit trail (B1 §4)."""
    status: OrderStatusEnum
    reason: str = Field(
        ...,
        min_length=15,
        max_length=1000,
        description="Why an emergency manual status change is required.",
    )


class UpdatePaymentStatusRequest(BaseModel):
    """Request to update payment status."""
    payment_status: PaymentStatusEnum
    payment_id: Optional[str] = None


class ReturnResolution(str, Enum):
    """How a Branch Manager / Super Admin settled a return claim (B4 §2)."""
    RETURNLESS_REFUND = "returnless_refund"   # spoilage — refund, no pickup
    REVERSE_PICKUP = "reverse_pickup"         # refund + a rider collects the goods


class ResolveReturnRequest(BaseModel):
    resolution: ReturnResolution = ReturnResolution.RETURNLESS_REFUND
    note: Optional[str] = Field(None, max_length=1000)


class RejectReturnRequest(BaseModel):
    note: Optional[str] = Field(None, max_length=1000)


class ReturnNoteRequest(BaseModel):
    """Customer Support appends a proof/context note to an open return claim."""
    note: str = Field(..., min_length=3, max_length=1000)


# ============================================================================
# Order Response Schemas
# ============================================================================

class OrderSummary(BaseModel):
    """Order summary for list views."""
    order_id: str
    order_number: str
    status: str
    payment_status: str
    total_amount: float
    item_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class OrderResponse(BaseModel):
    """Full order response."""
    order_id: str
    order_number: str
    user_id: str

    # Amounts
    subtotal: float
    tax_amount: float
    shipping_amount: float
    discount_amount: float
    wallet_deduction: float = 0
    cashback_earned: float = 0
    total_amount: float

    # Status
    status: str
    payment_status: str
    payment_method: Optional[str] = None
    payment_id: Optional[str] = None

    # Marx IPG gateway (response-only; payment_redirect_url is never persisted)
    payment_redirect_url: Optional[str] = None
    gateway_status: Optional[str] = None

    # Delivery handover verification code
    delivery_verification_code: Optional[str] = None
    delivery_verification_status: Optional[str] = None

    # Courier tracking
    courier_tracking_status: Optional[str] = None
    courier_waybill_number: Optional[str] = None

    # Delivery
    delivery_address: Optional[DeliveryAddressSchema] = None
    delivery_slot_date: Optional[datetime] = None
    delivery_slot_time: Optional[str] = None

    # Additional
    coupon_code: Optional[str] = None
    notes: Optional[str] = None

    # Items
    items: List[OrderItemResponse] = []
    item_count: int = 0

    # Status timeline (derived from status + timestamps)
    status_timeline: List[OrderStatusStep] = []

    # Timestamps
    created_at: datetime
    updated_at: Optional[datetime] = None
    shipped_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class OrdersListResponse(BaseModel):
    """Response for orders list."""
    success: bool = True
    data: dict


class OrderDetailResponse(BaseModel):
    """Response for single order."""
    success: bool = True
    data: OrderResponse


class OrderCreateResponse(BaseModel):
    """Response after creating an order."""
    success: bool = True
    data: OrderResponse
    message: str = "Order placed successfully"


class OrderQuoteBreakdown(BaseModel):
    """Authoritative, server-calculated financial breakdown (no persistence)."""
    subtotal: float
    delivery_fee: float
    discount: float
    tax: float
    wallet_deduction: float
    cashback_earned: float
    total: float
    # Set when the delivery fee came from a live courier quote (None on
    # flat-fee fallback) -- echoed back so booking can reference it.
    courier_quote_id: Optional[str] = None
    # Context so the client can render wallet UI without a second call.
    currency: str = "LKR"
    cashback_rate: float
    wallet_balance: float
    wallet_applied: bool
    item_count: int


class OrderQuoteResponse(BaseModel):
    """Response wrapper for POST /orders/quote."""
    success: bool = True
    data: OrderQuoteBreakdown
