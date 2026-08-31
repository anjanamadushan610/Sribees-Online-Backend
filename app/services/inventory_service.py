"""
Order ↔ branch-inventory stock bookkeeping.

Wires ``branch_inventory.reserved_quantity`` / ``stock_quantity`` into the order
lifecycle. Every mutation here is a single atomic SQL ``UPDATE`` (no
read-modify-write races) and every entry point is **best-effort and
non-blocking** — a missing ``branch_inventory`` row or a NULL ``branch_id`` is
logged and skipped, never raised, so an inventory hiccup can't roll back a paid
or placed order. The financial + courier side of the order is the source of
truth; stock counts trail it.

Lifecycle (driven from OrderService):

    checkout                → reserve_for_order      reserved += qty
    → handed_to_courier      → commit_for_order       stock -= qty, reserved -= qty
    cancel (pre-dispatch)    → release_reservation_for_order   reserved -= qty
    cancel (post-dispatch)   → restock_for_order      stock += qty
"""
from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem
from app.models.product import BranchInventory


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _order_line_quantities(db: AsyncSession, order_id: UUID) -> list[tuple[UUID, int]]:
    """(product_id, quantity) for each line of an order, read straight from the
    table — ``order.items`` is not always eagerly loaded on the instance callers
    hold and a lazy load would fail under the async session."""
    rows = (
        await db.execute(
            select(OrderItem.product_id, OrderItem.quantity).where(
                OrderItem.order_id == order_id
            )
        )
    ).all()
    return [(pid, qty) for pid, qty in rows]


class InventoryService:
    """Atomic, non-blocking stock moves tied to the order lifecycle."""

    @staticmethod
    async def _apply(
        db: AsyncSession,
        branch_id: UUID,
        lines: Iterable[tuple[UUID, int]],
        *,
        values_fn,
        op: str,
    ) -> None:
        for product_id, qty in lines:
            if not qty or qty <= 0:
                continue
            result = await db.execute(
                update(BranchInventory)
                .where(
                    BranchInventory.branch_id == branch_id,
                    BranchInventory.product_id == product_id,
                )
                .values(**values_fn(qty))
            )
            if (result.rowcount or 0) == 0:
                logger.warning(
                    f"InventoryService.{op}: no branch_inventory row for product "
                    f"{product_id} in branch {branch_id} — skipped"
                )

    @staticmethod
    async def reserve_for_order(
        db: AsyncSession,
        order: Order,
        lines: Iterable[tuple[UUID, int]],
    ) -> None:
        """Hold each line's quantity against the fulfilling branch. Additive and
        unconditional (never blocks checkout); ``available = stock - reserved``
        may legitimately go negative and surface as an ops signal. Idempotent
        via ``order.stock_reserved_at``. Does not commit."""
        if order.stock_reserved_at is not None:
            return
        if order.branch_id is not None:
            await InventoryService._apply(
                db,
                order.branch_id,
                lines,
                values_fn=lambda q: {
                    "reserved_quantity": BranchInventory.reserved_quantity + q
                },
                op="reserve_for_order",
            )
        order.stock_reserved_at = _now()

    @staticmethod
    async def commit_for_order(db: AsyncSession, order: Order) -> bool:
        """Convert the reservation into a real ``stock_quantity`` deduction when
        the parcel is handed to the courier. Idempotent via
        ``order.stock_committed_at``. Returns True if it ran. Does not commit."""
        if order.stock_committed_at is not None:
            return False
        if order.branch_id is not None:
            lines = await _order_line_quantities(db, order.order_id)
            await InventoryService._apply(
                db,
                order.branch_id,
                lines,
                values_fn=lambda q: {
                    "stock_quantity": func.greatest(BranchInventory.stock_quantity - q, 0),
                    "reserved_quantity": func.greatest(
                        BranchInventory.reserved_quantity - q, 0
                    ),
                },
                op="commit_for_order",
            )
        order.stock_committed_at = _now()
        return True

    @staticmethod
    async def release_reservation_for_order(db: AsyncSession, order: Order) -> None:
        """Give reserved units back on a pre-dispatch cancellation. No-op if the
        order was never reserved or has already been committed (in which case
        the caller uses ``restock_for_order`` instead). Does not commit."""
        if order.stock_reserved_at is None or order.stock_committed_at is not None:
            return
        if order.branch_id is not None:
            lines = await _order_line_quantities(db, order.order_id)
            await InventoryService._apply(
                db,
                order.branch_id,
                lines,
                values_fn=lambda q: {
                    "reserved_quantity": func.greatest(
                        BranchInventory.reserved_quantity - q, 0
                    )
                },
                op="release_reservation_for_order",
            )
        order.stock_reserved_at = None

    @staticmethod
    async def restock_for_order(db: AsyncSession, order: Order) -> None:
        """Put units back on the shelf when an already-dispatched order is
        cancelled (Super Admin override only — the workflow forbids it for
        branch actors). Clears ``stock_committed_at`` so the order could be
        re-committed if it somehow ships again. Does not commit."""
        if order.stock_committed_at is None:
            return
        if order.branch_id is not None:
            lines = await _order_line_quantities(db, order.order_id)
            await InventoryService._apply(
                db,
                order.branch_id,
                lines,
                values_fn=lambda q: {
                    "stock_quantity": BranchInventory.stock_quantity + q
                },
                op="restock_for_order",
            )
        order.stock_committed_at = None
