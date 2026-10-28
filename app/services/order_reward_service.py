"""
SRIBEESonline — what an order earns, and when it becomes the customer's.

An order's cashback used to reach the wallet the moment it was placed. The
customer could spend it that day and then cancel, or return the goods, and be
clawed back into a negative balance -- and a return never took the cashback
back at all. Now the reward waits:

    placed / paid ─► pending ─► (delivered: vests_at = delivery + window + 1 min)
                        │                            │
                  cancelled ─► reversed     return refunded ─► amount lowered
                                                     │
                                  window closed, no open return ─► settled
                                  (cashback into the wallet, points added)

It is the rule referral commissions already follow (migration 058), and it
settles one minute after the customer's own return deadline so the two can
never race.

``orders.cashback_credited_at`` stays the single fact of "this order's
cashback is in the wallet". Settling checks it before crediting, which is what
lets orders credited at checkout under the old rule earn their points without
being paid twice, and what makes a replayed settlement harmless.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.loyalty import OrderReward, OrderRewardStatus
from app.models.order import Order, OrderStatus
from app.models.wallet import WalletTransactionType
from app.services.finance_outbox import WalletReason
from app.services.loyalty_service import IssuedReward, LoyaltyService
from app.services.wallet_service import WalletService

# Settles a minute past the customer's own return deadline, so a return that
# lands on the last second still wins.
SETTLE_AFTER_WINDOW = timedelta(minutes=1)

# An order in one of these states has nothing left that could take goods
# back. RETURN_REQUESTED / RETURN_APPROVED wait for the return to finish.
SETTLEABLE_STATUSES = (OrderStatus.DELIVERED.value, OrderStatus.REFUNDED.value)

OPEN_RETURN_STATUSES = (
    OrderStatus.RETURN_REQUESTED.value,
    OrderStatus.RETURN_APPROVED.value,
)


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class SettledReward:
    """What one settlement did, for the notifications sent after commit."""

    user_id: UUID
    order_number: Optional[str]
    cashback_credited: Decimal
    points_added: int
    lifetime_points: int
    tiers_reached: list = field(default_factory=list)
    rewards_issued: list[IssuedReward] = field(default_factory=list)


class OrderRewardService:
    """The lifecycle of an order's cashback and points."""

    # ------------------------------------------------------------------
    # Accrual and scheduling
    # ------------------------------------------------------------------

    @staticmethod
    async def accrue_for_order(db: AsyncSession, order: Order) -> None:
        """
        Record what this order will earn, held until it settles.

        Called when the order becomes real money: at placement for COD and
        wallet orders, on payment confirmation for card orders (an unpaid card
        order is not a purchase). Every order gets a row, even one with no
        cashback, because points are earned on its value.

        ``ON CONFLICT DO NOTHING`` on the order: a Marx confirm racing the
        reconciliation sweep both land here, and only one row can exist.
        Staged in the caller's transaction.
        """
        # The order row may still be only in the session; the reward points at it.
        await db.flush()
        await db.execute(
            pg_insert(OrderReward)
            .values(
                order_id=order.order_id,
                order_number=order.order_number,
                user_id=order.user_id,
                cashback_amount=max(_money(order.cashback_earned), Decimal("0")),
                status=OrderRewardStatus.PENDING,
            )
            .on_conflict_do_nothing(index_elements=["order_id"])
        )

    @staticmethod
    async def schedule_for_order(
        db: AsyncSession, order_id: UUID, delivered_at: datetime
    ) -> None:
        """
        Start the clock: the reward settles once the return window closes.

        Measured from the delivery, not from now, for the reason the referral
        clock is -- an ops correction marking a parcel delivered two days late
        must not restart the customer's window.
        """
        from app.services.order_service import RETURN_WINDOW

        result = await db.execute(
            select(OrderReward).where(
                OrderReward.order_id == order_id,
                OrderReward.status == OrderRewardStatus.PENDING,
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            row.vests_at = _aware(delivered_at) + RETURN_WINDOW + SETTLE_AFTER_WINDOW

    # ------------------------------------------------------------------
    # Returns and cancellation
    # ------------------------------------------------------------------

    @staticmethod
    def returned_cashback(order: Order) -> Decimal:
        """
        The cashback the returned goods earned, at the rate each line was
        bought at (``order_items.cashback_percentage``, migration 065).

        No ``return_items`` means the whole order came back. A line without a
        frozen rate predates 065; then the order's cashback is shared out in
        proportion to the value returned, the only fair split left.
        """
        items = list(order.items or [])
        earned = _money(getattr(order, "cashback_earned", None))
        if not items or earned <= 0:
            return Decimal("0.00")

        if not order.return_items:
            returned = [(item, item.quantity) for item in items]
        else:
            by_id = {str(i.order_item_id): i for i in items}
            returned = []
            for line in order.return_items:
                item = by_id.get(str(line.get("order_item_id")))
                if item is None:
                    continue
                qty = min(int(line.get("quantity", 0)), item.quantity)
                if qty > 0:
                    returned.append((item, qty))

        if not returned:
            return Decimal("0.00")

        if all(getattr(item, "cashback_percentage", None) is not None for item, _ in returned):
            total = sum(
                Decimal(str(item.unit_price)) * qty * Decimal(str(item.cashback_percentage)) / 100
                for item, qty in returned
            )
        else:
            value = sum(Decimal(str(item.unit_price)) * qty for item, qty in returned)
            basis = sum(Decimal(str(i.unit_price)) * i.quantity for i in items)
            total = earned * value / basis if basis > 0 else Decimal("0")

        return min(_money(total), earned)

    @staticmethod
    async def reduce_for_return(db: AsyncSession, order: Order) -> Decimal:
        """
        Take the returned goods' cashback off the order. Staged in the
        caller's transaction; called once per return, from the refund.

        Normally the reward is still pending -- the window cannot close while
        a return is open -- so this only lowers what will be paid. If the
        cashback is already in the wallet (credited at checkout under the old
        rule, or a return that raced the settlement), the difference comes back
        out, and only the difference. Points are left alone: they never go down.
        """
        returned = OrderRewardService.returned_cashback(order)
        if returned <= 0:
            return Decimal("0.00")

        result = await db.execute(
            select(OrderReward)
            .where(OrderReward.order_id == order.order_id)
            .with_for_update()
        )
        row = result.scalar_one_or_none()
        if row is not None and row.status == OrderRewardStatus.REVERSED:
            return Decimal("0.00")

        current = _money(row.cashback_amount if row is not None else order.cashback_earned)
        remaining = max(current - returned, Decimal("0.00"))
        difference = current - remaining
        if difference <= 0:
            return Decimal("0.00")

        if row is not None:
            row.cashback_amount = remaining
        order.cashback_earned = remaining

        if order.cashback_credited_at is not None:
            await WalletService.apply_transaction(
                db,
                user_id=order.user_id,
                tx_type=WalletTransactionType.SPENT,
                amount=difference,
                title="Cashback adjusted — items returned",
                reason=WalletReason.CASHBACK_REVERSAL,
                order_id=order.order_id,
                order_number=order.order_number,
            )

        logger.info(
            f"Cashback on {order.order_number} reduced by {difference} for returned goods"
        )
        return difference

    @staticmethod
    async def reverse_for_order(db: AsyncSession, order: Order, *, reason: str) -> None:
        """
        A cancelled order earns nothing. Staged in the caller's transaction.

        A pending reward is simply voided -- nothing was paid, so nothing comes
        back and the wallet is untouched. Cashback already in the wallet
        (orders credited at checkout under the old rule) is clawed back as
        before, and may take the wallet negative: the ledger records the debt.
        Points stay; the owner's rule is that they never go down.
        """
        result = await db.execute(
            select(OrderReward)
            .where(OrderReward.order_id == order.order_id)
            .with_for_update()
        )
        row = result.scalar_one_or_none()
        if row is not None and row.status != OrderRewardStatus.REVERSED:
            row.status = OrderRewardStatus.REVERSED
            row.reversed_at = datetime.now(timezone.utc)

        credited = _money(order.cashback_earned)
        if order.cashback_credited_at is not None and credited > 0:
            await WalletService.apply_transaction(
                db,
                user_id=order.user_id,
                tx_type=WalletTransactionType.SPENT,
                amount=credited,
                title="Order cancellation cashback reversal",
                reason=WalletReason.CASHBACK_REVERSAL,
                order_id=order.order_id,
                order_number=order.order_number,
                notes=f"{reason} (balance may go negative)",
            )
            logger.info(f"Cashback clawback {credited} for cancelled order {order.order_number}")

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    @staticmethod
    def qualifying_amount(order: Order) -> Decimal:
        """
        The value points are earned on: what the customer paid for goods,
        without delivery -- subtotal less the coupon discount, plus tax, however
        it was paid (card, cash or wallet). Goods that came back are taken off
        at the value they were refunded.
        """
        value = (
            _money(order.subtotal)
            - _money(order.discount_amount)
            + _money(order.tax_amount)
        )
        if order.status == OrderStatus.REFUNDED.value:
            value -= _money(order.refund_amount)
        return max(_money(value), Decimal("0.00"))

    @staticmethod
    async def _settle(
        db: AsyncSession, reward: OrderReward, order: Order
    ) -> SettledReward:
        credited = Decimal("0.00")
        amount = _money(reward.cashback_amount)
        if amount > 0 and order.cashback_credited_at is None:
            tx = await WalletService.apply_transaction(
                db,
                user_id=order.user_id,
                tx_type=WalletTransactionType.EARNED,
                amount=amount,
                title="Order cashback",
                reason=WalletReason.CASHBACK_CREDIT,
                order_id=order.order_id,
                order_number=order.order_number,
            )
            reward.wallet_transaction_id = tx.transaction_id
            order.cashback_credited_at = datetime.now(timezone.utc)
            credited = amount

        qualifying = OrderRewardService.qualifying_amount(order)
        points = await LoyaltyService.add_spend(db, reward.user_id, qualifying)
        issued = await LoyaltyService.issue_level_rewards(
            db, reward.user_id, points.lifetime_points
        )

        reward.qualifying_amount = qualifying
        reward.points_awarded = points.points_added
        reward.status = OrderRewardStatus.SETTLED
        reward.settled_at = datetime.now(timezone.utc)

        return SettledReward(
            user_id=reward.user_id,
            order_number=order.order_number,
            cashback_credited=credited,
            points_added=points.points_added,
            lifetime_points=points.lifetime_points,
            tiers_reached=points.tiers_reached,
            rewards_issued=issued,
        )

    @staticmethod
    async def settle_due(db: AsyncSession, *, limit: int = 200) -> list[SettledReward]:
        """
        Settle every reward whose window has closed on an order with no
        return open.

        The reward AND its order are locked ``FOR UPDATE SKIP LOCKED``: two
        replicas cannot settle the same row, and an order someone is changing
        right now (a return being requested) is left for the next pass rather
        than settled underneath them.

        Each row settles in its own savepoint. One bad row is logged and left
        pending; it must not hold back everyone else's money every minute.
        """
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(OrderReward, Order)
            .join(Order, Order.order_id == OrderReward.order_id)
            .where(
                OrderReward.status == OrderRewardStatus.PENDING,
                OrderReward.vests_at.isnot(None),
                OrderReward.vests_at <= now,
                Order.status.in_(SETTLEABLE_STATUSES),
            )
            .order_by(OrderReward.vests_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        pairs = list(result.all())

        settled: list[SettledReward] = []
        for reward, order in pairs:
            # Read before the attempt: a rolled-back savepoint expires the
            # row, and touching it afterwards would need a query.
            order_number = order.order_number
            try:
                async with db.begin_nested():
                    settled.append(await OrderRewardService._settle(db, reward, order))
            except Exception:
                logger.exception(
                    f"Could not settle the reward on {order_number}; it stays "
                    "pending and is retried on the next pass"
                )

        if pairs:
            await db.commit()
        if settled:
            logger.info(f"{len(settled)} order reward(s) settled")
        return settled

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @staticmethod
    async def pending_for_user(db: AsyncSession, user_id: UUID) -> list[dict]:
        """
        This customer's cashback on its way, newest first, with where each one
        is. ``stage`` is decided here so the app shows a word, not a rule.
        """
        result = await db.execute(
            select(OrderReward, Order.status)
            .join(Order, Order.order_id == OrderReward.order_id)
            .where(
                OrderReward.user_id == user_id,
                OrderReward.status == OrderRewardStatus.PENDING,
            )
            .order_by(OrderReward.created_at.desc())
        )
        rows = []
        for reward, order_status in result.all():
            if reward.vests_at is None:
                stage = "awaiting_delivery"
            elif order_status in OPEN_RETURN_STATUSES:
                stage = "return_in_progress"
            else:
                stage = "return_window"
            rows.append(
                {
                    "order_id": str(reward.order_id),
                    "order_number": reward.order_number,
                    "amount": float(_money(reward.cashback_amount)),
                    "stage": stage,
                    "available_at": (
                        _aware(reward.vests_at).isoformat()
                        if reward.vests_at is not None and stage == "return_window"
                        else None
                    ),
                }
            )
        return rows

    @staticmethod
    def pending_total(rows: list[dict]) -> Decimal:
        return _money(sum(Decimal(str(r["amount"])) for r in rows))

    @staticmethod
    def order_cashback_state(order: Order) -> dict:
        """
        Where this order's cashback is, for the order screen. Derived from the
        order alone -- its settlement time is delivery + window + a minute, the
        same instant the reward row carries -- so every order read does not
        pay for a join.
        """
        from app.services.order_service import OrderService

        if order.cashback_credited_at is not None:
            return {"cashback_status": "credited", "cashback_available_at": None}
        if order.status == OrderStatus.CANCELLED.value:
            return {"cashback_status": "reversed", "cashback_available_at": None}

        available_at = None
        if order.status not in OPEN_RETURN_STATUSES:
            expires = OrderService.return_window_expires_at(order)
            if expires is not None:
                available_at = (expires + SETTLE_AFTER_WINDOW).isoformat()
        return {"cashback_status": "pending", "cashback_available_at": available_at}
