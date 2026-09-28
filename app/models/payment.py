"""
One try at paying an order online (migration 076).

An order has as many as the customer needed: a declined card, then a bank
transfer. The order is paid when any of them is; see app/payments/service.py.
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.config.database import Base


class PaymentAttemptStatus(str, enum.Enum):
    STARTED = "started"        # sent to the gateway; the customer is paying
    PAID = "paid"
    FAILED = "failed"          # the gateway said no
    PENDING = "pending"        # the gateway has not decided yet
    SUPERSEDED = "superseded"  # a newer try was started; still checked
    EXPIRED = "expired"        # the order's payment window closed unpaid


# A try the customer may still complete, so the sweep keeps asking about it.
OPEN_ATTEMPT_STATUSES = (
    PaymentAttemptStatus.STARTED.value,
    PaymentAttemptStatus.PENDING.value,
    PaymentAttemptStatus.SUPERSEDED.value,
)


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"

    attempt_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    order_id = Column(
        UUID(as_uuid=True), ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_no = Column(Integer, nullable=False)
    method_code = Column(String(40), nullable=False)
    gateway = Column(String(30), nullable=False)
    reference = Column(String(80), nullable=False, unique=True)
    gateway_ref = Column(String(80), nullable=True)
    amount = Column(Numeric(10, 2), nullable=False)
    status = Column(String(20), nullable=False, default=PaymentAttemptStatus.STARTED.value)
    gateway_code = Column(String(80), nullable=True)
    raw = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<PaymentAttempt {self.reference} {self.status}>"
