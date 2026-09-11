"""
Courier Webhooks — inbound endpoints SribeesExpress calls INTO us.

Authenticated by **HMAC signature**, not a shared API key: the contract
(``docs/SRIBEESEXPRESS_ECOMMERCE_API.md`` section 10.3) signs every push with
the secret issued once at ``POST /ecommerce/webhook-endpoints`` registration,
which lives in ``settings.courier_webhook_secret``. The old
``courier_inbound_api_key`` scheme is gone with the old contract.

Two properties matter here and neither is optional:

* **Verify before parsing.** The signature covers the raw body, so the body is
  read as bytes and checked before anything is deserialized. An unverified
  payload is never allowed to reach the order table.
* **Dedupe on ``event_id``.** Contract section 10.2 calls it a stable dedupe
  key. Every event is recorded in ``courier_webhook_events`` first; a repeat
  is acknowledged and dropped. That table is also the audit trail — with no
  webhook retry on SribeesExpress's side (section 10.4), it is what a
  "we never got the delivered event" investigation actually reads.

A ``200`` from us means "recorded", not "the order moved". Anything we cannot
match to an order is still stored, still acknowledged, and picked up later by
``courier_sync_service.sweep``.
"""
import hashlib
import hmac
import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.services import courier_sync_service
from app.services.order_service import OrderService

# Prefix "/courier/webhooks" is applied by app/api/v1/router.py.
router = APIRouter(tags=["Courier Webhooks"])


def _parse_signature(header: str) -> tuple[Optional[str], Optional[str]]:
    """Split ``t=<unix>,v1=<hex>`` into its parts, tolerating spacing."""
    timestamp = signature = None
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signature = value
    return timestamp, signature


async def verify_signature(
    request: Request,
    x_sribees_signature: str = Header(..., alias="X-Sribees-Signature"),
) -> bytes:
    """
    Verify ``X-Sribees-Signature`` over ``"{t}.{raw body}"`` and return the raw
    body (contract section 10.3).

    The timestamp is checked against ``courier_webhook_tolerance_seconds``
    before the HMAC: a valid signature replayed days later is still a replay,
    and the signed timestamp is the only thing that distinguishes it.
    """
    if not settings.courier_webhook_secret:
        # Fail closed. An unconfigured secret must never mean "accept
        # everything" -- this endpoint writes to orders.
        logger.error("Courier webhook received but courier_webhook_secret is not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook verification is not configured",
        )

    timestamp, provided = _parse_signature(x_sribees_signature)
    if not timestamp or not provided:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed signature")

    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed signature")
    if age > settings.courier_webhook_tolerance_seconds:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Signature expired")

    raw_body = await request.body()
    expected = hmac.new(
        settings.courier_webhook_secret.encode(),
        f"{timestamp}.".encode() + raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, provided):
        logger.warning("Courier webhook signature mismatch -- payload rejected")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    return raw_body


async def _record_event(db: AsyncSession, payload: dict) -> bool:
    """
    Store the event; return False if this ``event_id`` was already seen.

    ON CONFLICT DO NOTHING is the dedupe: it is a single atomic statement, so
    two concurrent deliveries of the same event cannot both win it.
    """
    event_id = payload.get("event_id")
    if not event_id:
        # No dedupe key means we cannot tell a repeat from a new event.
        # Process it -- applying a payload is idempotent anyway -- but say so.
        logger.warning("Courier webhook arrived without an event_id")
        return True

    result = await db.execute(
        text(
            "INSERT INTO courier_webhook_events "
            "  (event_id, event_type, shipment_id, status, payload) "
            "VALUES (:event_id, :event_type, :shipment_id, :status, CAST(:payload AS JSONB)) "
            "ON CONFLICT (event_id) DO NOTHING"
        ),
        {
            "event_id": event_id,
            "event_type": payload.get("event") or "unknown",
            "shipment_id": payload.get("shipment_id"),
            "status": payload.get("status"),
            "payload": json.dumps(payload),
        },
    )
    await db.commit()
    return result.rowcount > 0


@router.post("/shipment-status")
async def shipment_status_webhook(
    raw_body: bytes = Depends(verify_signature),
    db: AsyncSession = Depends(get_db),
):
    """
    ``shipment.status_changed`` push (contract section 10.2).

    On a transition to ``delivered`` the payload also carries the ``handover``
    and ``cod`` blocks, and on ``failed``/``returned`` a ``failure_note`` —
    all of which ``courier_sync_service`` persists. This is how we learn a COD
    collection actually happened without a separate poll.
    """
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed JSON")

    shipment_id = payload.get("shipment_id")
    if not shipment_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing shipment_id")

    is_new = await _record_event(db, payload)
    if not is_new:
        logger.info(f"Courier webhook {payload.get('event_id')} already processed, ignoring repeat")
        return {"success": True, "duplicate": True}

    order = await OrderService.get_by_courier_shipment_id(db, shipment_id)
    if not order:
        # Acknowledged, not rejected: a 4xx here would make SribeesExpress log
        # a delivery failure for an event that is genuinely not theirs to fix,
        # and the event is already stored for investigation.
        logger.warning(f"Courier webhook for unknown shipment {shipment_id}")
        return {"success": True, "matched": False}

    await courier_sync_service.apply_shipment_payload(db, order, payload)
    logger.info(
        f"Courier webhook: shipment {shipment_id} -> {payload.get('status')} "
        f"(order {order.order_number})"
    )
    return {"success": True, "matched": True}
