"""
Admin Courier Operations — SribeesExpress account plumbing.

Four things have to happen outside the customer-facing flow, and none of
them belong on a checkout path:

* **Postal cities.** ``POST /postal-cities/sync`` fills the postal city
  directory from SribeesExpress, with the id every delivery quote is keyed on
  (``docs/SRIBEESEXPRESS_ECOMMERCE_API.md`` section 5). It also runs by itself
  (courier_coverage_worker): after startup, every few hours, and right after
  any courier key, base URL or environment is saved. The button is for "now".
* **Outlets.** A branch that books under the shared account key must be
  registered as an outlet of that account, or its parcels are priced from, and
  collected at, the account's head office (section 12). A branch with its own
  key is its own SribeesExpress client and needs none.
* **Reconciliation sweep.** SribeesExpress does not retry webhooks (section
  10.4), so ``GET /ecommerce/shipments?updated_since=`` is the real source of
  truth — swept across every account we book into. This backend has no job
  scheduler, so the sweep is exposed as an endpoint for an external cron.
* **Webhook registration.** Per account. The signing secret SribeesExpress
  shows once is stored encrypted the moment it arrives, never returned.

Prefix "/admin/courier" is applied by app/api/v1/router.py.
"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.crypto import DecryptionError
from app.core.dependencies import require_roles
from app.models.branch import Branch
from app.models.order import Order
from app.schemas.courier import (
    CourierBaseUrlUpdate,
    CourierBranchKeyStatus,
    CourierCredentialsResponse,
    CourierEnvironmentConfig,
    CourierEnvironmentUpdate,
    CourierKeyTestResult,
    CourierKeyUpdate,
    CourierSweepResponse,
    CoverageSyncResponse,
    OutletSyncResponse,
    OutletSyncResult,
    WebhookRegistrationRequest,
    WebhookRegistrationResponse,
)
from app.services import (
    courier_coverage_service,
    courier_coverage_worker,
    courier_credentials,
    courier_location_service,
    courier_sync_service,
)
from app.services.courier_service import (
    CourierError,
    CourierService,
    CourierUnavailableError,
)

router = APIRouter(tags=["Admin - Courier"])

# Every write here touches a SribeesExpress ACCOUNT, not one order: outlets for
# the whole estate, the webhook secret, the credentials. None of it is
# branch-scoped and none of it is delegatable (`settings` is excluded from
# DELEGATABLE_PERMISSIONS by design), so these are role-gated rather than
# permission-gated.
_ACCOUNT_OPS = Depends(require_roles("super_admin"))
# Reading what SribeesExpress owes us is a finance/ops question a Branch
# Manager legitimately asks about their own deliveries, so reads open one
# role wider than the writes -- and a Branch Manager is held to their branch.
_COURIER_READ = Depends(require_roles("super_admin", "branch_manager"))

_NOT_CONFIGURED = (
    courier_credentials.CourierNotConfiguredError,
    DecryptionError,
)


def _role(admin) -> str:
    return admin.role.value if hasattr(admin.role, "value") else str(admin.role)


def _scoped_branch_id(admin, branch_id: Optional[UUID]) -> Optional[UUID]:
    """
    The branch a courier read may act for.

    A Super Admin names any branch, or none for the account. A Branch Manager is
    always their own branch: a ``branch_id`` naming another one is refused, not
    silently swapped, so a mistyped id reads as an error and not as someone
    else's cash balance.
    """
    if _role(admin) == "super_admin":
        return branch_id
    own = getattr(admin, "branch_id", None)
    if branch_id is not None and branch_id != own:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only read your own branch's courier account.",
        )
    return own


async def _load_branch(db: AsyncSession, branch_id: UUID) -> Branch:
    branch = (
        await db.execute(select(Branch).where(Branch.branch_id == branch_id))
    ).scalar_one_or_none()
    if branch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")
    return branch


# ---------------------------------------------------------------------------
# Postal cities
# ---------------------------------------------------------------------------


@router.post("/postal-cities/sync", response_model=CoverageSyncResponse)
async def sync_postal_cities(
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CoverageSyncResponse:
    """
    Rebuild the postal city directory from ``GET /ecommerce/postal-cities``.

    This is what makes a real customer address quotable, and it re-matches every
    saved address that had no courier id in the same run. A failure is returned
    as a 502 rather than swallowed: an operator asked for this and needs to know
    it did not happen.
    """
    try:
        result = await courier_coverage_service.sync(db)
    except _NOT_CONFIGURED as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except (CourierUnavailableError, CourierError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return CoverageSyncResponse(**result.to_dict())


# ---------------------------------------------------------------------------
# Outlets
# ---------------------------------------------------------------------------


async def _recover_outlet_by_name(name: str) -> Optional[dict]:
    """
    Find an already-registered outlet by name (case-insensitive).

    Only used to recover from ``outlet_name_already_registered``: SribeesExpress
    refuses a duplicate name rather than silently returning the existing row, so
    a deployment that lost its stored id has to look it up.
    """
    for outlet in await CourierService.list_outlets():
        if (outlet.get("name") or "").strip().lower() == name.strip().lower():
            return outlet
    return None


async def _register_outlet(branch: Branch, payload: dict) -> tuple[dict, str]:
    """Create or update one branch's outlet in the active (account) scope."""
    if branch.courier_outlet_id:
        return await CourierService.update_outlet(branch.courier_outlet_id, **payload), "updated"
    try:
        return await CourierService.create_outlet(**payload), "created"
    except CourierError as exc:
        if exc.code != "outlet_name_already_registered":
            raise
        recovered = await _recover_outlet_by_name(branch.name)
        if recovered is None:
            raise
        return recovered, "recovered"


@router.post("/outlets/sync", response_model=OutletSyncResponse)
async def sync_outlets(
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> OutletSyncResponse:
    """
    Register every active branch that books under the account key as an outlet
    of that account, or update the one already registered (contract section 12).

    A branch with its own key in the environment it books in is reported as
    ``own_account`` and left alone: it is its own SribeesExpress client, priced
    from and collected at the address it registered with them.

    Safe to re-run: a branch that already holds a ``courier_outlet_id`` is
    PATCHed, never re-created, so ids stay stable. Branches are processed
    independently — one failure does not abort the rest.
    """
    branches = (
        (await db.execute(select(Branch).where(Branch.is_active.is_(True)).order_by(Branch.code)))
        .scalars()
        .all()
    )

    results: list[OutletSyncResult] = []
    synced = failed = 0

    for branch in branches:
        try:
            target = await courier_credentials.resolve(db, branch.branch_id)
        except _NOT_CONFIGURED as exc:
            failed += 1
            results.append(
                OutletSyncResult(
                    branch_code=branch.code, branch_name=branch.name, action="failed", detail=str(exc)[:300]
                )
            )
            continue

        if target.key_source == "branch":
            results.append(
                OutletSyncResult(
                    branch_code=branch.code,
                    branch_name=branch.name,
                    action="own_account",
                    detail="Books with its own SribeesExpress key; collected at that account's address.",
                )
            )
            continue

        postal_city_id = await courier_location_service.postal_city_id_for(
            db, branch.postal_city, branch.district
        )
        missing = [
            label
            for label, value in (
                ("address", branch.address),
                ("phone", branch.phone),
                ("a postal city SribeesExpress recognises", postal_city_id),
            )
            if not value
        ]
        if missing:
            results.append(
                OutletSyncResult(
                    branch_code=branch.code,
                    branch_name=branch.name,
                    outlet_id=branch.courier_outlet_id,
                    action="skipped",
                    detail=f"Branch has no {', '.join(missing)} -- a rider cannot be sent to it.",
                )
            )
            continue

        payload = {
            "name": branch.name,
            "phone": branch.phone,
            "address": branch.address,
            "postal_city_id": postal_city_id,
        }
        try:
            async with courier_credentials.using(target):
                result, action = await _register_outlet(branch, payload)
        except (CourierUnavailableError, CourierError) as exc:
            failed += 1
            logger.error(f"Outlet sync failed for branch {branch.code}: {exc}")
            results.append(
                OutletSyncResult(
                    branch_code=branch.code,
                    branch_name=branch.name,
                    outlet_id=branch.courier_outlet_id,
                    action="failed",
                    detail=str(exc)[:300],
                )
            )
            continue

        branch.courier_outlet_id = result.get("id") or branch.courier_outlet_id
        branch.courier_outlet_synced_at = datetime.now(timezone.utc)
        synced += 1
        results.append(
            OutletSyncResult(
                branch_code=branch.code,
                branch_name=branch.name,
                outlet_id=branch.courier_outlet_id,
                action=action,
            )
        )

    await db.commit()
    logger.info(f"Courier outlet sync: {synced} synced, {failed} failed")
    return OutletSyncResponse(synced=synced, failed=failed, results=results)


# ---------------------------------------------------------------------------
# Reconciliation sweep
# ---------------------------------------------------------------------------


@router.post("/sync", response_model=CourierSweepResponse)
async def run_reconciliation_sweep(
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CourierSweepResponse:
    """
    Run the shipment reconciliation sweep now (contract section 7a), across the
    account key and every branch with its own.

    Intended to be called on a schedule (external cron; every 10-15 minutes is
    a reasonable starting point) and by ops after a webhook outage. Idempotent.
    """
    seen, applied, accounts = await courier_sync_service.sweep(db)
    return CourierSweepResponse(seen=seen, applied=applied, accounts=accounts)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


async def _credentials_payload(db: AsyncSession) -> CourierCredentialsResponse:
    """
    Build the whole Settings → Courier picture: both environments, every
    branch, and whether this deployment can verify a webhook at all.

    Nothing secret is returned. Keys come back masked; the base URLs are not
    secrets and are returned in full.
    """
    account_env = await courier_credentials.account_environment(db)

    any_unreadable = False

    environments: list[CourierEnvironmentConfig] = []
    for env in courier_credentials.ENVIRONMENTS:
        base = await courier_credentials.base_url_for(db, env)
        stored = await courier_credentials.raw_account_api_key(db, env)
        key, unreadable = courier_credentials.try_decrypt(stored)
        any_unreadable = any_unreadable or unreadable
        environments.append(
            CourierEnvironmentConfig(
                environment=env,
                base_url=base,
                base_url_ok=bool(base) and not base.endswith("/api/v1/ecommerce"),
                api_key_set=bool(stored),
                api_key_masked=courier_credentials.mask(key),
                api_key_environment=courier_credentials.environment_of(key),
                api_key_unreadable=unreadable,
                usable=bool(base and key),
                webhook_secret_stored=await courier_credentials.account_webhook_secret_set(db, env),
            )
        )

    rows: list[CourierBranchKeyStatus] = []
    for branch in (await db.execute(select(Branch).order_by(Branch.code))).scalars().all():
        effective = (branch.courier_environment or account_env).lower()
        raw_test = getattr(branch, courier_credentials.BRANCH_KEY_COLUMN[courier_credentials.TEST])
        raw_live = getattr(branch, courier_credentials.BRANCH_KEY_COLUMN[courier_credentials.LIVE])
        test_key, test_bad = courier_credentials.try_decrypt(raw_test)
        live_key, live_bad = courier_credentials.try_decrypt(raw_live)
        any_unreadable = any_unreadable or test_bad or live_bad
        own = test_key if effective == courier_credentials.TEST else live_key
        secret_column = courier_credentials.BRANCH_WEBHOOK_SECRET_COLUMN.get(effective)
        rows.append(
            CourierBranchKeyStatus(
                branch_id=str(branch.branch_id),
                branch_code=branch.code,
                branch_name=branch.name,
                environment_override=branch.courier_environment,
                effective_environment=effective,
                test_key_set=bool(raw_test),
                test_key_masked=courier_credentials.mask(test_key),
                live_key_set=bool(raw_live),
                live_key_masked=courier_credentials.mask(live_key),
                key_unreadable=test_bad or live_bad,
                key_source="branch" if own else "account",
                set_at=(
                    branch.courier_api_key_set_at.isoformat()
                    if branch.courier_api_key_set_at
                    else None
                ),
                set_by=branch.courier_api_key_set_by,
                outlet_id=branch.courier_outlet_id,
                webhook_secret_stored=bool(secret_column and getattr(branch, secret_column)),
            )
        )

    stored_secrets = await courier_credentials.webhook_secrets(db)
    return CourierCredentialsResponse(
        environment=account_env,
        environments=environments,
        branches=rows,
        webhook_secret_configured=bool(
            stored_secrets or (settings.courier_webhook_secret or "").strip()
        ),
        any_key_unreadable=any_unreadable,
        credential_encryption=(
            "dedicated"
            if (settings.credential_encryption_key or "").strip()
            else "derived"
        ),
    )


@router.get("/credentials", response_model=CourierCredentialsResponse)
async def get_courier_credentials(
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CourierCredentialsResponse:
    """
    Which SribeesExpress account and environment each branch books against.

    Masks only — there is no endpoint that reads a key back, just ones that
    replace it.
    """
    return await _credentials_payload(db)


@router.put("/credentials/key", response_model=CourierCredentialsResponse)
async def set_account_courier_key(
    body: CourierKeyUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CourierCredentialsResponse:
    """
    Set the account-wide key for one environment — the key every branch
    without its own books against. Stored encrypted; an empty value clears it.
    """
    actor = getattr(current_admin, "email", "unknown")
    try:
        await courier_credentials.set_account_api_key(
            db, body.environment, body.api_key, actor=actor
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    courier_coverage_worker.sync_soon("courier settings changed")
    return await _credentials_payload(db)


@router.put("/credentials/base-url", response_model=CourierCredentialsResponse)
async def set_courier_base_url(
    body: CourierBaseUrlUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CourierCredentialsResponse:
    """
    Set the SribeesExpress host for one environment. Origin only.
    """
    actor = getattr(current_admin, "email", "unknown")
    try:
        await courier_credentials.set_base_url(db, body.environment, body.base_url, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    courier_coverage_worker.sync_soon("courier settings changed")
    return await _credentials_payload(db)


@router.put("/credentials/environment", response_model=CourierCredentialsResponse)
async def set_courier_environment(
    body: CourierEnvironmentUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CourierCredentialsResponse:
    """
    Switch every branch that has no override between sandbox and live.

    This is go-live. Refused unless that environment is actually usable.
    """
    if body.environment not in courier_credentials.ENVIRONMENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"environment must be one of {courier_credentials.ENVIRONMENTS}",
        )
    base = await courier_credentials.base_url_for(db, body.environment)
    key = await courier_credentials.account_api_key(db, body.environment)
    if not base or not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"The {body.environment} environment is not set up yet "
                f"({'no base URL' if not base else 'no API key'}). "
                "Switching to it would stop every branch from booking."
            ),
        )
    actor = getattr(current_admin, "email", "unknown")
    await courier_credentials.set_account_environment(db, body.environment, actor=actor)
    courier_coverage_worker.sync_soon("courier settings changed")
    return await _credentials_payload(db)


@router.put("/credentials/branches/{branch_id}/key", response_model=CourierCredentialsResponse)
async def set_branch_courier_key(
    branch_id: UUID,
    body: CourierKeyUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CourierCredentialsResponse:
    """
    Give one branch its own SribeesExpress account for an environment, or take
    it back.

    For a branch that signs with SribeesExpress in its own name: its parcels,
    its pricing origin and its COD ledger all then live in that account. After
    setting one, register the webhook for that account too.
    """
    branch = await _load_branch(db, branch_id)
    actor = getattr(current_admin, "email", "unknown")
    try:
        await courier_credentials.set_branch_api_key(
            db, branch, body.environment, body.api_key, actor=actor
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    courier_coverage_worker.sync_soon("courier settings changed")
    return await _credentials_payload(db)


@router.put(
    "/credentials/branches/{branch_id}/environment",
    response_model=CourierCredentialsResponse,
)
async def set_branch_courier_environment(
    branch_id: UUID,
    body: CourierEnvironmentUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CourierCredentialsResponse:
    """
    Pin one branch to an environment, or return it to following the account.
    """
    branch = await _load_branch(db, branch_id)
    actor = getattr(current_admin, "email", "unknown")
    try:
        await courier_credentials.set_branch_environment(
            db, branch, body.environment, actor=actor
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    courier_coverage_worker.sync_soon("courier settings changed")
    return await _credentials_payload(db)


@router.post("/credentials/test", response_model=CourierKeyTestResult)
async def test_courier_key(
    branch_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CourierKeyTestResult:
    """
    Make a real, side-effect-free call with whichever credentials serve this
    scope.

    ``GET /ecommerce/postal-cities`` is the probe: it is a read, it needs a
    valid key, and its row count is a useful answer in itself — a key that
    authenticates but sees zero postal cities is pointed at an environment with
    no delivery areas, which fails later and far more confusingly.
    """
    try:
        target = await courier_credentials.resolve(db, branch_id)
    except _NOT_CONFIGURED as exc:
        return CourierKeyTestResult(ok=False, detail=str(exc))

    try:
        async with courier_credentials.using(target):
            rows = await CourierService.list_postal_cities()
    except (CourierUnavailableError, CourierError) as exc:
        return CourierKeyTestResult(
            ok=False,
            environment=target.environment,
            key_source=target.key_source,
            detail=str(exc)[:300],
        )

    return CourierKeyTestResult(
        ok=True,
        environment=target.environment,
        key_source=target.key_source,
        postal_cities=len(rows),
        detail=(
            f"Authenticated against the {target.environment} environment using the "
            f"{target.key_source} key; {len(rows)} serviceable postal city(ies) visible."
        ),
    )


# ---------------------------------------------------------------------------
# COD remittances (read-only)
# ---------------------------------------------------------------------------


@router.get("/cod-balance", response_model=dict)
async def get_cod_balance(
    branch_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    current_admin=_COURIER_READ,
) -> dict:
    """
    Cash SribeesExpress has collected from our customers and not yet paid out
    (contract section 14), for the account serving ``branch_id`` (a Branch
    Manager: their own branch; a Super Admin: any branch, or the account).
    """
    scope_branch = _scoped_branch_id(current_admin, branch_id)
    try:
        async with courier_credentials.for_branch(db, scope_branch):
            data = await CourierService.get_cod_balance()
    except _NOT_CONFIGURED as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except (CourierUnavailableError, CourierError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return {"success": True, "data": data}


@router.get("/remittances", response_model=dict)
async def list_remittances(
    limit: int = 50,
    offset: int = 0,
    branch_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    current_admin=_COURIER_READ,
) -> dict:
    """
    Our COD payout history (contract section 14), for the account serving
    ``branch_id``. Read-only — generating a run and marking it paid are staff
    actions on SribeesExpress's side.
    """
    scope_branch = _scoped_branch_id(current_admin, branch_id)
    try:
        async with courier_credentials.for_branch(db, scope_branch):
            data = await CourierService.list_remittances(limit=limit, offset=offset)
    except _NOT_CONFIGURED as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except (CourierUnavailableError, CourierError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return {"success": True, "data": data}


@router.get("/remittances/{remittance_id}/shipments", response_model=dict)
async def get_remittance_shipments(
    remittance_id: int,
    branch_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    current_admin=_COURIER_READ,
) -> dict:
    """
    Which shipments a given payout covered, joined back to our own order
    numbers so finance can tie a bank credit to real orders.

    Also backfills ``courier_remittance_id`` on the matching orders.
    """
    scope_branch = _scoped_branch_id(current_admin, branch_id)
    try:
        async with courier_credentials.for_branch(db, scope_branch):
            rows = await CourierService.get_remittance_shipments(remittance_id)
    except _NOT_CONFIGURED as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except (CourierUnavailableError, CourierError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    matched = 0
    for row in rows:
        shipment_id = row.get("shipment_id")
        if not shipment_id:
            continue
        order = (
            await db.execute(select(Order).where(Order.courier_shipment_id == shipment_id))
        ).scalar_one_or_none()
        if order is None:
            continue
        row["merchant_order_ref"] = row.get("merchant_order_ref") or order.order_number
        if order.courier_remittance_id != remittance_id:
            order.courier_remittance_id = remittance_id
            matched += 1
    if matched:
        await db.commit()
        logger.info(f"Remittance {remittance_id}: marked {matched} order(s) settled")

    return {"success": True, "data": rows, "orders_updated": matched}


# ---------------------------------------------------------------------------
# Webhook registration
# ---------------------------------------------------------------------------


@router.post("/webhook-endpoint", response_model=WebhookRegistrationResponse)
async def register_webhook_endpoint(
    body: WebhookRegistrationRequest,
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> WebhookRegistrationResponse:
    """
    Register our webhook URL with one SribeesExpress account (contract section
    10.1) — the account key's, or ``branch_id``'s own.

    **The signing secret is stored here, encrypted, and never returned.**
    SribeesExpress shows it exactly once; handing it to an operator to paste
    into an environment file is how it leaks and how a forgotten redeploy
    becomes a week of rejected webhooks. Re-registering a URL the account
    already has rotates its secret instead, since SribeesExpress refuses a
    duplicate URL.
    """
    if not body.url.startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SribeesExpress only accepts an https:// webhook URL.",
        )
    branch_uuid: Optional[UUID] = None
    if body.branch_id:
        try:
            branch_uuid = UUID(body.branch_id)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid branch_id")
        await _load_branch(db, branch_uuid)

    try:
        target = await courier_credentials.resolve(db, branch_uuid)
    except _NOT_CONFIGURED as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if branch_uuid is not None and target.key_source != "branch":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This branch has no key of its own in the environment it books in; its "
                "parcels push to the account-wide registration. Register without branch_id."
            ),
        )

    try:
        async with courier_credentials.using(target):
            try:
                result = await CourierService.register_webhook_endpoint(
                    url=body.url, description=body.description
                )
            except CourierError as exc:
                if exc.code != "webhook_url_already_registered":
                    raise
                existing = next(
                    (e for e in await CourierService.list_webhook_endpoints() if e.get("url") == body.url),
                    None,
                )
                if existing is None:
                    raise
                result = await CourierService.rotate_webhook_secret(existing["id"])
    except (CourierUnavailableError, CourierError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    secret = result.get("secret")
    if secret:
        await courier_credentials.store_webhook_secret(
            db, target, secret, actor=getattr(current_admin, "email", "unknown")
        )
    return WebhookRegistrationResponse(
        endpoint_id=result.get("id"),
        url=result.get("url") or body.url,
        environment=target.environment,
        key_source=target.key_source,
        secret_stored=bool(secret),
    )
