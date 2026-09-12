"""
Admin Courier Operations — SribeesExpress account plumbing.

Three things have to happen outside the customer-facing flow, and none of
them belong on a checkout path:

* **Pickup locations.** Every branch that ships must be registered with
  SribeesExpress once so the rider collects from the right address
  (``docs/SRIBEESEXPRESS_ECOMMERCE_API.md`` section 12). Run the sync after
  adding a branch or changing its address.
* **Reconciliation sweep.** SribeesExpress does not retry webhooks (section
  10.4), so ``GET /ecommerce/shipments?updated_since=`` is the real source of
  truth. This backend has no job scheduler, so the sweep is exposed as an
  endpoint for an external cron to call — and for ops to run by hand after an
  outage.
* **Webhook registration.** The secret is shown exactly once (section 10.1),
  so registration is a deliberate, audited action rather than a startup hook
  that would silently mint a new secret on every deploy.

Prefix "/admin/courier" is applied by app/api/v1/router.py.
"""
from datetime import datetime, timezone
from uuid import UUID
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_admin, require_roles
from app.models.branch import Branch
from app.models.order import Order
from app.schemas.courier import (
    CourierCredentialsResponse,
    CourierKeyStatus,
    CourierKeyTestResult,
    CourierKeyUpdate,
    CourierSweepResponse,
    CoverageSyncResponse,
    PickupLocationSyncResponse,
    PickupLocationSyncResult,
)
from app.services.app_settings_service import AppSettingsService
from app.services import (
    courier_coverage_service,
    courier_credentials,
    courier_sync_service,
)
from app.services.courier_service import (
    CourierError,
    CourierService,
    CourierUnavailableError,
)

router = APIRouter(tags=["Admin - Courier"])

# Every route here touches the SribeesExpress ACCOUNT, not one order: pickup
# addresses for the whole estate, the webhook secret, the COD ledger. None of
# it is branch-scoped and none of it is delegatable (`settings` is excluded
# from DELEGATABLE_PERMISSIONS by design), so these are role-gated rather than
# permission-gated. Before this, any authenticated admin -- Customer Support
# included -- could re-register the webhook endpoint and invalidate the secret
# every inbound status update is verified against.
_ACCOUNT_OPS = Depends(require_roles("super_admin"))
# Reading what SribeesExpress owes us is a finance/ops question a Branch
# Manager legitimately asks about their own deliveries, so reads open one
# role wider than the writes.
_COURIER_READ = Depends(require_roles("super_admin", "branch_manager"))


async def _recover_pickup_location_by_name(name: str) -> Optional[dict]:
    """
    Find an already-registered pickup location by name (case-insensitive).

    Only used to recover from ``pickup_location_name_already_registered``:
    SribeesExpress refuses a duplicate name rather than silently returning the
    existing row, so a merchant that lost its stored ids has to look it up.
    """
    for location in await CourierService.list_pickup_locations():
        if (location.get("name") or "").strip().lower() == name.strip().lower():
            return location
    return None


async def _register_one_pickup_location(db, branch, payload: dict) -> tuple[dict, str]:
    """
    Register (or update) one branch's pickup location, in that branch's own
    SribeesExpress account.

    The scope matters: a pickup location only exists inside the account that
    created it, so a branch with its own merchant key must register in that
    account. Registering it under the account-wide key instead would store an
    id that resolves to nothing at booking time, and the failure would not
    surface until a rider was already expected.

    Returns ``(location, action)`` where action is created | updated | recovered.
    """
    async with courier_credentials.for_branch(db, branch.branch_id):
        if branch.courier_pickup_location_id:
            return (
                await CourierService.update_pickup_location(
                    branch.courier_pickup_location_id, **payload
                ),
                "updated",
            )
        try:
            return await CourierService.create_pickup_location(**payload), "created"
        except CourierError as exc:
            if exc.code != "pickup_location_name_already_registered":
                raise
            # We lost the id (restored from a backup, or a previous sync stored
            # nothing) but SribeesExpress still holds the location. Recover it
            # by name rather than leaving the branch permanently unregistered --
            # creating a second one is refused, by design, so this is the only
            # way back.
            recovered = await _recover_pickup_location_by_name(branch.name)
            if recovered is None:
                raise
            return recovered, "recovered"


@router.post("/pickup-locations/sync", response_model=PickupLocationSyncResponse)
async def sync_pickup_locations(
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> PickupLocationSyncResponse:
    """
    Register every active branch as a SribeesExpress pickup location, or
    update the one already registered (contract section 12).

    Safe to re-run: a branch that already holds a ``courier_pickup_location_id``
    is PATCHed, never re-created, so ids stay stable and shipments already
    booked against them keep resolving. Branches are processed independently —
    one failure does not abort the rest, since a half-registered estate is
    still better than none.

    Editing a pickup location never changes an already-booked shipment: its
    pickup address is a snapshot taken at booking time.
    """
    branches = (
        (await db.execute(select(Branch).where(Branch.is_active.is_(True)).order_by(Branch.code)))
        .scalars()
        .all()
    )

    results: list[PickupLocationSyncResult] = []
    synced = failed = 0

    for branch in branches:
        if not branch.address:
            results.append(
                PickupLocationSyncResult(
                    branch_code=branch.code,
                    branch_name=branch.name,
                    action="skipped",
                    detail="Branch has no address -- a rider cannot be sent to it.",
                )
            )
            continue

        payload = {
            "name": branch.name,
            "phone": branch.phone,
            "address": branch.address,
            "post_office": branch.post_office,
            "district": branch.district,
            "province": branch.province,
        }

        try:
            result, action = await _register_one_pickup_location(db, branch, payload)
        except (CourierUnavailableError, CourierError, courier_credentials.CourierKeyMissingError) as exc:
            failed += 1
            logger.error(f"Pickup location sync failed for branch {branch.code}: {exc}")
            results.append(
                PickupLocationSyncResult(
                    branch_code=branch.code,
                    branch_name=branch.name,
                    pickup_location_id=branch.courier_pickup_location_id,
                    action="failed",
                    detail=str(exc)[:300],
                )
            )
            continue

        location_id = result.get("id") or branch.courier_pickup_location_id
        branch.courier_pickup_location_id = location_id
        branch.courier_pickup_synced_at = datetime.now(timezone.utc)
        synced += 1
        results.append(
            PickupLocationSyncResult(
                branch_code=branch.code,
                branch_name=branch.name,
                pickup_location_id=location_id,
                action=action,
            )
        )

    await db.commit()
    logger.info(f"Courier pickup-location sync: {synced} synced, {failed} failed")
    return PickupLocationSyncResponse(synced=synced, failed=failed, results=results)


@router.post("/sync", response_model=CourierSweepResponse)
async def run_reconciliation_sweep(
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CourierSweepResponse:
    """
    Run the shipment reconciliation sweep now (contract section 7a).

    Intended to be called on a schedule (external cron; every 10-15 minutes is
    a reasonable starting point) and by ops after a webhook outage. Idempotent
    — applying a shipment payload twice changes nothing the second time — so
    an overlapping run is harmless.
    """
    seen, applied = await courier_sync_service.sweep(db)
    return CourierSweepResponse(seen=seen, applied=applied)


def _key_status(
    *,
    scope: str,
    own_key: Optional[str],
    effective_source: str,
    branch=None,
) -> CourierKeyStatus:
    """Describe one slot for the UI without handing over the credential."""
    return CourierKeyStatus(
        scope=scope,
        branch_id=str(branch.branch_id) if branch is not None else None,
        branch_code=branch.code if branch is not None else None,
        branch_name=branch.name if branch is not None else None,
        is_set=bool(own_key),
        masked=courier_credentials.mask(own_key),
        environment=courier_credentials.environment_of(own_key),
        source=effective_source,
        set_at=(
            branch.courier_api_key_set_at.isoformat()
            if branch is not None and branch.courier_api_key_set_at
            else None
        ),
        set_by=branch.courier_api_key_set_by if branch is not None else None,
    )


@router.get("/credentials", response_model=CourierCredentialsResponse)
async def get_courier_credentials(
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CourierCredentialsResponse:
    """
    Which SribeesExpress account each branch books against.

    Returns masks, never keys — there is no endpoint that reads a key back,
    only ones that replace it. A branch with no key of its own is shown as
    booking against the account, so the page answers "where does this branch's
    parcel actually go?" rather than just "is a key set?".
    """
    account_raw = await courier_credentials.account_key(db)
    db_row = await AppSettingsService.get_by_key(db, courier_credentials.ACCOUNT_KEY)
    from_db = bool(db_row and db_row.is_active and (db_row.value or "").strip())

    account = _key_status(
        scope="account",
        own_key=account_raw,
        effective_source=("account" if from_db else "environment") if account_raw else "none",
    )

    branches = (
        (await db.execute(select(Branch).order_by(Branch.code))).scalars().all()
    )
    rows = [
        _key_status(
            scope="branch",
            own_key=branch.courier_api_key,
            effective_source=(
                "branch"
                if branch.courier_api_key
                else (("account" if from_db else "environment") if account_raw else "none")
            ),
            branch=branch,
        )
        for branch in branches
    ]
    return CourierCredentialsResponse(account=account, branches=rows)


@router.put("/credentials", response_model=CourierCredentialsResponse)
async def set_account_courier_key(
    body: CourierKeyUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CourierCredentialsResponse:
    """
    Set the account-wide key — the one every branch without its own key books
    against, and the one the sandbox → live switch turns.

    Clearing it falls back to the deployment's own ``COURIER_API_KEY``, not to
    "no courier": undoing a mistaken change should land somewhere that works.
    """
    actor = getattr(current_admin, "email", "unknown")
    await courier_credentials.set_account_key(db, body.api_key, actor=actor)
    return await get_courier_credentials(db=db, current_admin=current_admin)


@router.put("/credentials/branches/{branch_id}", response_model=CourierCredentialsResponse)
async def set_branch_courier_key(
    branch_id: UUID,
    body: CourierKeyUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CourierCredentialsResponse:
    """
    Give one branch its own SribeesExpress account, or take it back.

    For a branch that signs with SribeesExpress in its own name: its parcels,
    its pickup locations and its COD ledger all then live in that account.
    After setting one, re-run the pickup-location sync — the branch's pickup
    address exists in the old account, not the new one.
    """
    branch = (
        await db.execute(select(Branch).where(Branch.branch_id == branch_id))
    ).scalar_one_or_none()
    if branch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")

    actor = getattr(current_admin, "email", "unknown")
    await courier_credentials.set_branch_key(db, branch, body.api_key, actor=actor)
    return await get_courier_credentials(db=db, current_admin=current_admin)


@router.post("/credentials/test", response_model=CourierKeyTestResult)
async def test_courier_key(
    branch_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CourierKeyTestResult:
    """
    Make a real, side-effect-free call with whichever key serves this scope.

    ``GET /ecommerce/coverage`` is the probe: it is a read, it needs a valid
    key, and its row count is a useful answer in itself — a key that
    authenticates but sees zero post offices is configured against an account
    with no delivery areas, which fails later and much more confusingly.
    """
    try:
        key = await courier_credentials.resolve(db, branch_id)
    except courier_credentials.CourierKeyMissingError as exc:
        return CourierKeyTestResult(ok=False, detail=str(exc))

    try:
        async with courier_credentials.using(key):
            rows = await CourierService.get_coverage()
    except (CourierUnavailableError, CourierError) as exc:
        return CourierKeyTestResult(
            ok=False,
            environment=courier_credentials.environment_of(key),
            detail=str(exc)[:300],
        )

    return CourierKeyTestResult(
        ok=True,
        environment=courier_credentials.environment_of(key),
        post_offices=len(rows),
        detail=(
            f"Authenticated against the {courier_credentials.environment_of(key)} "
            f"environment; {len(rows)} serviceable post office(s) visible."
        ),
    )


@router.post("/coverage/sync", response_model=CoverageSyncResponse)
async def sync_coverage(
    db: AsyncSession = Depends(get_db),
    current_admin=_ACCOUNT_OPS,
) -> CoverageSyncResponse:
    """
    Rebuild ``post_office_directory`` from ``GET /ecommerce/coverage``.

    This is what makes a real customer address quotable. Our directory was
    hand-seeded in migration 018 and two of its three names do not match
    SribeesExpress's spelling, which reaches the customer as "not
    serviceable" — so run this before the first live order, and again
    whenever SribeesExpress says they have configured another district.

    Unlike the address-level resolve, a failure here is returned as a 502
    rather than swallowed: an operator asked for this and needs to know it
    did not happen.
    """
    try:
        result = await courier_coverage_service.sync(db)
    except (CourierUnavailableError, CourierError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return CoverageSyncResponse(**result.to_dict())


@router.get("/cod-balance", response_model=dict)
async def get_cod_balance(current_admin=_COURIER_READ) -> dict:
    """
    Cash SribeesExpress has collected from our customers and not yet paid out
    (contract section 14).

    This is the number to watch, because their remittance runs are triggered
    by their ops team rather than a scheduler — nothing else would tell us a
    payout is overdue. Cross-check it against our own ledger of
    ``courier_cod_collected`` orders with no ``courier_remittance_id``; a
    persistent gap between the two is a reconciliation problem worth raising
    with them.
    """
    try:
        async with courier_credentials.for_account(db):
            data = await CourierService.get_cod_balance()
    except (CourierUnavailableError, CourierError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return {"success": True, "data": data}


@router.get("/remittances", response_model=dict)
async def list_remittances(
    limit: int = 50,
    offset: int = 0,
    current_admin=_COURIER_READ,
) -> dict:
    """
    Our COD payout history (contract section 14). Read-only — generating a run
    and marking it paid are staff actions on SribeesExpress's side.

    ``status`` is ``pending`` until their ops confirms the bank transfer
    landed, then ``paid`` with a ``bank_reference`` to match against our
    statement.
    """
    try:
        async with courier_credentials.for_account(db):
            data = await CourierService.list_remittances(limit=limit, offset=offset)
    except (CourierUnavailableError, CourierError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return {"success": True, "data": data}


@router.get("/remittances/{remittance_id}/shipments", response_model=dict)
async def get_remittance_shipments(
    remittance_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin=_COURIER_READ,
) -> dict:
    """
    Which shipments a given payout covered, joined back to our own order
    numbers so finance can tie a bank credit to real orders.

    Also backfills ``courier_remittance_id`` on the matching orders: a
    settlement usually reaches us through this read rather than a status
    webhook, since being remitted is not a shipment status change.
    """
    try:
        async with courier_credentials.for_account(db):
            rows = await CourierService.get_remittance_shipments(remittance_id)
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


@router.post("/webhook-endpoint", response_model=dict)
async def register_webhook_endpoint(
    url: str,
    description: str = "",
    current_admin=_ACCOUNT_OPS,
) -> dict:
    """
    Register our webhook URL with SribeesExpress (contract section 10.1).

    **The returned secret is shown exactly once** and can never be read back —
    only rotated, which immediately invalidates the old one. Copy it into
    ``COURIER_WEBHOOK_SECRET`` and redeploy before the next status change, or
    every inbound push will fail signature verification.

    Deliberately not automatic on startup: a redeploy that silently minted a
    new endpoint would leave a trail of dead registrations and a secret nobody
    captured.
    """
    if not url.startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SribeesExpress only accepts an https:// webhook URL.",
        )
    try:
        async with courier_credentials.for_account(db):
            result = await CourierService.register_webhook_endpoint(
                url=url, description=description
            )
    except (CourierUnavailableError, CourierError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    logger.warning(
        "Courier webhook endpoint registered -- the secret in this response is shown ONCE. "
        "Store it in COURIER_WEBHOOK_SECRET now."
    )
    return {"success": True, "data": result}
