"""
Finance outbox (migration 093): events Sribees Finance pulls to keep the books.

Rows are written by app/services/finance_outbox.py in the same transaction as
the change they describe, and are never updated or deleted by this app.
"""
import uuid

from sqlalchemy import BigInteger, Column, DateTime, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.config.database import Base


class FinanceOutbox(Base):
    __tablename__ = "finance_outbox"

    seq = Column(BigInteger, primary_key=True, autoincrement=True)
    event_id = Column(UUID(as_uuid=True), nullable=False, unique=True, default=uuid.uuid4)
    event_type = Column(String(40), nullable=False)
    aggregate_id = Column(UUID(as_uuid=True), nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    payload = Column(JSONB, nullable=False)

    def __repr__(self):
        return f"<FinanceOutbox {self.seq} {self.event_type}>"


class FinanceStockLevel(Base):
    """Sellable stock as Sribees Finance last pushed it (migration 094). A shadow
    record for the parallel run; it does not change what the shop sells."""

    __tablename__ = "finance_stock_levels"

    branch_id = Column(UUID(as_uuid=True), primary_key=True)
    # migration 100: one row per stock item -- the product, or a pack size (the variant)
    item_id = Column(UUID(as_uuid=True), primary_key=True)
    product_id = Column(UUID(as_uuid=True), nullable=False)
    variant_id = Column(UUID(as_uuid=True), nullable=True)
    sellable_qty = Column(Numeric(14, 3), nullable=False)
    unit = Column(String(10), nullable=False)
    version = Column(BigInteger, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FinanceMinPrice(Base):
    """The lowest price that keeps the margin, as Sribees Finance last pushed it
    (migration 102). Advice for the admin, the Price app and Price Intel; it never
    changes what a customer pays."""

    __tablename__ = "finance_min_prices"

    branch_id = Column(UUID(as_uuid=True), primary_key=True)
    item_id = Column(UUID(as_uuid=True), primary_key=True)  # the product, or a pack size
    product_id = Column(UUID(as_uuid=True), nullable=False)
    variant_id = Column(UUID(as_uuid=True), nullable=True)
    min_price = Column(Numeric(14, 2), nullable=True)
    ready_cost = Column(Numeric(14, 2), nullable=True)
    purchase_cost = Column(Numeric(14, 2), nullable=True)
    materials_cost = Column(Numeric(14, 2), nullable=True)
    margin_pct = Column(Numeric(5, 2), nullable=True)
    cashback_pct = Column(Numeric(5, 2), nullable=True)
    referral_pct = Column(Numeric(5, 2), nullable=True)
    cost_ref = Column(String(120), nullable=True)
    no_floor_reason = Column(String(200), nullable=True)
    version = Column(BigInteger, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
