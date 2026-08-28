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

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_admin
from app.models.branch import Branch
from app.schemas.courier import (
    CourierSweepResponse,
    PickupLocationSyncResponse,
    PickupLocationSyncResult,
)
from app.services import courier_sync_service
from app.services.courier_service import CourierError, CourierService, CourierUnavailableError

router = APIRouter(tags=["Admin - Courier"])


@router.post("/pickup-locations/sync", response_model=PickupLocationSyncResponse)
async def sync_pickup_locations(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
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

        payload = dict(
            name=branch.name,
            phone=branch.phone,
            address=branch.address,
            post_office=branch.post_office,
            district=branch.district,
            province=branch.province,
        )

        try:
            if branch.courier_pickup_location_id:
                result = await CourierService.update_pickup_location(
                    branch.courier_pickup_location_id, **payload
                )
                action = "updated"
            else:
                result = await CourierService.create_pickup_location(**payload)
                action = "created"
        except (CourierUnavailableError, CourierError) as exc:
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
    current_admin=Depends(get_current_admin),
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


@router.post("/webhook-endpoint", response_model=dict)
async def register_webhook_endpoint(
    url: str,
    description: str = "",
    current_admin=Depends(get_current_admin),
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
        result = await CourierService.register_webhook_endpoint(url=url, description=description)
    except (CourierUnavailableError, CourierError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    logger.warning(
        "Courier webhook endpoint registered -- the secret in this response is shown ONCE. "
        "Store it in COURIER_WEBHOOK_SECRET now."
    )
    return {"success": True, "data": result}
