"""
Internal: the event feed Sribees Finance pulls (``GET /internal/finance/events``).

Not for browsers, apps or partners. Three locks, all required:

* **Service token.** ``Authorization: Bearer <token>``; only its SHA-256 is in
  our settings (``FINANCE_SERVICE_TOKEN_SHA256``), compared in constant time.
  No token configured = the route answers 404, as if it did not exist.
* **Never through the edge.** Finance calls the app container over the internal
  Docker network. A request that came through Caddy carries
  ``X-Forwarded-For``; such a request is refused whatever token it has.
* **IP allow-list** (``FINANCE_ALLOWED_IPS``, comma-separated addresses or
  CIDRs) on the connecting peer, when set.

Reads only. Rows come back in ``seq`` order after the caller's cursor. A
transaction that commits late can leave a lower ``seq`` appear after a higher
one; Finance keeps a cursor that only advances past gaps that have stayed
gaps for a while, and dedupes on ``event_id``, so nothing is skipped or
counted twice.
"""
import hashlib
import hmac
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.models.finance_outbox import FinanceOutbox

router = APIRouter(prefix="/internal/finance", tags=["Internal: Finance"], include_in_schema=False)


def _allowed(peer: str) -> bool:
    raw = (settings.finance_allowed_ips or "").strip()
    if not raw:
        return True
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for part in raw.split(","):
        part = part.strip()
        if part and ip in ipaddress.ip_network(part, strict=False):
            return True
    return False


def require_finance_service(request: Request) -> None:
    expected = (settings.finance_service_token_sha256 or "").strip().lower()
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if request.headers.get("x-forwarded-for") or request.headers.get("forwarded"):
        raise HTTPException(status_code=403, detail="not available from outside")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    presented = hashlib.sha256(token.encode()).hexdigest() if token else ""
    if not token or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="service token required")
    peer = request.client.host if request.client else ""
    if not _allowed(peer):
        raise HTTPException(status_code=403, detail="caller not allowed")


@router.get("/events")
async def finance_events(
    after: int = Query(default=0, ge=0, description="return events with seq greater than this"),
    limit: int = Query(default=500, ge=1, le=2000),
    _: None = Depends(require_finance_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(
            select(FinanceOutbox).where(FinanceOutbox.seq > after).order_by(FinanceOutbox.seq).limit(limit)
        )
    ).scalars().all()
    return {
        "events": [
            {
                "seq": r.seq,
                "event_id": str(r.event_id),
                "type": r.event_type,
                "aggregate_id": str(r.aggregate_id) if r.aggregate_id else None,
                "occurred_at": r.occurred_at.isoformat(),
                "payload": r.payload,
            }
            for r in rows
        ],
        "enabled": bool(settings.finance_outbox_enabled),
    }
