"""
Which SribeesExpress account, and which environment, a given call acts as.

Three facts drive the shape of this module:

**Sandbox and live are the same integration with different credentials.** So
the deployment does not pick one — it holds both, and an operator flips a
switch. Go-live is the change most likely to be made under time pressure, and
making it a redeploy is how a launch slips.

**A branch can hold its own SribeesExpress merchant account.** A branch that
signs with them in its own name has its own key, its own pickup locations and
its own COD ledger. One environment variable cannot express "per branch, per
environment", so none of this lives in the environment.

**The base URL is not a secret.** Anyone holding a key already knows which host
it belongs to. It is configuration, and it belongs next to the thing it
configures rather than in a deploy pipeline.

Resolution, for a call made on behalf of a branch:

    environment = branches.courier_environment or app_settings courier_environment
    api_key     = branches.courier_api_key_<env> or app_settings courier_api_key_<env>
    base_url    = app_settings courier_base_url_<env>

The base URL is per-environment but never per-branch: sandbox and live are
different hosts, but every merchant on a host shares it.

Keys are **encrypted at rest** (app/core/crypto.py) and never returned by any
API — ``mask()`` is what a client sees. The mask keeps the ``sk_test_`` /
``sk_live_`` prefix on purpose: which environment a key books in is the single
most consequential thing about it.
"""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import DecryptionError, decrypt, encrypt
from app.models.branch import Branch
from app.services.app_settings_service import AppSettingsService

#: The two environments SribeesExpress runs. Keys are prefixed to match.
TEST = "test"
LIVE = "live"
ENVIRONMENTS = (TEST, LIVE)

#: app_settings keys.
ENVIRONMENT_KEY = "courier_environment"
BASE_URL_KEY = {TEST: "courier_base_url_test", LIVE: "courier_base_url_live"}
API_KEY_KEY = {TEST: "courier_api_key_test", LIVE: "courier_api_key_live"}
WEBHOOK_SECRET_KEY = {TEST: "courier_webhook_secret_test", LIVE: "courier_webhook_secret_live"}

#: branches columns, by environment.
BRANCH_KEY_COLUMN = {TEST: "courier_api_key_test", LIVE: "courier_api_key_live"}
BRANCH_WEBHOOK_SECRET_COLUMN = {
    TEST: "courier_webhook_secret_test",
    LIVE: "courier_webhook_secret_live",
}


class CourierNotConfiguredError(Exception):
    """No usable base URL and key for this call."""


@dataclass(frozen=True)
class CourierTarget:
    """Everything one courier call needs to know about who it is."""

    base_url: str
    api_key: str
    environment: str
    #: Where the key came from: branch | account. For the UI and the logs.
    key_source: str = "account"
    #: The branch whose own key this is; None for the account key.
    branch_id: Optional[UUID] = None


def mask(key: Optional[str]) -> Optional[str]:
    """
    A key rendered safe to show. ``sk_test_KBaX...HJ0`` -> ``sk_test_****HJ0``.

    The prefix survives deliberately: an operator checking whether go-live
    actually happened should be able to see it at a glance.
    """
    if not key:
        return None
    prefix = ""
    body = key
    for known in ("sk_test_", "sk_live_"):
        if key.startswith(known):
            prefix, body = known, key[len(known):]
            break
    return f"{prefix}****{body[-4:] if len(body) >= 4 else ''}"


def environment_of(key: Optional[str]) -> Optional[str]:
    """``test`` | ``live`` | ``unknown`` — the environment a key's prefix claims."""
    if not key:
        return None
    if key.startswith("sk_live_"):
        return LIVE
    if key.startswith("sk_test_"):
        return TEST
    return "unknown"


# ---------------------------------------------------------------------------
# Reading configuration
# ---------------------------------------------------------------------------


async def _setting(db: AsyncSession, key: str) -> Optional[str]:
    row = await AppSettingsService.get_by_key(db, key)
    if row is None or not row.is_active:
        return None
    return (row.value or "").strip() or None


async def account_environment(db: AsyncSession) -> str:
    """
    The environment every branch books in unless it overrides it.

    Defaults to ``test``: a deployment that has never been configured must not
    quietly start creating real parcels.
    """
    value = (await _setting(db, ENVIRONMENT_KEY) or TEST).lower()
    return value if value in ENVIRONMENTS else TEST


async def base_url_for(db: AsyncSession, environment: str) -> Optional[str]:
    """The host for one environment, with any trailing slash removed."""
    url = await _setting(db, BASE_URL_KEY[environment])
    return url.rstrip("/") if url else None


def try_decrypt(stored: Optional[str]) -> tuple[Optional[str], bool]:
    """
    Decrypt for display. Returns ``(value, unreadable)``.

    Used only by the screen an operator would use to FIX a bad master key.
    Letting the exception through there would take down the one page that can
    repair the situation, so the failure is reported per slot instead. Every
    path that actually *uses* a key still raises — booking a parcel with a
    credential we cannot read is not something to degrade gracefully into.
    """
    if not stored:
        return None, False
    try:
        return decrypt(stored), False
    except DecryptionError:
        return None, True


async def raw_account_api_key(db: AsyncSession, environment: str) -> Optional[str]:
    """The stored (still encrypted) account key for one environment."""
    return await _setting(db, API_KEY_KEY[environment])


async def account_api_key(db: AsyncSession, environment: str) -> Optional[str]:
    """The account-wide key for one environment, decrypted."""
    return decrypt(await _setting(db, API_KEY_KEY[environment]))


async def branch_api_key(
    db: AsyncSession, branch_id: UUID, environment: str
) -> Optional[str]:
    """One branch's own key for an environment, decrypted, or None."""
    column = getattr(Branch, BRANCH_KEY_COLUMN[environment])
    stored = (
        await db.execute(select(column).where(Branch.branch_id == branch_id))
    ).scalar_one_or_none()
    return decrypt(stored) if stored else None


async def resolve(db: AsyncSession, branch_id: Optional[UUID] = None) -> CourierTarget:
    """
    Everything a call on behalf of ``branch_id`` needs.

    Raises ``CourierNotConfiguredError`` rather than returning something
    half-built: an empty key produces a 401 that reads like an outage, and a
    missing base URL produces an ``httpx.InvalidURL`` deep inside the client.
    Both are "nobody finished the setup", which an operator fixes a different
    way from "the courier is down".
    """
    environment = None
    key = None
    source = "account"

    if branch_id is not None:
        branch_env = (
            await db.execute(
                select(Branch.courier_environment).where(Branch.branch_id == branch_id)
            )
        ).scalar_one_or_none()
        if branch_env and branch_env.lower() in ENVIRONMENTS:
            environment = branch_env.lower()

    if environment is None:
        environment = await account_environment(db)

    if branch_id is not None:
        key = await branch_api_key(db, branch_id, environment)
        if key:
            source = "branch"

    if not key:
        key = await account_api_key(db, environment)

    base_url = await base_url_for(db, environment)

    if not base_url or not key:
        missing = []
        if not base_url:
            missing.append(f"the {environment} base URL")
        if not key:
            missing.append(f"a {environment} API key")
        raise CourierNotConfiguredError(
            f"SribeesExpress is not set up for the {environment} environment: "
            f"{' and '.join(missing)} is missing. A Super Admin can set it under "
            f"Settings -> Courier."
        )

    return CourierTarget(
        base_url=base_url,
        api_key=key,
        environment=environment,
        key_source=source,
        branch_id=branch_id if source == "branch" else None,
    )


async def all_targets(db: AsyncSession) -> list[CourierTarget]:
    """
    Every distinct SribeesExpress account this deployment books into.

    The account key, plus each branch that holds its own key, each in the
    environment it currently books in. A branch holding its own key is its own
    SribeesExpress client: its parcels, webhooks and COD ledger are invisible to
    the account key, so anything that reads "all our shipments" — the
    reconciliation sweep above all — has to walk every one of these, or a
    delivered COD parcel booked under a branch key is never seen.

    Targets that cannot be built (no host, a key that will not decrypt) are
    skipped with a warning rather than failing the caller: one misconfigured
    branch must not stop the sweep reconciling everyone else.
    """
    targets: list[CourierTarget] = []
    seen: set[tuple[str, str]] = set()

    try:
        account = await resolve(db, None)
    except (CourierNotConfiguredError, DecryptionError) as exc:
        logger.info(f"No account-wide courier target: {exc}")
    else:
        targets.append(account)
        seen.add((account.base_url, account.api_key))

    branch_ids = (
        await db.execute(
            select(Branch.branch_id).where(
                (Branch.courier_api_key_test.isnot(None)) | (Branch.courier_api_key_live.isnot(None))
            )
        )
    ).scalars().all()
    for branch_id in branch_ids:
        try:
            target = await resolve(db, branch_id)
        except (CourierNotConfiguredError, DecryptionError) as exc:
            logger.warning(f"Skipping courier target for branch {branch_id}: {exc}")
            continue
        # A branch with a key only in the other environment resolves to the
        # account key here; that account is already in the list.
        if (target.base_url, target.api_key) in seen:
            continue
        seen.add((target.base_url, target.api_key))
        targets.append(target)
    return targets


async def any_target(db: AsyncSession) -> CourierTarget:
    """
    Some usable account, for calls whose answer is the same whichever account
    asks — the postal city directory above all.

    The account key when there is one, else the first branch with its own. A
    deployment where every branch signs with SribeesExpress separately has no
    account key at all, and must still be able to sync the directory.
    """
    targets = await all_targets(db)
    if not targets:
        raise CourierNotConfiguredError(
            "No SribeesExpress account is set up: enter an account key or a branch key "
            "under Settings -> Courier."
        )
    return targets[0]


# ---------------------------------------------------------------------------
# Webhook signing secrets
# ---------------------------------------------------------------------------


async def store_webhook_secret(
    db: AsyncSession, target: CourierTarget, secret: str, *, actor: str
) -> None:
    """
    Keep the signing secret SribeesExpress issued to ``target``'s account.

    Encrypted and never returned, exactly like the keys. Stored the moment
    registration succeeds, because SribeesExpress shows it once: an operator
    copying it out of a response into an environment file is how it ends up in
    a chat log, and a redeploy they forget is a week of rejected webhooks.
    """
    if target.branch_id is None:
        await AppSettingsService.upsert(
            db,
            key=WEBHOOK_SECRET_KEY[target.environment],
            value=encrypt(secret),
            description=(
                f"Webhook signing secret for the account-wide SribeesExpress "
                f"{target.environment} account, encrypted at rest."
            ),
            is_active=True,
        )
    else:
        branch = (
            await db.execute(select(Branch).where(Branch.branch_id == target.branch_id))
        ).scalar_one()
        setattr(branch, BRANCH_WEBHOOK_SECRET_COLUMN[target.environment], encrypt(secret))
        await db.commit()
    logger.warning(
        f"Courier webhook secret stored for {target.key_source} account "
        f"({target.environment}) by {actor}"
    )


async def account_webhook_secret_set(db: AsyncSession, environment: str) -> bool:
    """Whether the account-wide signing secret for ``environment`` is stored. Never the value."""
    return bool(await _setting(db, WEBHOOK_SECRET_KEY[environment]))


async def webhook_secrets(db: AsyncSession) -> list[str]:
    """
    Every secret an inbound push may be signed with, decrypted.

    One per registered account and environment. A push does not say which of
    our accounts it belongs to before its signature is checked, so the verifier
    tries each — a few HMACs, compared in constant time. Unreadable entries are
    skipped (and logged): a broken branch secret must not reject every other
    account's pushes.
    """
    stored: list[Optional[str]] = []
    for environment in ENVIRONMENTS:
        stored.append(await _setting(db, WEBHOOK_SECRET_KEY[environment]))
    rows = (
        await db.execute(
            select(Branch.courier_webhook_secret_test, Branch.courier_webhook_secret_live)
        )
    ).all()
    for test_secret, live_secret in rows:
        stored.extend([test_secret, live_secret])

    secrets: list[str] = []
    for value in stored:
        if not value:
            continue
        try:
            plain = decrypt(value)
        except DecryptionError:
            logger.error("A stored courier webhook secret will not decrypt -- re-register that endpoint")
            continue
        if plain:
            secrets.append(plain)
    return secrets


# ---------------------------------------------------------------------------
# Writing configuration
# ---------------------------------------------------------------------------


async def set_account_environment(db: AsyncSession, environment: str, *, actor: str) -> None:
    """Flip every branch that has no override between sandbox and live."""
    if environment not in ENVIRONMENTS:
        raise ValueError(f"environment must be one of {ENVIRONMENTS}")
    await AppSettingsService.upsert(
        db,
        key=ENVIRONMENT_KEY,
        value=environment,
        description="Which SribeesExpress environment branches book in by default: test | live.",
        is_active=True,
    )
    logger.warning(f"Courier environment switched to {environment} by {actor}")


async def set_base_url(
    db: AsyncSession, environment: str, url: Optional[str], *, actor: str
) -> None:
    """
    Set the host for one environment. Not a secret, so stored as written.

    The stored value is the ORIGIN: CourierService appends
    ``/api/v1/ecommerce`` itself, and a URL that already carries the prefix
    doubles the path and 404s every call. That is checked here rather than
    discovered later, because a 404 on every call reads like an outage.
    """
    if environment not in ENVIRONMENTS:
        raise ValueError(f"environment must be one of {ENVIRONMENTS}")
    cleaned = (url or "").strip().rstrip("/")
    if cleaned and cleaned.endswith("/api/v1/ecommerce"):
        raise ValueError(
            "Enter the origin only (e.g. https://devapiexpress.sribees.com). "
            "The /api/v1/ecommerce path is added automatically; including it here "
            "would double the path and every call would fail."
        )
    if cleaned and not cleaned.startswith("https://"):
        raise ValueError("The base URL must be https://.")
    await AppSettingsService.upsert(
        db,
        key=BASE_URL_KEY[environment],
        value=cleaned or None,
        description=f"SribeesExpress {environment} origin, without the /api/v1/ecommerce path.",
        is_active=bool(cleaned),
    )
    logger.info(f"Courier {environment} base URL set to {cleaned or '(cleared)'} by {actor}")


async def set_account_api_key(
    db: AsyncSession, environment: str, key: Optional[str], *, actor: str
) -> None:
    """Set (or clear) the account-wide key for one environment. Stored encrypted."""
    if environment not in ENVIRONMENTS:
        raise ValueError(f"environment must be one of {ENVIRONMENTS}")
    cleaned = (key or "").strip()
    await AppSettingsService.upsert(
        db,
        key=API_KEY_KEY[environment],
        value=encrypt(cleaned) if cleaned else None,
        description=(
            f"Account-wide SribeesExpress {environment} merchant key, encrypted at rest."
        ),
        is_active=bool(cleaned),
    )
    logger.warning(
        f"Courier account {environment} key {'set' if cleaned else 'cleared'} by {actor}"
    )


async def set_branch_api_key(
    db: AsyncSession, branch: Branch, environment: str, key: Optional[str], *, actor: str
) -> None:
    """
    Set (or clear) one branch's own key for an environment. Stored encrypted.

    Who changed it is recorded; the previous key is not. A rotated credential
    should be unrecoverable from our side — keeping a history of live keys
    would turn an audit trail into a second place to steal them from.
    """
    if environment not in ENVIRONMENTS:
        raise ValueError(f"environment must be one of {ENVIRONMENTS}")
    cleaned = (key or "").strip()
    setattr(branch, BRANCH_KEY_COLUMN[environment], encrypt(cleaned) if cleaned else None)
    branch.courier_api_key_set_at = datetime.now(timezone.utc) if cleaned else None
    branch.courier_api_key_set_by = actor if cleaned else None
    await db.commit()
    logger.warning(
        f"Courier {environment} key for branch {branch.code} "
        f"{'set' if cleaned else 'cleared'} by {actor}"
    )


async def set_branch_environment(
    db: AsyncSession, branch: Branch, environment: Optional[str], *, actor: str
) -> None:
    """
    Pin one branch to an environment, or return it to following the account.

    Exists for a staged go-live: one branch books real parcels while the rest
    stay in the sandbox.
    """
    if environment is not None and environment not in ENVIRONMENTS:
        raise ValueError(f"environment must be one of {ENVIRONMENTS} or null")
    branch.courier_environment = environment
    await db.commit()
    logger.warning(
        f"Branch {branch.code} courier environment "
        f"{'pinned to ' + environment if environment else 'now follows the account'} by {actor}"
    )


# ---------------------------------------------------------------------------
# The active target, for the duration of one courier operation.
#
# CourierService exposes eighteen endpoint methods. Threading base URL and key
# through every one of them -- and every caller of every one -- would put a
# credential in eighteen signatures while changing nothing about how it is
# chosen. A scope keeps the choice in one place and makes the call sites say
# whose account they act as, which is the part a reader needs:
#
#     async with courier_credentials.for_branch(db, order.branch_id):
#         await CourierService.book_shipment(...)
#
# ContextVars are copied into each asyncio task, so concurrent requests cannot
# see each other's credentials.
# ---------------------------------------------------------------------------
_ACTIVE: ContextVar[Optional[CourierTarget]] = ContextVar("courier_target", default=None)


def active_target() -> Optional[CourierTarget]:
    """The target for the call in flight, or None outside a scope."""
    return _ACTIVE.get()


@asynccontextmanager
async def using(target: CourierTarget) -> AsyncIterator[CourierTarget]:
    """Run a block against an explicitly built target."""
    token = _ACTIVE.set(target)
    try:
        yield target
    finally:
        _ACTIVE.reset(token)


@asynccontextmanager
async def for_branch(
    db: AsyncSession, branch_id: Optional[UUID]
) -> AsyncIterator[CourierTarget]:
    """Act as whichever SribeesExpress account and environment serves ``branch_id``."""
    target = await resolve(db, branch_id)
    async with using(target):
        yield target


@asynccontextmanager
async def optional_for_branch(
    db: AsyncSession, branch_id: Optional[UUID]
) -> AsyncIterator[Optional[CourierTarget]]:
    """
    Like ``for_branch``, but yields nothing instead of raising when the courier
    is not configured.

    For checkout. A deployment with no SribeesExpress credentials must still be
    able to sell: PricingService already falls back to the flat platform fee
    when the courier cannot be reached, and "nobody has entered a key yet" is
    the same answer to a shopper as "the courier is down". Refusing the sale
    over our own missing configuration would be strictly worse.

    Every path that BOOKS a parcel uses ``for_branch`` and does raise -- there
    the difference matters, because a booking that silently did not happen is
    a parcel nobody collects.
    """
    try:
        target = await resolve(db, branch_id)
    except (CourierNotConfiguredError, DecryptionError) as exc:
        logger.warning(f"Pricing without a courier quote: {exc}")
        yield None
        return
    async with using(target):
        yield target


@asynccontextmanager
async def for_account(db: AsyncSession) -> AsyncIterator[CourierTarget]:
    """
    Act as the account-wide merchant, ignoring any branch key.

    For operations that describe one account rather than one parcel: coverage,
    webhook registration, the reconciliation sweep. A branch key would answer
    them for the wrong account.
    """
    target = await resolve(db, None)
    async with using(target):
        yield target
