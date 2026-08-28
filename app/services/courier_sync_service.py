"""
Courier Sync — apply SribeesExpress shipment state onto our orders.

Three callers converge here, and they must agree on what a shipment payload
means, so the mapping lives in one place:

* ``app/api/v1/courier_webhooks.py`` — the signed push (contract section 10.2)
* ``app/api/v1/orders.py`` — the customer opening the tracking screen
* ``sweep()`` below — the reconciliation poll (contract section 7a)

The poll is not belt-and-braces, it is load-bearing. SribeesExpress has no
webhook retry (section 10.4): a push that fails is gone, and a shipment whose
status changes exactly once — straight to ``delivered`` — is never resent. For
a COD order that lost push is the only record that the cash was collected, so
webhooks are treated as a latency optimisation and this sweep as the source of
truth.

**A `delivered` status does not mean a COD order was paid.** Contract section
8 is explicit that ``cod.collected`` flips separately, and can lag the status.
Anything that cares about money reads ``Order.courier_cod_collected``, never
``courier_tracking_status``.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Tuple

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.order import Order
from app.schemas.order import OrderStatusEnum
from app.services.courier_service import CourierError, CourierService, CourierUnavailableError
from app.services.delivery_verification_service import DeliveryVerificationService

# SribeesExpress's external status vocabulary (contract section 8) mapped onto
# our own order statuses. Only the transitions that mean something to a
# customer are mapped: booked/picked_up/in_transit all sit under our existing
# "shipped", and `cancelled`/`returned` are deliberately absent — a courier
# cancelling a parcel must not silently cancel a paid order, that is an ops
# decision with refund consequences.
STATUS_MAP = {
    "out_for_delivery": OrderStatusEnum.OUT_FOR_DELIVERY,
    "delivered": OrderStatusEnum.DELIVERED,
}


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def apply_shipment_payload(
    db: AsyncSession,
    order: Order,
    payload: dict,
    *,
    commit: bool = True,
) -> bool:
    """
    Write a shipment payload (webhook event, tracking read, or sweep row) onto
    ``order``. Returns True if anything actually changed.

    Tolerant of partial payloads by design: a webhook event carries only
    ``status``/``at`` plus the conditional ``handover``/``cod``/
    ``failure_note`` blocks, while a section 8 read carries everything. A key
    that is absent is left alone rather than nulled — absent means "not
    reported in this message", not "cleared".
    """
    changed = False

    status = payload.get("status")
    if status and status != order.courier_tracking_status:
        order.courier_tracking_status = status
        changed = True

    at = _parse_dt(payload.get("at") or payload.get("updated_at"))
    if at is not None:
        order.courier_tracking_updated_at = at
        changed = True

    if payload.get("tracking_url") and payload["tracking_url"] != order.courier_tracking_url:
        order.courier_tracking_url = payload["tracking_url"]
        changed = True

    if payload.get("pickup_location_name"):
        order.courier_pickup_location_name = payload["pickup_location_name"]
        changed = True

    handover = payload.get("handover")
    if isinstance(handover, dict):
        order.courier_handover_required = bool(handover.get("required"))
        order.courier_handover_verified = handover.get("code_verified")
        order.courier_handover_attempts = handover.get("attempts")
        # Keep our own verification row in step, so the admin/order screens
        # have one story about whether the handover was proven. The status
        # vocabulary is owned by DeliveryVerificationService, not duplicated
        # here.
        if handover.get("required") and handover.get("code_verified") is not None:
            await DeliveryVerificationService.record_outcome(
                db,
                order,
                verified=bool(handover.get("code_verified")),
                attempts=handover.get("attempts"),
                at=_parse_dt(payload.get("at")),
            )
        changed = True

    cod = payload.get("cod")
    if isinstance(cod, dict):
        collected = bool(cod.get("collected"))
        if collected and not order.courier_cod_collected:
            order.courier_cod_collected = True
            amount = cod.get("amount")
            if amount is not None:
                order.courier_cod_collected_amount = Decimal(str(amount))
            order.courier_cod_collected_at = _parse_dt(payload.get("at")) or datetime.now(
                timezone.utc
            )
            logger.info(
                f"COD collected for order {order.order_number}: "
                f"LKR {order.courier_cod_collected_amount}"
            )
            changed = True

    if "failure_note" in payload and payload["failure_note"] != order.courier_failure_note:
        order.courier_failure_note = payload["failure_note"]
        changed = True

    # Mirror onto our own order status last, so the courier fields are already
    # written when update_status fires its side effects (notifications,
    # cashback crediting).
    mapped = STATUS_MAP.get(status or "")
    if mapped is not None and order.status != mapped.value:
        from app.services.order_service import OrderService

        await OrderService.update_status(
            db, order, mapped, changed_by="SribeesExpress", notes=f"Courier reported {status}"
        )
        changed = True
    elif changed and commit:
        await db.commit()

    return changed


async def refresh_order_tracking(db: AsyncSession, order: Order) -> Optional[dict]:
    """
    Pull one shipment's current state (contract section 8, with ``history``)
    and apply it. Returns the raw payload, or None if SribeesExpress could not
    be reached — the caller shows whatever it already has rather than erroring.
    """
    if not order.courier_shipment_id:
        return None
    try:
        payload = await CourierService.get_shipment(order.courier_shipment_id)
    except (CourierUnavailableError, CourierError) as exc:
        logger.warning(f"Could not refresh tracking for order {order.order_number}: {exc}")
        return None

    await apply_shipment_payload(db, order, payload)
    return payload


async def _get_cursor(db: AsyncSession) -> Optional[datetime]:
    row = (await db.execute(text("SELECT last_synced_at FROM courier_sync_state WHERE id = 1"))).first()
    return row[0] if row else None


async def _set_cursor(db: AsyncSession, *, synced_at: Optional[datetime], error: Optional[str]) -> None:
    await db.execute(
        text(
            "UPDATE courier_sync_state "
            "SET last_synced_at = COALESCE(:synced_at, last_synced_at), "
            "    last_run_at = NOW(), "
            "    last_error = :error "
            "WHERE id = 1"
        ),
        {"synced_at": synced_at, "error": error},
    )
    await db.commit()


async def sweep(db: AsyncSession) -> Tuple[int, int]:
    """
    Reconciliation poll (contract section 7a). Returns (rows_seen, rows_applied).

    Pages ``GET /ecommerce/shipments?updated_since=<cursor>`` and applies every
    row to the matching order. The cursor only advances on a clean pass: a
    partial failure re-reads the same window next time, which is safe because
    applying a payload is idempotent, and is much better than skipping a
    ``delivered`` we never saw.

    **The cursor is our own start-of-run timestamp, not a value read from the
    rows.** It has to be: ``updated_since`` filters on a field SribeesExpress
    never actually serializes — no ``updated_at`` appears anywhere in a
    shipment response (see docs/SRIBEESEXPRESS_OPEN_ITEMS.md section 2, which
    asks them to add it). ``created_at`` is not a substitute; a shipment booked
    Monday and delivered Friday would drag the cursor back to Monday on every
    sweep, so it would never advance at all.

    Taking the time *before* the first request, rather than after the last, is
    what makes this safe: anything SribeesExpress updates while the sweep is
    running is stamped at or after that instant, so the next run still sees it.
    The extra minute of rewind covers clock skew between their host and ours.
    Both are mitigations for the missing field, not a fix — a shipment updated
    inside the skew window is still re-read rather than missed, which is the
    direction we want to fail in.
    """
    # Captured BEFORE the first request -- see the docstring. Using the time
    # the sweep *finished* would silently skip anything updated mid-run.
    started_at = datetime.now(timezone.utc)
    cursor = await _get_cursor(db)
    since = (cursor - timedelta(minutes=1)) if cursor else None

    seen = 0
    applied = 0
    offset = 0
    limit = settings.courier_sync_batch_limit

    try:
        while True:
            rows = await CourierService.list_shipments(
                updated_since=since, limit=limit, offset=offset
            )
            if not rows:
                break

            for row in rows:
                seen += 1
                shipment_id = row.get("shipment_id")
                if not shipment_id:
                    continue
                order = (
                    await db.execute(
                        select(Order).where(Order.courier_shipment_id == shipment_id)
                    )
                ).scalar_one_or_none()
                if order is None:
                    # A shipment we did not book, or one whose order was
                    # deleted. Not an error -- the sandbox and production share
                    # a host, and ops can book manually.
                    logger.debug(f"Sweep: no order for shipment {shipment_id}")
                    continue
                if await apply_shipment_payload(db, order, row, commit=False):
                    applied += 1

            await db.commit()
            if len(rows) < limit:
                break
            offset += limit
    except (CourierUnavailableError, CourierError) as exc:
        await db.rollback()
        logger.error(f"Courier reconciliation sweep failed: {exc}")
        await _set_cursor(db, synced_at=None, error=str(exc)[:500])
        return seen, applied

    await _set_cursor(db, synced_at=started_at, error=None)
    logger.info(f"Courier reconciliation sweep: {seen} shipment(s) seen, {applied} applied")
    return seen, applied
