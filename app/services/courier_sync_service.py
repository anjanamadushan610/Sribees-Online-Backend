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
from app.models.order import Order, OrderStatusHistory
from app.schemas.order import OrderStatusEnum
from app.services import courier_credentials
from app.services.courier_service import (
    CourierError,
    CourierService,
    CourierUnavailableError,
)
from app.services.delivery_verification_service import DeliveryVerificationService

# SribeesExpress's external status vocabulary (contract section 8) mapped onto
# our own order statuses. Once a parcel is ready for pickup the courier drives
# the order: their rider collecting it is our "handed to courier", and so on to
# the door. `booked` maps to nothing (booking is what READY_FOR_PICKUP already
# records), and `cancelled`/`returned` are deliberately absent — a courier
# cancelling or returning a parcel must not silently cancel or refund a paid
# order; that is an ops decision with refund consequences.
STATUS_MAP = {
    "picked_up": OrderStatusEnum.HANDED_TO_COURIER,
    "in_transit": OrderStatusEnum.SHIPPED,
    "out_for_delivery": OrderStatusEnum.OUT_FOR_DELIVERY,
    "delivered": OrderStatusEnum.DELIVERED,
    "failed": OrderStatusEnum.DELIVERY_FAILED,
}

# Our statuses in delivery order, for "is this courier update news or history".
_DELIVERY_PROGRESS = (
    "pending",
    "confirmed",
    "processing",
    "packing",
    "packed",
    "ready_for_pickup",
    "handed_to_courier",
    "shipped",
    "out_for_delivery",
    "delivered",
)

# Nothing the courier reports moves an order out of these: it has been
# delivered, cancelled, sent back, or is in a returns/refund conversation.
_SETTLED = frozenset(
    {"delivered", "cancelled", "rto_initiated", "return_requested", "return_approved", "refunded"}
)


def should_mirror(current: str, target: str) -> bool:
    """
    Whether a courier-reported status should move our order from ``current``.

    Forward only. Webhooks arrive out of order and the sweep replays history, so
    a late ``picked_up`` must not pull an order that is already
    ``out_for_delivery`` back to ``handed_to_courier``. Two sideways steps are
    real, though: a failed attempt (``delivery_failed``) from anywhere on the
    road, and a redispatch or late delivery out of it.
    """
    if current == target or current in _SETTLED:
        return False
    if target == OrderStatusEnum.DELIVERY_FAILED.value:
        return current in ("ready_for_pickup", "handed_to_courier", "shipped", "out_for_delivery")
    if current == OrderStatusEnum.DELIVERY_FAILED.value:
        return target in ("out_for_delivery", "delivered")
    if current not in _DELIVERY_PROGRESS or target not in _DELIVERY_PROGRESS:
        return False
    return _DELIVERY_PROGRESS.index(target) > _DELIVERY_PROGRESS.index(current)

# How far back the next sweep re-reads. Covers a row stamped before our query
# ran but committed after it -- see sweep()'s docstring.
CURSOR_REWIND = timedelta(minutes=1)


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

    # The machine-readable half of a failure: True means SribeesExpress can
    # still redispatch, False means it is heading for `returned`. That is the
    # refund-now vs tell-them-tomorrow decision, and it is the only part of a
    # failure we are willing to branch on -- `failure_note` is free text.
    if "will_retry" in payload and payload["will_retry"] != order.courier_will_retry:
        order.courier_will_retry = payload["will_retry"]
        changed = True

    # NULL until this shipment's collected COD has actually been paid out to
    # us. Only ever set, never cleared: a payload that omits it means "not
    # reported here", and clearing a settled remittance would make money look
    # unreceived.
    remittance_id = payload.get("remittance_id")
    if remittance_id is not None and remittance_id != order.courier_remittance_id:
        order.courier_remittance_id = remittance_id
        logger.info(
            f"Order {order.order_number}: COD remitted in run {remittance_id}"
        )
        changed = True

    # Mirror onto our own order status last, so the courier fields are already
    # written when update_status fires its side effects (notifications,
    # cashback crediting).
    mapped = STATUS_MAP.get(status or "")
    if mapped is not None and should_mirror(order.status, mapped.value):
        from app.services.order_service import OrderService

        await OrderService.update_status(
            db, order, mapped, changed_by="SribeesExpress", notes=f"Courier reported {status}"
        )
        changed = True
        # From pickup onwards the courier is what moves the order, so it is
        # also what tells the customer. Best-effort: a failed push must not
        # un-apply a status the courier really reported.
        try:
            from app.services.notification_service import NotificationService

            await NotificationService.notify_order_status(
                db,
                user_id=order.user_id,
                order_number=order.order_number,
                status=mapped.value,
                order_id=order.order_id,
            )
        except Exception as exc:  # pragma: no cover - notification is best-effort
            logger.warning(f"Courier status notification failed for {order.order_number}: {exc}")
    elif changed and commit:
        await db.commit()

    return changed


# The return leg's own external statuses. Note what `delivered` means here:
# **the goods are back with us**, not with the customer. That is the event the
# refund waits for.
_RETURN_COLLECTED_STATUSES = frozenset({"picked_up", "in_transit", "out_for_delivery"})


RETURN_CREATED_EVENT = "shipment.return_created"


async def apply_return_created(db: AsyncSession, order: Order, payload: dict) -> bool:
    """Record a return leg SribeesExpress has raised against one of our orders.

    Two things arrive here, and they are not the same:

    1. **Our own reverse pickup, echoed back.** We already stored the waybill,
       fee and code from the booking response, so this is confirmation. The
       code is re-stored anyway — it is the same one, and a booking response we
       failed to persist is exactly the case this heals.
    2. **A return we did not ask for.** A SribeesExpress rider could not
       deliver the parcel, phoned our branch, and raised the leg from the road.
       Nothing on our side knows about it yet, and this push is the *only*
       delivery of the handover code — without it our counter staff cannot
       release the goods back to the rider when they arrive.

    The second case does not touch the order's own status. The parcel never
    reached the customer, so there is no return *claim* to approve and no
    refund to make; the order is simply coming back, and a Branch Manager sees
    it on the order with the code beside it. Inventing a `return_requested`
    here would put a claim in the returns queue that no customer ever made.
    """
    changed = False

    return_id = payload.get("return_id")
    if return_id and return_id != order.return_waybill_number:
        order.return_waybill_number = return_id
        changed = True

    # The one and only delivery of this value — see the column's comment in
    # models/order.py. Overwrites, because a regenerated code invalidates the
    # old one and a stale code in this column is worse than none.
    code = payload.get("handover_code")
    if code and code != order.return_handover_code:
        order.return_handover_code = code
        changed = True

    fee = payload.get("return_fee_lkr")
    if fee is not None:
        fee_value = Decimal(str(fee))
        if fee_value != order.return_fee:
            order.return_fee = fee_value
            changed = True

    if payload.get("reason") and not order.return_reason:
        order.return_reason = str(payload["reason"])[:255]
        changed = True

    if order.return_booking_status != "booked":
        order.return_booking_status = "booked"
        order.return_booking_error = None
        changed = True

    at = _parse_dt(payload.get("at"))
    if at is not None and at != order.return_tracking_updated_at:
        order.return_tracking_updated_at = at
        changed = True

    if changed:
        await db.commit()
        await db.refresh(order)
        logger.info(
            f"Return {order.return_waybill_number} recorded on {order.order_number} "
            f"(fee {order.return_fee})"
        )
    return changed


async def apply_return_payload(db: AsyncSession, order: Order, payload: dict) -> bool:
    """Write a *return leg's* shipment payload onto ``order``.

    Deliberately separate from :func:`apply_shipment_payload` rather than a
    flag on it. The two describe opposite journeys of the same order and write
    to different columns, and the forward shipment's own tracking is the proof
    that the delivery happened — a return must never overwrite it. Sharing one
    function would mean a `delivered` on the return leg landing in
    `courier_tracking_status`, where every screen reads it as the customer
    receiving the parcel a second time.

    Returns True if anything changed.
    """
    changed = False

    status_value = payload.get("status")
    if status_value and status_value != order.return_tracking_status:
        order.return_tracking_status = status_value
        changed = True

    at = _parse_dt(payload.get("at") or payload.get("updated_at"))
    if at is not None:
        order.return_tracking_updated_at = at
        changed = True

    # First movement on the leg means a rider has the goods. Set once — a
    # later `in_transit` must not restamp the moment of collection.
    if status_value in _RETURN_COLLECTED_STATUSES and order.return_collected_at is None:
        order.return_collected_at = at or datetime.now(timezone.utc)
        history = OrderStatusHistory(
            order_id=order.order_id,
            old_status=order.status,
            new_status=order.status,
            changed_by="SribeesExpress",
            notes=f"Courier collected returned item from customer ({status_value})",
            created_at=order.return_collected_at,
        )
        db.add(history)
        changed = True

    if status_value == "delivered":
        from app.services.order_service import OrderService

        # Commits, credits the wallet and closes the order — idempotently, so
        # a redelivered webhook cannot refund twice.
        await OrderService.complete_return(db, order)
        return True

    if changed:
        await db.commit()
    return changed


async def refresh_return_tracking(db: AsyncSession, order: Order) -> Optional[dict]:
    """
    Pull this order's return leg (contract section 9a.2) and apply it -- which
    on `delivered` credits the refund and closes the order.

    Returns the raw payload, or None if there is no leg or SribeesExpress could
    not be reached.
    """
    if not order.return_waybill_number:
        return None
    try:
        async with courier_credentials.for_branch(db, order.branch_id):
            payload = await CourierService.get_return(order.return_waybill_number)
    except (
        CourierUnavailableError,
        CourierError,
        courier_credentials.CourierNotConfiguredError,
        courier_credentials.DecryptionError,
    ) as exc:
        logger.warning(f"Could not refresh return for order {order.order_number}: {exc}")
        return None

    await apply_return_payload(db, order, payload)
    return payload


async def sweep_open_returns(db: AsyncSession, *, limit: int = 200) -> int:
    """
    Re-read every approved reverse pickup that is still waiting for its goods,
    and settle the ones SribeesExpress says are back. Returns how many were
    refunded.

    The refund waits on one event: the return leg reaching `delivered` when
    our counter gives the rider the handover code. That event arrives as a
    webhook, SribeesExpress does not retry webhooks, and the forward sweep
    above never looks at return legs -- so one lost push used to leave the
    order in QC Pending and the customer unpaid, with nothing to notice.
    """
    from app.models.order import OrderStatus

    rows = (
        await db.execute(
            select(Order)
            .where(
                Order.status == OrderStatus.RETURN_APPROVED.value,
                Order.return_waybill_number.isnot(None),
            )
            .order_by(Order.return_approved_at)
            .limit(limit)
        )
    ).scalars().all()

    refunded = 0
    for order in rows:
        try:
            await refresh_return_tracking(db, order)
        except Exception:
            # One order's bad data must not strand every order behind it.
            await db.rollback()
            logger.exception(f"Return sweep could not settle {order.order_number}")
            continue
        if order.status == OrderStatus.REFUNDED.value:
            refunded += 1
    return refunded


async def refresh_order_tracking(db: AsyncSession, order: Order) -> Optional[dict]:
    """
    Pull one shipment's current state (contract section 8, with ``history``)
    and apply it. Returns the raw payload, or None if SribeesExpress could not
    be reached — the caller shows whatever it already has rather than erroring.
    """
    if not order.courier_shipment_id:
        return None
    try:
        async with courier_credentials.for_branch(db, order.branch_id):
            payload = await CourierService.get_shipment(order.courier_shipment_id)
    except (
        CourierUnavailableError,
        CourierError,
        courier_credentials.CourierNotConfiguredError,
        courier_credentials.DecryptionError,
    ) as exc:
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


async def _sweep_one_account(
    db: AsyncSession,
    target: courier_credentials.CourierTarget,
    since: Optional[datetime],
) -> Tuple[int, int, Optional[datetime]]:
    """One account's shipments since ``since``: (seen, applied, newest updated_at)."""
    high_water: Optional[datetime] = None
    seen = 0
    applied = 0
    offset = 0
    limit = settings.courier_sync_batch_limit

    # Resolved once for the whole account rather than per page: re-deriving the
    # credential on every page would be work with no answer that could change.
    async with courier_credentials.using(target):
        while True:
            rows = await CourierService.list_shipments(
                updated_since=since, limit=limit, offset=offset
            )
            if not rows:
                break

            for row in rows:
                seen += 1
                # Tracked over every row, including ones we do not own: an
                # unmatched shipment still proves the cursor can move past it.
                row_at = _parse_dt(row.get("updated_at"))
                if row_at and (high_water is None or row_at > high_water):
                    high_water = row_at

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
    return seen, applied, high_water


async def sweep(db: AsyncSession) -> Tuple[int, int, int]:
    """
    Reconciliation poll (contract section 7a), across every SribeesExpress
    account we book into. Returns (rows_seen, rows_applied, accounts_swept).

    **Every account, not just the account key.** A branch holding its own key
    is its own SribeesExpress client, and its shipments are invisible to any
    other key. Sweeping only the account key would leave every branch-key
    parcel stuck at "ready for pickup" forever unless a webhook happened to
    arrive — and a COD ``delivered`` is exactly the push that can be lost.

    Pages ``GET /ecommerce/shipments?updated_since=<cursor>`` for each account
    and applies every row to the matching order. The cursor only advances on a
    clean pass over every account: a partial failure re-reads the same window
    next time, which is safe because applying a payload is idempotent, and is
    much better than skipping a ``delivered`` we never saw.

    **The cursor is the newest ``updated_at`` seen — the oldest of those
    across accounts.** It is SribeesExpress's clock, not ours, so no assumption
    about two machines agreeing on the time is involved; and taking the minimum
    means a quiet account cannot drag the cursor past a busy one's rows.
    ``updated_since`` is inclusive (``>=``), so the boundary is re-read, which
    is cheap and idempotent.

    Two guards remain:

    * A short rewind on the next read. A row can be *stamped* before our query
      runs but only become *visible* after it commits.
    * A fallback to our own start-of-run clock for an account whose pages carry
      no ``updated_at`` at all, so the cursor cannot freeze.
    """
    # Captured BEFORE the first request, for the fallback path only: using the
    # time the sweep *finished* would silently skip anything updated mid-run.
    started_at = datetime.now(timezone.utc)
    cursor = await _get_cursor(db)
    since = (cursor - CURSOR_REWIND) if cursor else None

    targets = await courier_credentials.all_targets(db)
    if not targets:
        await _set_cursor(db, synced_at=None, error="No SribeesExpress account is configured")
        return 0, 0, 0

    seen = applied = 0
    marks: list[datetime] = []
    for target in targets:
        try:
            account_seen, account_applied, high_water = await _sweep_one_account(db, target, since)
        except (CourierUnavailableError, CourierError) as exc:
            await db.rollback()
            logger.error(
                f"Courier reconciliation sweep failed for the {target.key_source} account "
                f"({target.environment}): {exc}"
            )
            await _set_cursor(db, synced_at=None, error=str(exc)[:500])
            return seen, applied, len(targets)
        seen += account_seen
        applied += account_applied
        marks.append(high_water or started_at)

    await _set_cursor(db, synced_at=min(marks), error=None)
    logger.info(
        f"Courier reconciliation sweep: {seen} shipment(s) seen, {applied} applied "
        f"across {len(targets)} account(s)"
    )
    return seen, applied, len(targets)
