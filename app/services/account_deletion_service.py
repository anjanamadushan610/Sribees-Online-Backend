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

**What actually goes,** and it is not nothing:

  * the account cannot be logged into, by any route
  * the phone numbers and the email are released to NULL, so the person can
    sign up again and so those numbers are free for whoever holds them next
  * every push token is deleted, so no notification can reach a closed account
  * every session is revoked, so a live token cannot outlive the account

**What stays:** the NIC, the orders, the wallet ledger, the cancellation
record. Orders are financial records with tax and courier obligations attached
— they belong to the transaction, not to the account. A customer asking for
deletion is asking to stop being a user, not to un-buy things.

This is a defensible position under data-protection law (a legal obligation to
retain transaction records), but it is a position worth being able to explain,
so the app says plainly what is kept and why before anyone confirms.
"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import PushToken
from app.models.user import User


class AccountDeletionError(Exception):
    """Raised when an account cannot be closed."""


async def close_account(
    db: AsyncSession,
    user_id: UUID,
    *,
    reason: Optional[str] = None,
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

    # The NIC stays. It is the whole reason this is a soft delete.

    user.deleted_at = now
    user.deletion_reason = (reason or "").strip()[:500] or None
    user.is_active = False

    # No notification may ever reach a closed account. Deleted outright rather
    # than deactivated: a token is a routing address, not a record of anything.
    await db.execute(delete(PushToken).where(PushToken.user_id == user_id))

    # A live token must not outlive the account. Best-effort by design -- the
    # sessions table is not worth failing a deletion over, and `is_active`
    # already refuses every authenticated request.
    try:
        from app.models.user import Session as UserSession

        await db.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id)
            .values(is_active=False)
        )
    except Exception:
        logger.warning(
            f"Could not revoke sessions for {user_id}; the account is closed "
            "and is_active=False already refuses every request"
        )

    await db.commit()
    await db.refresh(user)
    logger.info(f"Account closed: {user_id}")
    return user


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
