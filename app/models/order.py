"""
Order SQLAlchemy Models
"""
import enum
import uuid

from sqlalchemy import (
    Boolean,
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
    """
    Order status enum.

    ``orders.status`` is a plain VARCHAR(30) (not a Postgres enum), so adding
    members here needs no ``ALTER TYPE`` migration — only the app-side guards
    in ``app/services/order_workflow.py``.

    PACKING / PACKED / READY_FOR_PICKUP are the branch-floor fulfilment steps
    driven from the admin Order Details panel (Branch Manager or Super Admin).
    READY_FOR_PICKUP is the moment the parcel is booked with SribeesExpress —
    "packed, come and collect it" — and from HANDED_TO_COURIER on, the courier
    drives the status. PROCESSING is the legacy pre-workflow equivalent of
    PACKING, still accepted for orders placed before this shipped.
    DELIVERY_FAILED / RTO_INITIATED are the last-mile exception path.

    AWAITING_PAYMENT is an online-payment order (card, bank) the gateway has
    not yet confirmed. It is not placed: the branch cannot act on it, the
    "order placed" notice is not sent, and its stock is only held. It becomes
    CONFIRMED when the money arrives, or CANCELLED when the payment window
    closes unpaid (app/payments/service.py).
    """
    AWAITING_PAYMENT = "awaiting_payment"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    PACKING = "packing"
    PACKED = "packed"
    READY_FOR_PICKUP = "ready_for_pickup"
    HANDED_TO_COURIER = "handed_to_courier"
    SHIPPED = "shipped"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    DELIVERY_FAILED = "delivery_failed"
    RTO_INITIATED = "rto_initiated"
    CANCELLED = "cancelled"
    # Post-delivery returns flow (Module 5.5)
    RETURN_REQUESTED = "return_requested"
    RETURN_APPROVED = "return_approved"
    RETURN_REJECTED = "return_rejected"
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
    # Two numbers the courier can reach the customer on, captured at
    # checkout and frozen here: the profile's numbers may change later, but
    # who to call about THIS parcel must not. Nullable in the column and
    # required by CreateOrderRequest, so orders placed before this shipped
    # still load.
    contact_phone = Column(String(20), nullable=True)
    alternate_phone = Column(String(20), nullable=True)

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

    # Delivery handover verification code. We generate it, show it to the
    # customer in the app, and hand it to SribeesExpress at booking time; the
    # rider must enter it before the parcel is released. SribeesExpress
    # accepts a code on PREPAID shipments only -- see
    # app/services/delivery_verification_service.py and
    # docs/SRIBEESEXPRESS_ECOMMERCE_API.md §7.
    delivery_verification_code = Column(String(6), nullable=True)
    delivery_verification_status = Column(String(20), default="not_required")
    delivery_verification_attempts = Column(Integer, default=0)
    delivery_verification_confirmed_at = Column(DateTime(timezone=True), nullable=True)

    # Courier integration -- SribeesExpress (see
    # docs/SRIBEESEXPRESS_ECOMMERCE_API.md, app/services/courier_service.py).
    # courier_waybill_number holds the same value as courier_shipment_id: the
    # contract confirms there is only ever one identifier (§11.1). It is kept
    # because GET /orders already returns it and the mobile app reads it.
    courier_quote_id = Column(String(64), nullable=True)
    courier_quote_expires_at = Column(DateTime(timezone=True), nullable=True)
    # The delivery charge SribeesExpress quoted and held for this order
    # (POST /ecommerce/rates/{id}/confirm, migration 052). shipping_amount is
    # what the customer was charged; this is what the courier agreed to bill.
    # They are equal at checkout; they differ only if a booking had to re-quote
    # after the hold lapsed, which is the number finance needs to see.
    courier_quote_amount = Column(Numeric(10, 2), nullable=True)
    referral_delivery_discount = Column(Numeric(10, 2), nullable=False, default=0, server_default="0")
    courier_quote_confirmed_at = Column(DateTime(timezone=True), nullable=True)
    courier_shipment_id = Column(String(64), nullable=True)
    courier_waybill_number = Column(String(64), nullable=True)
    courier_tracking_status = Column(String(20), nullable=True)
    courier_tracking_updated_at = Column(DateTime(timezone=True), nullable=True)
    courier_booking_status = Column(String(20), default="pending")
    courier_tracking_url = Column(String(500), nullable=True)
    courier_pickup_location_name = Column(String(255), nullable=True)

    # Delivery outcome as reported by SribeesExpress. This is the ONLY
    # proof-of-delivery we ever receive, so all of it is persisted rather than
    # collapsed into the status. For a COD order, `delivered` on its own does
    # NOT mean the cash was collected -- courier_cod_collected does (§8).
    # server_default, not just default: `default=` is Python-side, so a
    # table built by create_all() (the E2E harness, a fresh local DB) would
    # get NOT NULL with no DEFAULT and reject any raw INSERT that omits the
    # column. Migration 050 declares the same default.
    courier_handover_required = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    courier_handover_verified = Column(Boolean, nullable=True)
    courier_handover_attempts = Column(Integer, nullable=True)
    courier_cod_collected = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    courier_cod_collected_amount = Column(Numeric(12, 2), nullable=True)
    courier_cod_collected_at = Column(DateTime(timezone=True), nullable=True)
    # Rider free text on a failed/returned shipment. Free text by design
    # (§8) -- never branch business logic on its exact wording. Branch on
    # courier_will_retry instead, which is the machine-readable half.
    courier_failure_note = Column(Text, nullable=True)
    # Only meaningful while courier_tracking_status is `failed`: True means
    # SribeesExpress can still redispatch, False means it is heading for
    # `returned`. This is the refund-now vs tell-them-tomorrow signal. There is
    # no next_attempt_at to go with it -- nothing on their side schedules a
    # redispatch on a clock, a human decides.
    courier_will_retry = Column(Boolean, nullable=True)
    # The remittance run that paid this shipment's collected COD out to us.
    # NULL until settled -- and their runs are triggered by ops rather than a
    # scheduler, so NULL for days is normal, not a fault.
    courier_remittance_id = Column(Integer, nullable=True)

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
    # Relative media paths for the photos the customer attached, e.g.
    # ["/uploads/returns/<order_id>/a1b2.jpg"]. Origin-free, like every
    # other image column -- media_url() resolves them when serializing.
    return_images = Column(JSONB, nullable=True)
    return_requested_at = Column(DateTime(timezone=True), nullable=True)
    refund_amount = Column(Numeric(10, 2), nullable=True)
    # B4 §2: how a Branch Manager / Super Admin settled the claim.
    # 'returnless_refund' (spoilage, no pickup) | 'reverse_pickup' (rider collects).
    return_resolution = Column(String(30), nullable=True)
    return_resolution_note = Column(Text, nullable=True)

    # --- The reverse pickup (migration 054) ---------------------------------
    #
    # A `reverse_pickup` resolution books a return leg with SribeesExpress: a
    # second shipment, with its own SXR waybill, that collects from the
    # customer and brings the goods back to our branch. See
    # SribeesExpress/backend-fastapi/docs/RETURNS_ARCHITECTURE.md §7.
    #
    # Kept in their own columns rather than reusing the courier_* ones above:
    # the forward shipment's tracking is what proves the delivery happened,
    # and a return must not overwrite it.
    return_waybill_number = Column(String(64), nullable=True)
    return_booking_status = Column(String(20), default="not_required")
    return_booking_error = Column(Text, nullable=True)
    # What SribeesExpress charges us to carry it back. Ours to pay, not the
    # customer's — the customer is refunded the goods, not the freight.
    return_fee = Column(Numeric(10, 2), nullable=True)

    # The code our counter staff read to the rider when the goods arrive back.
    # Plain text, and *only* here: SribeesExpress stores a hash and genuinely
    # cannot tell us what it was, so a lost value means the branch asks for a
    # new one. We are the merchant in this exchange and this is our own
    # secret — the same posture as `delivery_verification_code` above, which
    # we hold for the customer's handover.
    return_handover_code = Column(String(6), nullable=True)

    return_tracking_status = Column(String(20), nullable=True)
    return_tracking_updated_at = Column(DateTime(timezone=True), nullable=True)

    # `return_received_at` is the one that matters: the goods are physically
    # back with us, and — per the business decision recorded in
    # RETURNS_ARCHITECTURE.md §7 — that, not approval, is when the customer's
    # wallet is credited.
    return_approved_at = Column(DateTime(timezone=True), nullable=True)
    return_rejected_at = Column(DateTime(timezone=True), nullable=True)
    return_collected_at = Column(DateTime(timezone=True), nullable=True)
    return_received_at = Column(DateTime(timezone=True), nullable=True)
    refunded_at = Column(DateTime(timezone=True), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # Fulfilment milestones — set by OrderService.update_status on the transition
    # into each state (mirror the shipped_at / delivered_at pattern).
    packed_at = Column(DateTime(timezone=True), nullable=True)
    ready_for_pickup_at = Column(DateTime(timezone=True), nullable=True)
    handed_to_courier_at = Column(DateTime(timezone=True), nullable=True)
    shipped_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    delivery_failed_at = Column(DateTime(timezone=True), nullable=True)
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
    display_size = Column(String(80), nullable=True)
    display_size_unit = Column(String(32), nullable=True)
    # The weight option bought ("500g") for a sold-by-weight product, else NULL.
    variant_name = Column(String(50), nullable=True)
    product_sku = Column(String(100), nullable=True)
    product_image = Column(String(500), nullable=True)

    # Pricing
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Numeric(10, 2), nullable=False)
    subtotal = Column(Numeric(10, 2), nullable=False)
    tax_amount = Column(Numeric(10, 2), default=0)

    # Snapshot of Product.is_returnable at checkout, like unit_price: the
    # terms a customer bought under never change after the fact. Migration
    # 064 added the column; this mapping was missing, which meant every
    # OrderItem(is_returnable=...) construction in order_service.py raised
    # TypeError and broke order placement entirely.
    is_returnable = Column(Boolean, nullable=False, default=True, server_default="true")
    # Snapshot of the effective cashback % resolved at checkout for this
    # line (branch override -> product default -> platform rate). See
    # migration 065. Null for orders placed before this column existed.
    cashback_percentage = Column(Numeric(5, 2), nullable=True)
    # Snapshot of the level-1 referral commission % resolved at checkout
    # (branch override -> product default -> 0), and this line's share of the
    # order's coupon discount. Together they are the line's commission basis
    # and rate; see migration 081. NULL rate = placed before per-product
    # commission existed.
    referral_commission_percentage = Column(Numeric(5, 2), nullable=True)
    coupon_discount = Column(
        Numeric(10, 2), nullable=False, default=0, server_default="0"
    )

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


class OrderCancellation(Base):
    """
    One row per cancelled order: who cancelled it, from where, how fast, and
    what it cost.

    Cancelling is a button in the customer's app rather than a support
    conversation, and a button that moves money is a button people will test.
    This is what makes the pattern visible to the Branch Manager, the Super
    Admin and Customer Support.

    Written inside the same transaction as the cancellation itself, so an
    order can never be cancelled without a record of why -- and, because
    ``order_id`` is UNIQUE, never recorded twice. That uniqueness is also what
    makes a double-submit on a slow connection impossible to refund twice.
    """

    __tablename__ = "order_cancellations"

    cancellation_id = Column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("orders.order_id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )

    # 'customer' or 'admin'. An admin cancelling on someone's behalf is a
    # normal operation and must never be counted against the customer.
    cancelled_by = Column(String(20), nullable=False, default="customer")
    actor_admin_id = Column(UUID(as_uuid=True), nullable=True)

    reason = Column(Text, nullable=True)

    # Where it came from. All nullable: an admin cancellation has no device,
    # and a proxy may strip the client address.
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(Text, nullable=True)
    device_id = Column(String(128), nullable=True)
    client_platform = Column(String(40), nullable=True)

    # Both timestamps, denormalised deliberately: the elapsed time is the
    # interesting figure and must stay correct even if the order row is later
    # touched.
    order_placed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), server_default=func.now())

    refund_amount = Column(Numeric(10, 2), nullable=False, default=0)
    # 'wallet' when money moved, 'none' for a COD order where nothing was paid.
    refund_destination = Column(String(20), nullable=False, default="none")

    # Kept back when the parcel was already booked with SribeesExpress. Stored
    # apart from refund_amount so the pair reads as "they paid X, we kept Y for
    # the courier" rather than one net figure nobody can reconcile.
    courier_fee = Column(Numeric(10, 2), nullable=False, default=0)

    # Set when the pattern is worth a human look. Never blocks the
    # cancellation or the refund: nobody is made to wait while somebody
    # decides whether they are a fraud.
    flagged = Column(Boolean, nullable=False, default=False)
    flag_reason = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<OrderCancellation {self.order_id} by {self.cancelled_by}>"
