"""
New-order alerts for staff phones (the Sribees New Orders app).

An order rings the phones of its branch's staff and of every super admin the
moment it becomes workable: at checkout for COD / wallet, and when the
payment lands for a card order. An order still ``awaiting_payment`` never
rings -- nobody should pack an order that may never be paid.

Owner rules (2026-11-10):
  * recipients = active super admins + active admins of the order's branch
    who hold ``orders:read``;
  * its own Android channel and sound, so a new order is told apart from
    anything else on the phone;
  * an alert must never cost an order: everything here runs after the order
    is committed, on its own session, and only logs on failure.

Send-once: ``admin_order_alerts`` has one row per order. Only the call that
inserts it sends, so a retried payment callback or the reconciliation sweep
reaching the same hook rings nobody twice.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import func, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin import Admin, AdminRole
from app.models.admin_push import AdminOrderAlert, AdminPushToken
from app.models.order import Order, OrderItem, OrderStatus

# The New Orders app creates this channel (importance max, raw sound
# ``new_order``). Change all three together.
STAFF_CHANNEL_ID = "new_orders"
STAFF_SOUND = "new_order"
STAFF_TITLE = "Sribees New Orders"
STAFF_LANGUAGES = ("si", "en")

# Workable = staff can act on it. Everything before (awaiting_payment) and
# after a cancel is silent.
_WORKABLE = {OrderStatus.PENDING.value, OrderStatus.CONFIRMED.value}

_COD = "COD"


# What a phone reports about itself on every registration (migration 113).
HEALTH_FIELDS = (
    "notifications_on",
    "channel_state",
    "background_restricted",
    "app_version",
    "device_model",
)
CHANNEL_STATES = ("on", "silent", "off")
# A phone re-registers every time the app opens; one unseen this long has
# probably been uninstalled or switched off.
PHONE_STALE_AFTER = timedelta(days=7)


def phone_problem(token: AdminPushToken, now: datetime) -> Optional[str]:
    """
    Why this phone would not ring, first problem first (the order the app
    fixes them in), or None when it can. "unknown" = an app too old to say.
    """
    seen = token.last_seen_at
    if seen is not None and seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    if seen is not None and now - seen > PHONE_STALE_AFTER:
        return "not_seen"
    if token.notifications_on is None and token.channel_state is None:
        return "unknown"
    if token.notifications_on is False:
        return "notifications_off"
    if token.channel_state == "off":
        return "channel_off"
    if token.channel_state == "silent":
        return "channel_silent"
    if token.background_restricted is True:
        return "battery_restricted"
    return None


def staff_language(value: Optional[str]) -> str:
    """The app speaks Sinhala (default) and English only."""
    code = (value or "").strip().lower()[:2]
    return code if code in STAFF_LANGUAGES else "si"


def _money(amount) -> str:
    return f"{float(amount or 0):,.2f}"


def render_alert(
    lang: str,
    *,
    order_number: str,
    item_count: int,
    total,
    is_cod: bool,
    branch_name: Optional[str],
) -> str:
    """One line, everything a packer needs before opening the app."""
    if lang == "en":
        parts = [
            f"New order #{order_number}",
            f"{item_count} item" + ("" if item_count == 1 else "s"),
            f"Rs. {_money(total)}",
            "COD" if is_cod else "Paid",
        ]
    else:
        parts = [
            f"නව ඇණවුම #{order_number}",
            f"භාණ්ඩ {item_count}",
            f"රු. {_money(total)}",
            "භාරදීමේදී ගෙවීම" if is_cod else "ගෙවා ඇත",
        ]
    if branch_name:
        parts.append(branch_name)
    return " · ".join(parts)


# Ring until accepted (owner, 2026-11-11).
RING_EVERY = timedelta(minutes=2)
RING_WINDOW = timedelta(minutes=30)
MAX_RINGS = 15
QUIET_FROM_HOUR = 22
QUIET_UNTIL_HOUR = 7
COLOMBO = ZoneInfo("Asia/Colombo")
SWEEP_BATCH = 50
SWEEP_SECONDS = 30


def alert_tag(order_id) -> str:
    """The Android notification tag: one notification per order on a phone."""
    return f"new_order:{order_id}"


def _quiet(at: datetime) -> bool:
    hour = at.astimezone(COLOMBO).hour
    return hour >= QUIET_FROM_HOUR or hour < QUIET_UNTIL_HOUR


def _next_morning(at: datetime) -> datetime:
    """The next 07:00 in Colombo at or after ``at``."""
    local = at.astimezone(COLOMBO)
    morning = local.replace(hour=QUIET_UNTIL_HOUR, minute=0, second=0, microsecond=0)
    if morning < local:
        morning += timedelta(days=1)
    return morning.astimezone(timezone.utc)


def _is_morning_window(window_started_at: datetime) -> bool:
    """The window opened by the 07:00 restart (07:00:00 Colombo exactly)."""
    local = window_started_at.astimezone(COLOMBO)
    return (local.hour, local.minute, local.second, local.microsecond) == (QUIET_UNTIL_HOUR, 0, 0, 0)


def plan_next_ring(
    now: datetime, *, window_started_at: datetime, rings: int
) -> Tuple[Optional[datetime], datetime, int]:
    """
    After ring number ``rings`` of the current window at ``now``: when to ring
    next, the window it belongs to, and the ring count to store.

    Every 2 minutes while the window (30 min, 15 rings) lasts; a ring that
    would fall in the night moves to 07:00 Colombo and opens a new window
    there. A day window that ends with the order still not accepted gets one
    more window at the next 07:00 (owner: at 07:00 every still-pending order
    rings again); a morning window that ends stops for good. Returns
    ``(None, ...)`` when ringing should stop.
    """
    candidate = now + RING_EVERY
    if rings >= MAX_RINGS or candidate > window_started_at + RING_WINDOW:
        if _is_morning_window(window_started_at):
            return None, window_started_at, rings
        morning = _next_morning(now)
        return morning, morning, 0
    if _quiet(candidate):
        morning = _next_morning(candidate)
        return morning, morning, 0
    return candidate, window_started_at, rings


class StaffOrderAlertService:
    """Ring staff phones for a newly workable order."""

    @staticmethod
    def schedule(order_id: UUID) -> None:
        """
        Fire and forget, after the order's commit. The checkout response does
        not wait for FCM, and an alert failure cannot reach the customer.
        """
        from app.services.notification_service import _spawn

        _spawn(StaffOrderAlertService._run(order_id))

    @staticmethod
    async def _run(order_id: UUID) -> None:
        from app.config.database import async_session_maker

        try:
            async with async_session_maker() as session:
                await StaffOrderAlertService.notify_new_order(session, order_id)
        except Exception:
            logger.exception(f"Staff new-order alert for {order_id} failed; the order is fine")

    # ------------------------------------------------------------------ core

    @staticmethod
    async def recipients(db: AsyncSession, branch_id: Optional[UUID]) -> List[Tuple[str, str]]:
        """(token, language) for every device that should ring for this branch."""
        from app.services.permission_service import PermissionService

        rows = (
            await db.execute(
                select(Admin, AdminPushToken.token, AdminPushToken.language)
                .join(AdminPushToken, AdminPushToken.admin_id == Admin.admin_id)
                .where(Admin.is_active.is_(True), AdminPushToken.is_active.is_(True))
            )
        ).all()

        allowed: Dict[UUID, bool] = {}
        out: List[Tuple[str, str]] = []
        for admin, token, language in rows:
            if admin.admin_id not in allowed:
                if admin.role == AdminRole.SUPER_ADMIN:
                    ok = True
                elif branch_id is None or admin.branch_id != branch_id:
                    ok = False
                else:
                    perms = await PermissionService.get_effective_permissions(db, admin)
                    ok = ("orders", "read") in perms
                allowed[admin.admin_id] = ok
            if allowed[admin.admin_id]:
                out.append((token, staff_language(language)))
        return out

    @staticmethod
    async def notify_new_order(db: AsyncSession, order_id: UUID) -> int:
        """
        Send the alert for ``order_id`` if it is workable and not sent before.
        Returns how many devices FCM accepted (0 when skipped).
        """

        # The latch, taken only while the order is workable -- checked in the
        # same statement, so a cancel landing in between cannot ring. Committed
        # before sending: two racing callers cannot both get a row back, and a
        # crash after the send cannot cause a re-send. A row whose
        # ``recipients`` is still NULL is an alert that was claimed but never
        # finished (FCM down, process restarted): visible, not silent.
        # The schedule is written with the claim: if the first ring then fails
        # (or the process dies), the sweep still rings it 2 minutes later.
        claimed = await db.scalar(
            pg_insert(AdminOrderAlert)
            .from_select(
                ["order_id", "alert_status", "rings", "window_started_at", "next_ring_at"],
                select(
                    Order.order_id,
                    Order.status,
                    literal(1),
                    func.now(),
                    func.now() + RING_EVERY,
                ).where(Order.order_id == order_id, Order.status.in_(_WORKABLE)),
            )
            .on_conflict_do_nothing(index_elements=["order_id"])
            .returning(AdminOrderAlert.order_id)
        )
        await db.commit()
        if claimed is None:
            return 0
        order = await db.get(Order, order_id)

        alert = await db.get(AdminOrderAlert, order_id)
        devices, accepted = await StaffOrderAlertService._ring(db, order)
        # Night: the claim's "2 minutes later" becomes 07:00.
        next_at, window, rings = plan_next_ring(
            alert.window_started_at, window_started_at=alert.window_started_at, rings=1
        )
        # Only while the row is still as the claim left it: a sweep that cleared
        # it (accepted meanwhile) or already rang it again must not be undone.
        await db.execute(
            update(AdminOrderAlert)
            .where(
                AdminOrderAlert.order_id == order_id,
                AdminOrderAlert.cleared_at.is_(None),
                AdminOrderAlert.stopped_at.is_(None),
                AdminOrderAlert.rings == 1,
            )
            .values(
                devices=devices,
                recipients=accepted,
                rings=rings,
                window_started_at=window,
                next_ring_at=next_at,
                stopped_at=None if next_at else func.now(),
            )
        )
        await db.commit()
        logger.info(f"Order {order.order_number}: staff alert to {accepted}/{devices} devices")
        return accepted

    @staticmethod
    async def _ring(db: AsyncSession, order: Order) -> Tuple[int, int]:
        """One ring of ``order`` on every staff phone that should hear it.
        Returns (devices, accepted by FCM). Dead tokens are switched off."""
        from app.services.fcm_service import FCMService

        devices = await StaffOrderAlertService.recipients(db, order.branch_id)
        if not devices:
            logger.info(f"Order {order.order_number}: no staff device registered for its branch")
            return 0, 0

        item_count = int(
            await db.scalar(
                select(func.count()).select_from(OrderItem).where(OrderItem.order_id == order.order_id)
            )
            or 0
        )
        branch_name = None
        if order.branch_id is not None:
            from app.models.branch import Branch

            branch = await db.get(Branch, order.branch_id)
            branch_name = branch.name if branch else None

        data = {
            "type": "new_order",
            "order_id": str(order.order_id),
            "order_number": order.order_number,
            "branch_id": str(order.branch_id) if order.branch_id else "",
        }
        is_cod = (order.payment_method or "").upper() == _COD

        by_lang: Dict[str, List[str]] = {}
        for token, lang in devices:
            by_lang.setdefault(lang, []).append(token)

        accepted = 0
        dead: List[str] = []
        for lang, tokens in by_lang.items():
            body = render_alert(
                lang,
                order_number=order.order_number,
                item_count=item_count,
                total=order.total_amount,
                is_cod=is_cod,
                branch_name=branch_name,
            )
            result = await FCMService.send_to_tokens(
                tokens,
                STAFF_TITLE,
                body,
                data=data,
                channel_id=STAFF_CHANNEL_ID,
                sound=STAFF_SOUND,
                # One notification per order: each ring replaces the last.
                tag=alert_tag(order.order_id),
            )
            accepted += int(result.get("success_count", 0))
            dead.extend(result.get("invalid_tokens", []))

        if dead:
            await db.execute(
                update(AdminPushToken)
                .where(AdminPushToken.token.in_(dead))
                .values(is_active=False)
            )
        return len(devices), accepted

    # ------------------------------------------------------- ring until accepted

    @staticmethod
    async def sweep(db: AsyncSession) -> int:
        """
        One pass: clear the phones for every order someone accepted, then
        re-ring every unaccepted order that is due. Returns how many rang.

        Safe with several API workers: rows are locked (SKIP LOCKED) and marked
        before anything is sent, so a due ring or a clear goes out once. A
        crash after the commit loses that one ring rather than doubling it.
        """
        from app.services.fcm_service import FCMService

        now = await db.scalar(select(func.now()))

        # 1. Accepted orders: tell the phones, whenever it happened -- in the
        # night, after the ringing window ended, it does not matter.
        accepted_rows = (
            await db.execute(
                select(AdminOrderAlert)
                .join(Order, Order.order_id == AdminOrderAlert.order_id)
                .where(
                    AdminOrderAlert.alert_status.isnot(None),
                    AdminOrderAlert.cleared_at.is_(None),
                    Order.status != AdminOrderAlert.alert_status,
                )
                .limit(SWEEP_BATCH)
                .with_for_update(of=AdminOrderAlert, skip_locked=True)
            )
        ).scalars().all()
        cleared: List[UUID] = []
        for alert in accepted_rows:
            alert.cleared_at = now
            alert.stopped_at = alert.stopped_at or now
            alert.next_ring_at = None
            cleared.append(alert.order_id)
        await db.commit()
        for order_id in cleared:
            try:
                await StaffOrderAlertService._clear(db, order_id, FCMService)
            except Exception:
                await db.rollback()
                logger.exception(f"Clearing the alert for order {order_id} failed")

        # 2. Due re-rings.
        rows = (
            await db.execute(
                select(AdminOrderAlert, Order.status)
                .join(Order, Order.order_id == AdminOrderAlert.order_id)
                .where(
                    AdminOrderAlert.stopped_at.is_(None),
                    AdminOrderAlert.next_ring_at.isnot(None),
                    AdminOrderAlert.next_ring_at <= now,
                )
                .order_by(AdminOrderAlert.next_ring_at)
                .limit(SWEEP_BATCH)
                .with_for_update(of=AdminOrderAlert, skip_locked=True)
            )
        ).all()
        to_ring: List[Tuple[UUID, str]] = []
        for alert, status in rows:
            expected = alert.alert_status or OrderStatus.PENDING.value
            if status != expected:
                # Accepted: never leave a row due that was not rung (it would
                # sit at the head of the queue forever). Step 1 sends the clear.
                alert.stopped_at = now
                alert.next_ring_at = None
                continue
            # Step from when it was due, not from when this sweep ran, so a
            # sweep running late does not stretch the 2-minute rhythm.
            due = alert.next_ring_at
            base = due if due is not None and now - due < RING_EVERY else now
            next_at, window, rings = plan_next_ring(
                base, window_started_at=alert.window_started_at or base, rings=alert.rings + 1
            )
            alert.rings = rings
            alert.window_started_at = window
            alert.next_ring_at = next_at
            if next_at is None:
                alert.stopped_at = now
            to_ring.append((alert.order_id, expected))
        await db.commit()

        rang = 0
        for order_id, expected in to_ring:
            try:
                db.expire_all()
                order = await db.get(Order, order_id)
                # Accepted between the lock and now: do not ring a done order.
                if order is None or order.status != expected:
                    continue
                _, ok = await StaffOrderAlertService._ring(db, order)
                await db.commit()
                rang += 1 if ok else 0
            except Exception:
                await db.rollback()
                logger.exception(f"Re-ringing order {order_id} failed")
        return rang

    @staticmethod
    async def _clear(db: AsyncSession, order_id: UUID, fcm) -> None:
        """Tell the phones the order was accepted, so they drop its notification."""
        order = await db.get(Order, order_id)
        if order is None:
            return
        tokens = [t for t, _ in await StaffOrderAlertService.recipients(db, order.branch_id)]
        if tokens:
            await fcm.send_data_to_tokens(
                tokens,
                {"type": "order_accepted", "order_id": str(order_id), "tag": alert_tag(order_id)},
            )

    # --------------------------------------------------------------- devices

    @staticmethod
    async def register_device(
        db: AsyncSession,
        admin_id: UUID,
        *,
        token: str,
        device_id: str,
        platform: Optional[str],
        language: Optional[str],
        health: Optional[Dict[str, object]] = None,
    ) -> AdminPushToken:
        """
        Upsert by token and bind it to ``admin_id``. One install (device_id)
        carries one live token: its older tokens are dropped, whoever held them.

        ``health`` is what the phone says about itself (see HEALTH_FIELDS);
        written as sent every time, so an older app that sends none reads as
        "unknown" rather than keeping a stale answer.
        """
        lang = staff_language(language)
        health_values = {f: (health or {}).get(f) for f in HEALTH_FIELDS}
        # One install registers at a time: without this, two racing calls from
        # the same device (token refresh + login) could each miss the other's
        # row and leave two live tokens. Released at commit.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"admin-push:{device_id}"}
        )
        await db.execute(
            AdminPushToken.__table__.delete().where(
                AdminPushToken.device_id == device_id,
                AdminPushToken.token != token,
            )
        )
        now = func.now()
        stmt = pg_insert(AdminPushToken).values(
            admin_id=admin_id,
            token=token,
            device_id=device_id,
            platform=platform,
            language=lang,
            is_active=True,
            **health_values,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["token"],
            set_={
                "admin_id": admin_id,
                "device_id": device_id,
                "platform": platform,
                "language": lang,
                "is_active": True,
                "updated_at": now,
                "last_seen_at": now,
                **health_values,
            },
        )
        await db.execute(stmt)
        await db.commit()
        return (
            await db.execute(select(AdminPushToken).where(AdminPushToken.token == token))
        ).scalar_one()

    @staticmethod
    async def staff_phones(db: AsyncSession, viewer: Admin) -> Dict[str, list]:
        """
        The staff phones registered for alerts and whether each can ring, plus
        the staff who would be rung but registered no phone.

        A super admin sees everyone; a branch manager only their own branch.
        """
        from app.models.branch import Branch
        from app.services.permission_service import PermissionService

        own_branch = None if viewer.role == AdminRole.SUPER_ADMIN else viewer.branch_id
        names = {
            b.branch_id: b.name for b in (await db.execute(select(Branch))).scalars().all()
        }

        admins = (
            await db.execute(select(Admin).where(Admin.is_active.is_(True)))
        ).scalars().all()
        rung: Dict[UUID, Admin] = {}
        for a in admins:
            if a.role == AdminRole.SUPER_ADMIN:
                if own_branch is None:
                    rung[a.admin_id] = a
            elif a.branch_id is not None and (own_branch is None or a.branch_id == own_branch):
                perms = await PermissionService.get_effective_permissions(db, a)
                if ("orders", "read") in perms:
                    rung[a.admin_id] = a

        tokens = (
            await db.execute(
                select(AdminPushToken)
                .where(AdminPushToken.is_active.is_(True), AdminPushToken.admin_id.in_(list(rung)))
                .order_by(AdminPushToken.last_seen_at.desc())
            )
        ).scalars().all()

        now = datetime.now(timezone.utc)
        phones = []
        for t in tokens:
            a = rung[t.admin_id]
            problem = phone_problem(t, now)
            phones.append({
                "device_id": t.device_id,
                "admin_id": str(a.admin_id),
                "full_name": a.full_name,
                "role": a.role.value,
                "branch_id": str(a.branch_id) if a.branch_id else None,
                "branch_name": names.get(a.branch_id),
                "platform": t.platform,
                "device_model": t.device_model,
                "app_version": t.app_version,
                "notifications_on": t.notifications_on,
                "channel_state": t.channel_state,
                "background_restricted": t.background_restricted,
                "last_seen_at": t.last_seen_at.isoformat() if t.last_seen_at else None,
                "can_ring": None if problem == "unknown" else problem is None,
                "problem": problem,
            })

        with_phone = {t.admin_id for t in tokens}
        without = [
            {
                "admin_id": str(a.admin_id),
                "full_name": a.full_name,
                "role": a.role.value,
                "branch_id": str(a.branch_id) if a.branch_id else None,
                "branch_name": names.get(a.branch_id),
            }
            for a in sorted(rung.values(), key=lambda x: x.full_name.lower())
            if a.admin_id not in with_phone
        ]
        return {"phones": phones, "staff_without_phone": without}

    @staticmethod
    async def unregister_device(db: AsyncSession, admin_id: UUID, device_id: str) -> int:
        """Logout: this admin's rows for this install stop ringing."""
        result = await db.execute(
            update(AdminPushToken)
            .where(AdminPushToken.admin_id == admin_id, AdminPushToken.device_id == device_id)
            .values(is_active=False)
        )
        await db.commit()
        return result.rowcount or 0


async def run_staff_alert_ring_worker():
    """Re-ring unaccepted orders every 2 minutes (see plan_next_ring)."""
    import asyncio

    from app.config.database import async_session_maker

    while True:
        try:
            async with async_session_maker() as db:
                rang = await StaffOrderAlertService.sweep(db)
                if rang:
                    logger.info(f"Staff alerts re-rang for {rang} order(s)")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Staff alert ring sweep failed; check migration 110")
        await asyncio.sleep(SWEEP_SECONDS)
