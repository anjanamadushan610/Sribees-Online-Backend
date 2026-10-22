"""
SRIBEESonline — closing a customer account.

Google Play requires an in-app way to delete an account for any app that lets
you create one, so this is not optional. What it is NOT is a row disappearing
from the users table.

**Why a soft delete.** The NIC is this business's identity key. If closing an
account erased what that NIC had done, anyone who cancelled orders on a loop
or ran up referral clawbacks could close, reopen, and arrive clean — and the
cancellation review screen would greet them as a first-time customer every
time. The row stays, the NIC stays, and the history stays attached to it.

**What actually goes:**

  * the account cannot be logged into, by any route
  * the name, the phone numbers and the email are released, so the person
    can sign up again and those numbers are free for whoever holds them next
  * saved addresses no order points at, the wishlist, the inbox, the AI
    assistant's saved preferences, reminders and receipts, support tickets,
    the AI chat history and the cart
  * a Google/Apple link, so the same Apple ID or Google account can open a
    fresh account; Sign in with Apple is also revoked at Apple (App Store
    5.1.1(v))
  * every push token and every session

**What stays, for five years after closure** (the owner's retention period,
2026-10-22): the NIC, the orders with the delivery address and contact number
they were sent to, the wallet ledger and payment records, the cancellation
record. Orders are financial records with tax and courier obligations
attached -- they belong to the transaction, not to the account. A customer
asking for deletion is asking to stop being a user, not to un-buy things.

This is a defensible position under data-protection law (a legal obligation to
retain transaction records), but it is a position worth being able to explain,
so the app says plainly what is kept and why before anyone confirms.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import Select, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.redis import RedisKeys
from app.models.app_usage import AppInstall, AppUsageDay
from app.models.assistant import AssistantProfile, AssistantReceipt, AssistantReminder
from app.models.notification import Notification, PushToken
from app.models.order import Order
from app.models.support_ticket import SupportTicket
from app.models.user import Address, User
from app.models.wishlist import WishlistItem
from app.services import apple_tokens


class AccountDeletionError(Exception):
    """Raised when an account cannot be closed."""


async def close_account(
    db: AsyncSession,
    user_id: UUID,
    *,
    reason: Optional[str] = None,
    redis=None,
) -> User:
    """
    Close an account: release its identity, revoke its access, keep its record.

    Idempotent. A second call on an already-closed account returns it
    unchanged rather than raising — a customer tapping twice on a slow
    connection must not see an error for something that worked.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise AccountDeletionError("Account not found")

    if user.deleted_at is not None:
        logger.info(f"Account {user_id} was already closed; nothing to do")
        return user

    now = datetime.now(timezone.utc)

    # Keep what they held, out of the unique columns. Support gets to answer
    # "this number used to be mine" without the number staying reserved.
    user.deleted_phone = user.phone
    user.deleted_email = user.email

    # Release the identity. These are unique columns: leaving them set would
    # stop the person signing up again with their own number, and stop anyone
    # who inherits that number from using it at all.
    user.phone = None
    user.email = None
    user.alternate_phone = None

    # The name goes too (full_name is NOT NULL, so empty). Orders keep their
    # delivery address, whose recipient name is what a dispute needs.
    user.full_name = ""
    user.first_name = None
    user.last_name = None

    # Closing must not depend on Apple's availability. Keep a failed token
    # only for the durable revocation worker; never discard the retry proof.
    if user.apple_refresh_token:
        revoked = await apple_tokens.revoke(user.apple_refresh_token, user.apple_client_id or "")
        if revoked:
            user.apple_refresh_token = None
            user.apple_client_id = None
            user.apple_revocation_retry_at = None
        else:
            user.apple_revocation_retry_at = now + timedelta(minutes=5)
            logger.warning("Apple revocation pending; durable retry scheduled")
    else:
        user.apple_client_id = None
        user.apple_revocation_retry_at = None

    # Release the Google/Apple link. Left set, signing in with the same Apple
    # ID would find this closed row and refuse, so the person could never
    # come back that way. The NIC carries the history to a new account.
    user.provider_uid = None

    # The NIC stays. It is the whole reason this is a soft delete.

    # Personal data with no record-keeping duty. An address an order points
    # at is part of that order (where the goods went) and stays with it.
    await db.execute(
        delete(Address).where(
            Address.user_id == user_id,
            ~select(Order.order_id)
            .where(Order.delivery_address_id == Address.address_id)
            .exists(),
        )
    )
    for model in (
        WishlistItem,
        Notification,
        AssistantProfile,
        AssistantReminder,
        AssistantReceipt,
        SupportTicket,
    ):
        await db.execute(delete(model).where(model.user_id == user_id))
    # Usage counts stay (they are totals); they just stop naming anyone.
    for model in (AppInstall, AppUsageDay):
        await db.execute(update(model).where(model.user_id == user_id).values(user_id=None))

    user.deleted_at = now
    user.deletion_reason = (reason or "").strip()[:500] or None
    user.is_active = False

    # No notification may ever reach a closed account. Deleted outright rather
    # than deactivated: a token is a routing address, not a record of anything.
    await db.execute(delete(PushToken).where(PushToken.user_id == user_id))

    # A live token must not outlive the account. Best-effort by design -- the
    # sessions table is not worth failing a deletion over, and `is_active`
    # already refuses every authenticated request.
    #
    # Deleted, not flagged: sessions have no is_active column, so the update
    # this used to be failed on every closure and revoked nothing.
    try:
        from app.models.user import Session as UserSession

        await db.execute(
            delete(UserSession).where(UserSession.user_id == user_id)
        )
    except Exception:
        logger.warning(
            f"Could not revoke sessions for {user_id}; the account is closed "
            "and is_active=False already refuses every request"
        )

    await db.commit()
    await db.refresh(user)

    # The AI chat history and the cart live in Redis, keyed by the user.
    if redis is not None:
        uid = str(user_id)
        try:
            await redis.delete(
                RedisKeys.chat_history(uid),
                RedisKeys.cart(uid),
                RedisKeys.cart_items(uid),
                RedisKeys.wishlist(uid),
                RedisKeys.wishlist_details(uid),
                RedisKeys.branch_context(uid),
            )
        except Exception:
            logger.warning(f"Could not clear Redis data for closed account {user_id}")

    logger.info(f"Account closed: {user_id}")
    return user


def live_nic_holder(nic: str, *, other_than: Optional[UUID] = None) -> Select:
    """
    The live account holding this NIC, if any -- the NIC uniqueness check.

    Closed accounts are left out on purpose. They keep their NIC so the
    history stays attached to the person, and that must not stop the same
    person registering again with it. Mirrors the `ix_users_nic_live` index,
    so a check that passes here cannot then fail on the index.
    """
    q = select(User).where(User.nic == nic, User.deleted_at.is_(None))
    if other_than is not None:
        q = q.where(User.user_id != other_than)
    return q


async def accounts_sharing_nic(
    db: AsyncSession, nic: Optional[str], *, other_than: UUID
) -> list[User]:
    """
    Every other account, live or closed, that carries this NIC -- newest first.

    What lets an admin get from a customer's new account to the one they
    closed, and back. The customer never sees this: their new account is a
    new user_id, so none of the old orders reach the app.
    """
    if not nic:
        return []
    result = await db.execute(
        select(User)
        .where(User.nic == nic, User.user_id != other_than)
        .order_by(User.created_at.desc())
    )
    return list(result.scalars().all())


async def find_prior_account_by_nic(db: AsyncSession, nic: str) -> Optional[User]:
    """
    A closed account for this NIC, if there is one.

    This is what makes the soft delete worth having: somebody re-registering
    with the same NIC looks like a new customer to themselves, and to us
    carries everything the old account did. The cancellation review reads it
    through the NIC, not the account id.
    """
    if not nic:
        return None
    result = await db.execute(
        select(User)
        .where(User.nic == nic, User.deleted_at.isnot(None))
        .order_by(User.deleted_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
