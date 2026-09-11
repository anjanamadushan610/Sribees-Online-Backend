"""
Delivery Slots API

The dispatch rounds a customer can choose when checking out.

``app/main.py`` has advertised ``/delivery/slots`` in its route index since
before anything could answer it — the endpoint simply did not exist, so the app
had no way to ask when an order would arrive and ``orders.delivery_slot_time``
was a free-text string the client made up. This implements it against
``branches.delivery_runs``, the same per-branch timetable the Marketing
Dashboard counts its cut-off down to, so the slot a customer picks and the run
a manager watches are by construction the same thing.

Prefix "/delivery" is applied by app/api/v1/router.py.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.products import _get_branch_context_for_request
from app.config.database import get_db
from app.config.redis import get_redis
from app.core.dependencies import get_current_user_optional
from app.models.branch import Branch
from app.services.marketing_dashboard_service import LK_TZ, _parse_hhmm

router = APIRouter(tags=["Delivery"])


@router.get("/slots", response_model=dict)
async def delivery_slots(
    request: Request,
    days: int = Query(2, ge=1, le=7, description="How many days ahead to offer"),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user_optional),
):
    """
    Delivery slots this customer can actually choose right now.

    A run whose ordering cut-off has already passed is **omitted**, not returned
    flagged as unavailable: the van is loaded, nothing more can be added to it,
    and offering a slot that checkout would refuse is worse than not offering
    it. This is why today's list shrinks as the day goes on.

    Branch comes from the customer's own delivery context, never from the
    request body — a client cannot ask for another branch's timetable and then
    order against it.
    """
    context = await _get_branch_context_for_request(redis, request, current_user)
    branch_id = UUID(context["branch_id"])

    branch = (
        await db.execute(select(Branch).where(Branch.branch_id == branch_id))
    ).scalar_one_or_none()
    if branch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "success": False,
                "error": {"message": "Branch not found", "code": "BRANCH_NOT_FOUND"},
            },
        )

    # Tolerate a malformed row rather than 500ing the whole checkout: a bad
    # entry costs one slot, an exception costs the customer their order.
    runs = []
    for entry in branch.delivery_runs or []:
        if not isinstance(entry, dict):
            continue
        dispatch = _parse_hhmm(entry.get("dispatch_time"))
        if dispatch is None:
            continue
        try:
            cutoff_minutes = max(0, int(entry.get("cutoff_minutes", 0)))
        except (TypeError, ValueError):
            cutoff_minutes = 0
        runs.append((dispatch, str(entry.get("label") or "Delivery run"), cutoff_minutes))
    runs.sort(key=lambda r: r[0])

    now = datetime.now(timezone.utc).astimezone(LK_TZ)
    slots = []
    for day_offset in range(days):
        day = (now + timedelta(days=day_offset)).date()
        for dispatch, label, cutoff_minutes in runs:
            dispatch_at = datetime.combine(day, dispatch, tzinfo=LK_TZ)
            cutoff_at = dispatch_at - timedelta(minutes=cutoff_minutes)
            if cutoff_at <= now:
                continue
            slots.append(
                {
                    "slotId": f"{day.isoformat()}T{dispatch.strftime('%H:%M')}",
                    "date": day.isoformat(),
                    "label": label,
                    "dispatchAt": dispatch_at.isoformat(),
                    "orderBy": cutoff_at.isoformat(),
                    "minutesToCutoff": int((cutoff_at - now).total_seconds() // 60),
                    "isToday": day_offset == 0,
                }
            )

    return {
        "success": True,
        "data": {
            "slots": slots,
            "branch": {
                "branchId": str(branch_id),
                "branchName": branch.name,
            },
            # An empty list with configured=False means "this branch has not set
            # its rounds up yet", which is a completely different message to the
            # customer than "everything for today is closed". The app must not
            # show the same screen for both.
            "configured": bool(runs),
        },
    }
