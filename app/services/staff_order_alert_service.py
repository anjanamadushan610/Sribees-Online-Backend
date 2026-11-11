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

from typing import Dict, List, Optional, Tuple
from uuid import UUID

from loguru import logger
from sqlalchemy import func, select, text, update
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
        from app.services.fcm_service import FCMService

        # The latch, taken only while the order is workable -- checked in the
        # same statement, so a cancel landing in between cannot ring. Committed
        # before sending: two racing callers cannot both get a row back, and a
        # crash after the send cannot cause a re-send. A row whose
        # ``recipients`` is still NULL is an alert that was claimed but never
        # finished (FCM down, process restarted): visible, not silent.
        claimed = await db.scalar(
            pg_insert(AdminOrderAlert)
            .from_select(
                ["order_id"],
                select(Order.order_id).where(
                    Order.order_id == order_id, Order.status.in_(_WORKABLE)
                ),
            )
            .on_conflict_do_nothing(index_elements=["order_id"])
            .returning(AdminOrderAlert.order_id)
        )
        await db.commit()
        if claimed is None:
            return 0
        order = await db.get(Order, order_id)

        devices = await StaffOrderAlertService.recipients(db, order.branch_id)
        if not devices:
            await db.execute(
                update(AdminOrderAlert)
                .where(AdminOrderAlert.order_id == order_id)
                .values(devices=0, recipients=0)
            )
            await db.commit()
            logger.info(f"Order {order.order_number}: no staff device registered for its branch")
            return 0

        item_count = int(
            await db.scalar(
                select(func.count()).select_from(OrderItem).where(OrderItem.order_id == order_id)
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
            )
            accepted += int(result.get("success_count", 0))
            dead.extend(result.get("invalid_tokens", []))

        if dead:
            await db.execute(
                update(AdminPushToken)
                .where(AdminPushToken.token.in_(dead))
                .values(is_active=False)
            )
        await db.execute(
            update(AdminOrderAlert)
            .where(AdminOrderAlert.order_id == order_id)
            .values(devices=len(devices), recipients=accepted)
        )
        await db.commit()
        logger.info(f"Order {order.order_number}: staff alert to {accepted}/{len(devices)} devices")
        return accepted

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
    ) -> AdminPushToken:
        """
        Upsert by token and bind it to ``admin_id``. One install (device_id)
        carries one live token: its older tokens are dropped, whoever held them.
        """
        lang = staff_language(language)
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
            },
        )
        await db.execute(stmt)
        await db.commit()
        return (
            await db.execute(select(AdminPushToken).where(AdminPushToken.token == token))
        ).scalar_one()

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
