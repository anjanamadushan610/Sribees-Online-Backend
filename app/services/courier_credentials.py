"""
Which SribeesExpress key to use for a given call.

A branch that signs with SribeesExpress in its own name has its own merchant
key, its own pickup locations and its own COD ledger. A branch that does not
books against the account-wide key. So every courier call has to answer "whose
account is this?" before it answers anything else.

Resolution order, for a call made **on behalf of a branch**:

    branches.courier_api_key         -- this branch's own account
 -> app_settings 'courier_api_key'   -- the account-wide default, UI-editable
 -> settings.courier_api_key         -- the environment variable

Account-level operations -- coverage sync, webhook registration, the
reconciliation sweep -- deliberately skip the branch layer: they describe one
merchant account, and answering them with a branch key would describe the
wrong one.

Keys are never returned to any client. ``mask()`` is what the API hands back,
and it shows enough to tell two keys apart (the ``sk_test_``/``sk_live_``
prefix, which is the one that matters at go-live, plus the last four) without
being usable.
"""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import AsyncIterator, Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.branch import Branch
from app.services.app_settings_service import AppSettingsService

#: app_settings row holding the account-wide key.
ACCOUNT_KEY = "courier_api_key"


class CourierKeyMissingError(Exception):
    """No key is configured anywhere for this call."""


def mask(key: Optional[str]) -> Optional[str]:
    """
    A key rendered safe to show. ``sk_test_KBaX...HJ0`` -> ``sk_test_****HJ0``.

    The prefix is kept deliberately: `sk_test_` vs `sk_live_` is the single
    most consequential thing about a courier key, and an operator checking
    whether go-live actually happened should be able to see it at a glance.
    """
    if not key:
        return None
    prefix = ""
    for known in ("sk_test_", "sk_live_"):
        if key.startswith(known):
            prefix = known
            key_body = key[len(known):]
            break
    else:
        key_body = key
    tail = key_body[-4:] if len(key_body) >= 4 else ""
    return f"{prefix}****{tail}"


def environment_of(key: Optional[str]) -> Optional[str]:
    """``test`` | ``live`` | None — which SribeesExpress environment a key books in."""
    if not key:
        return None
    if key.startswith("sk_live_"):
        return "live"
    if key.startswith("sk_test_"):
        return "test"
    return "unknown"


async def account_key(db: AsyncSession) -> Optional[str]:
    """The account-wide key: the DB setting if set, else the environment var."""
    row = await AppSettingsService.get_by_key(db, ACCOUNT_KEY)
    if row is not None and row.is_active and (row.value or "").strip():
        return row.value.strip()
    return (settings.courier_api_key or "").strip() or None


async def resolve(db: AsyncSession, branch_id: Optional[UUID] = None) -> str:
    """
    The key to send for a call on behalf of ``branch_id``.

    Raises ``CourierKeyMissingError`` rather than returning an empty string:
    sending no key produces a 401 from SribeesExpress that reads like an
    outage, and "nobody configured a key" is a different problem an operator
    fixes a different way.
    """
    if branch_id is not None:
        branch_key = (
            await db.execute(
                select(Branch.courier_api_key).where(Branch.branch_id == branch_id)
            )
        ).scalar_one_or_none()
        if branch_key and branch_key.strip():
            return branch_key.strip()

    key = await account_key(db)
    if not key:
        raise CourierKeyMissingError(
            "No SribeesExpress API key is configured. A Super Admin can set one "
            "under Settings -> Courier."
        )
    return key


async def set_account_key(db: AsyncSession, key: Optional[str], *, actor: str) -> None:
    """
    Set (or clear) the account-wide key.

    Clearing falls back to the environment variable rather than disabling the
    courier, which is what an operator undoing a mistaken change actually
    wants -- the deployment's own key is the safe floor.
    """
    cleaned = (key or "").strip()
    await AppSettingsService.upsert(
        db,
        key=ACCOUNT_KEY,
        value=cleaned or None,
        description=(
            "Account-wide SribeesExpress merchant key. Empty = fall back to the "
            "COURIER_API_KEY environment variable."
        ),
        is_active=bool(cleaned),
    )
    logger.warning(
        f"Courier account key {'set' if cleaned else 'cleared'} by {actor} "
        f"(environment={environment_of(cleaned) or 'env fallback'})"
    )


async def set_branch_key(
    db: AsyncSession, branch: Branch, key: Optional[str], *, actor: str
) -> None:
    """
    Set (or clear) one branch's own key. Clearing returns it to the
    account-wide key.

    Who changed it is recorded; the previous key is not. A rotated credential
    should be unrecoverable from our side -- keeping a history of live keys
    would turn an audit trail into a second place to steal them from.
    """
    cleaned = (key or "").strip()
    branch.courier_api_key = cleaned or None
    branch.courier_api_key_set_at = datetime.now(timezone.utc) if cleaned else None
    branch.courier_api_key_set_by = actor if cleaned else None
    await db.commit()
    logger.warning(
        f"Courier key for branch {branch.code} "
        f"{'set' if cleaned else 'cleared'} by {actor} "
        f"(environment={environment_of(cleaned) or 'account default'})"
    )


# ---------------------------------------------------------------------------
# The active key, for the duration of one courier operation.
#
# CourierService exposes eighteen endpoint methods. Threading a key parameter
# through every one of them -- and every caller of every one -- would put the
# credential in eighteen signatures while changing nothing about how it is
# chosen. A scope keeps the choice in one place and makes the call sites say
# whose account they are acting as, which is the part a reader needs:
#
#     async with courier_credentials.for_branch(db, order.branch_id):
#         await CourierService.book_shipment(...)
#
# ContextVars are copied into each asyncio task, so concurrent requests cannot
# see each other's key. Outside any scope the value is None and CourierService
# falls back to the environment variable, which is exactly the behaviour that
# existed before branch keys.
# ---------------------------------------------------------------------------
_ACTIVE_KEY: ContextVar[Optional[str]] = ContextVar("courier_active_key", default=None)


def active_key() -> Optional[str]:
    """The key for the call in flight, or None outside a scope."""
    return _ACTIVE_KEY.get()


@asynccontextmanager
async def using(key: Optional[str]) -> AsyncIterator[None]:
    """Run a block with ``key`` as the active courier credential."""
    token = _ACTIVE_KEY.set(key)
    try:
        yield
    finally:
        _ACTIVE_KEY.reset(token)


@asynccontextmanager
async def for_branch(db: AsyncSession, branch_id: Optional[UUID]) -> AsyncIterator[str]:
    """
    Act as whichever SribeesExpress account serves ``branch_id``.

    Raises ``CourierKeyMissingError`` before any network call when nothing is
    configured -- a missing key produces a 401 that reads like an outage, and
    that is a different problem with a different fix.
    """
    key = await resolve(db, branch_id)
    async with using(key):
        yield key


@asynccontextmanager
async def for_account(db: AsyncSession) -> AsyncIterator[str]:
    """
    Act as the account-wide merchant, skipping any branch key.

    For the operations that describe one account rather than one parcel:
    coverage, webhook registration, the reconciliation sweep. A branch key
    would answer them for the wrong account.
    """
    key = await account_key(db)
    if not key:
        raise CourierKeyMissingError(
            "No SribeesExpress API key is configured. A Super Admin can set one "
            "under Settings -> Courier."
        )
    async with using(key):
        yield key
