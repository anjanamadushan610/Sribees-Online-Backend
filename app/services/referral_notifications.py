"""
SRIBEESonline — telling people their referral earnings are ready.

The money becomes theirs when a return window closes, which is a moment they
have no way to know about: they did not place the order, they did not receive
it, and twelve hours after someone else's delivery is not a time anybody is
watching for. So the server tells them.

One notification per person per sweep, not one per commission. Three friends
ordering on the same evening is one piece of good news, and three buzzes for
it is the behaviour that makes people turn notifications off entirely — after
which they never hear about anything again.
"""
from collections import defaultdict
from decimal import Decimal
from typing import Iterable

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationType
from app.models.referral import ReferralCommission
from app.services import notification_i18n as i18n
from app.services.notification_service import NotificationService


async def notify_vested(
    db: AsyncSession, commissions: Iterable[ReferralCommission]
) -> int:
    """
    Tell each beneficiary that their earnings can now be claimed.

    Only customer beneficiaries. A partner is a professional with a portal and
    a payout schedule; a push telling them about Rs 45 is noise on a screen
    they check for a living.

    Never raises. The money is claimable whether or not this works, and an
    exception here would be caught by the caller and logged as if the vesting
    had failed -- which it had not.
    """
    totals: dict = defaultdict(Decimal)
    counts: dict = defaultdict(int)

    for row in commissions:
        if not row.beneficiary_user_id:
            continue
        amount = Decimal(str(row.amount or 0))
        if amount <= 0:
            continue
        totals[row.beneficiary_user_id] += amount
        counts[row.beneficiary_user_id] += 1

    sent = 0
    for user_id, amount in totals.items():
        orders = counts[user_id]
        try:
            # Say the figure. "You have new earnings" makes people open the
            # app to find out whether it is worth opening the app.
            await NotificationService.notify(
                db,
                user_id,
                template="referral.ready.many" if orders > 1 else "referral.ready.one",
                type=NotificationType.PAYMENT,
                params={"amount": i18n.money(amount), "orders": orders},
                reference_type="referral",
                data={
                    # Lets the app open the right screen straight from the
                    # notification instead of dropping people on the home tab
                    # to go looking.
                    "screen": "referrals",
                    "amount": str(amount),
                    "orders": orders,
                },
            )
            sent += 1
        except Exception:
            # One bad token or one unreachable device must not cost everyone
            # else in the batch their notification.
            logger.exception(f"Could not notify {user_id} about vested referrals")

    return sent
