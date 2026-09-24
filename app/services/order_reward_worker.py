"""
SRIBEESonline — the order-reward settlement sweep.

An order's cashback and points become the customer's when its return window
closes, a moment nobody is watching for. The server notices, settles, and says
so: "Rs. 45 cashback is now in your wallet" is the news the whole wait was for,
and a level reached comes with its coupon in the same message.

Its own loop, not a branch of the referral sweep: they fail for unrelated
reasons, and a settlement loop that silently stopped would look exactly like
"cashback never arrived" -- the one failure nobody reports as a bug.
"""
import asyncio
from collections import defaultdict
from decimal import Decimal

from loguru import logger

from app.config.database import async_session_maker
from app.models.notification import NotificationType
from app.services.notification_service import NotificationService
from app.services.order_reward_service import OrderRewardService, SettledReward

SWEEP_INTERVAL_SECONDS = 60


def _money(amount: Decimal) -> str:
    return f"Rs. {amount:,.2f}"


def _reward_text(coupon) -> str:
    value = Decimal(str(coupon.discount_value))
    off = f"{value:g}% off" if coupon.discount_type == "percentage" else f"{_money(value)} off"
    days = (coupon.valid_until - coupon.valid_from).days
    return f"{off} with code {coupon.code}, valid {days} days"


async def notify_settled(db, settled: list[SettledReward]) -> int:
    """
    One notification per customer per sweep for the cashback, and one per
    level reached. Never raises: the money is theirs whether or not a push
    gets through, and one unreachable device must not cost the rest.
    """
    cashback: dict = defaultdict(Decimal)
    orders: dict = defaultdict(list)
    for s in settled:
        if s.cashback_credited > 0:
            cashback[s.user_id] += s.cashback_credited
            orders[s.user_id].append(s.order_number)

    sent = 0
    for user_id, amount in cashback.items():
        numbers = orders[user_id]
        message = (
            f"{_money(amount)} cashback from order #{numbers[0]} is now in your wallet."
            if len(numbers) == 1
            else f"{_money(amount)} cashback from {len(numbers)} orders is now in your wallet."
        )
        try:
            await NotificationService.create(
                db,
                user_id=user_id,
                type=NotificationType.PAYMENT,
                title="Cashback added to your wallet",
                message=message,
                reference_type="wallet",
                data={"screen": "wallet", "amount": str(amount)},
            )
            sent += 1
        except Exception:
            logger.exception(f"Could not notify {user_id} about settled cashback")

    for s in settled:
        issued = {r.tier.tier_id: r for r in s.rewards_issued}
        for tier in s.tiers_reached:
            reward = issued.get(tier.tier_id)
            message = (
                f"You've reached {tier.name}! Your reward: {_reward_text(reward.coupon)}."
                if reward
                else f"You've reached {tier.name} with {s.lifetime_points} points."
            )
            try:
                await NotificationService.create(
                    db,
                    user_id=s.user_id,
                    type=NotificationType.PROMOTION,
                    title=f"Welcome to {tier.name}",
                    message=message,
                    reference_type="loyalty",
                    data={
                        "screen": "coupons" if reward else "profile",
                        **({"code": reward.coupon.code} if reward else {}),
                    },
                )
                sent += 1
            except Exception:
                logger.exception(f"Could not notify {s.user_id} about reaching {tier.name}")

    return sent


async def sweep_once() -> int:
    """One pass. Returns how many rewards settled."""
    async with async_session_maker() as db:
        settled = await OrderRewardService.settle_due(db)

    if not settled:
        return 0

    # Outside the settling transaction on purpose: a push failure must never
    # roll back money that has already moved.
    try:
        async with async_session_maker() as db:
            await notify_settled(db, settled)
    except Exception:
        logger.exception("Rewards settled but the notifications failed; the money is in the wallet")

    return len(settled)


async def run_order_reward_worker():
    while True:
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Order reward sweep failed; check migration 066 and database "
                "connectivity. Rewards stay pending until it recovers -- nothing "
                "is lost, but no cashback settles."
            )
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
