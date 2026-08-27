"""
Order SQLAlchemy Models
"""
import enum
import uuid

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.config.database import Base


class OrderStatus(str, enum.Enum):
    """Order status enum."""
    PENDING = "pending"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    # Post-delivery returns flow (Module 5.5)
    RETURN_REQUESTED = "return_requested"
    RETURN_APPROVED = "return_approved"
    REFUNDED = "refunded"


class PaymentStatus(str, enum.Enum):
    """Payment status enum."""
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class Order(Base):
    """Order model."""

    __tablename__ = "orders"

    order_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    order_number = Column(String(50), unique=True, nullable=False)

    # Fulfilling branch, resolved from the delivery address's post office at
    # checkout. Nullable: legacy/unmapped orders have no branch and are only
    # visible to Super Admins in the admin dashboard.
    branch_id = Column(
        UUID(as_uuid=True),
        ForeignKey("branches.branch_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Amounts
    subtotal = Column(Numeric(10, 2), nullable=False)
    tax_amount = Column(Numeric(10, 2), default=0)
    shipping_amount = Column(Numeric(10, 2), default=0)
    discount_amount = Column(Numeric(10, 2), default=0)
    # Wallet balance applied to this order + cashback credited for it.
    wallet_deduction = Column(Numeric(10, 2), default=0)
    cashback_earned = Column(Numeric(10, 2), default=0)
    total_amount = Column(Numeric(10, 2), nullable=False)

    # Status
    status = Column(String(30), default=OrderStatus.PENDING.value)
    payment_status = Column(String(30), default=PaymentStatus.PENDING.value)
    payment_id = Column(String(255), nullable=True)
    payment_method = Column(String(50), nullable=True)

    # Marx IPG gateway state (see app/services/marx_ipg_service.py). trId is
    # Marx's own transaction id; merchantRID is order_number, already unique.
    gateway_tr_id = Column(String(50), nullable=True)
    gateway_status = Column(String(20), nullable=True)
    gateway_response_code = Column(String(50), nullable=True)
    gateway_confirmed_at = Column(DateTime(timezone=True), nullable=True)
    gateway_raw_response = Column(JSONB, nullable=True)
    # Guards deferred cashback crediting (gateway orders) against double-credit.
    cashback_credited_at = Column(DateTime(timezone=True), nullable=True)

    # Delivery handover verification code (non-COD orders only). See
    # app/services/delivery_verification_service.py and docs/COURIER_API.md §15.
    delivery_verification_code = Column(String(3), nullable=True)
    delivery_verification_status = Column(String(20), default="not_required")
    delivery_verification_attempts = Column(Integer, default=0)
    delivery_verification_confirmed_at = Column(DateTime(timezone=True), nullable=True)

    # Courier integration (see docs/COURIER_API.md, app/services/courier_service.py).
    courier_quote_id = Column(String(64), nullable=True)
    courier_quote_expires_at = Column(DateTime(timezone=True), nullable=True)
    courier_shipment_id = Column(String(64), nullable=True)
    courier_waybill_number = Column(String(64), nullable=True)
    courier_tracking_status = Column(String(20), nullable=True)
    courier_tracking_updated_at = Column(DateTime(timezone=True), nullable=True)
    courier_booking_status = Column(String(20), default="pending")

    # Delivery
    delivery_address_id = Column(UUID(as_uuid=True), ForeignKey("addresses.address_id"), nullable=True)
    delivery_slot_date = Column(DateTime(timezone=True), nullable=True)
    delivery_slot_time = Column(String(50), nullable=True)

    # Additional info
    coupon_code = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)

    # Returns & refunds (Module 5.5). return_items is a JSON list of
    # {"order_item_id": "...", "quantity": N}; empty/NULL means a full return.
    return_reason = Column(String(255), nullable=True)
    return_comments = Column(Text, nullable=True)
    return_items = Column(JSONB, nullable=True)
    return_requested_at = Column(DateTime(timezone=True), nullable=True)
    refund_amount = Column(Numeric(10, 2), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    shipped_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    delivery_address = relationship("Address")
    branch = relationship("Branch")
    status_history = relationship(
        "OrderStatusHistory",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderStatusHistory.created_at.asc()",
    )

    def __repr__(self):
        return f"<Order {self.order_number}>"


class OrderItem(Base):
    """Order item model."""

    __tablename__ = "order_items"

    order_item_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.product_id"), nullable=False)
    variant_id = Column(UUID(as_uuid=True), ForeignKey("product_variants.variant_id"), nullable=True)

    # Snapshot data (at time of order)
    product_name = Column(String(255), nullable=False)
    product_sku = Column(String(100), nullable=True)
    product_image = Column(String(500), nullable=True)

    # Pricing
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Numeric(10, 2), nullable=False)
    subtotal = Column(Numeric(10, 2), nullable=False)
    tax_amount = Column(Numeric(10, 2), default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    order = relationship("Order", back_populates="items")
    product = relationship("Product")
    variant = relationship("ProductVariant")

    def __repr__(self):
        return f"<OrderItem {self.order_item_id}>"


class OrderStatusHistory(Base):
    """Order status change history model for audit logging."""

    __tablename__ = "order_status_history"

    history_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False, index=True)
    old_status = Column(String(50), nullable=True)
    new_status = Column(String(50), nullable=False)
    changed_by = Column(String(255), default="System")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    order = relationship("Order", back_populates="status_history")

    def __repr__(self):
        return f"<OrderStatusHistory {self.old_status} -> {self.new_status}>"
