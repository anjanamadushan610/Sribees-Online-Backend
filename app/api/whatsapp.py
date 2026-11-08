"""Meta webhook verification and signed message receipt."""

import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.services.whatsapp_menu import valid_signature
from app.services.whatsapp_service import receive_messages

router = APIRouter(prefix="/api/v1/whatsapp", tags=["WhatsApp"])
MAX_BODY_BYTES = 1024 * 1024


@router.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(request: Request):
    params = request.query_params
    token = settings.whatsapp_verify_token
    if not settings.whatsapp_enabled:
        raise HTTPException(404, "Not found")
    if not token:
        raise HTTPException(503, "WhatsApp is not configured")
    if params.get("hub.mode") != "subscribe" or not hmac.compare_digest(
        params.get("hub.verify_token", "").encode(), token.encode()
    ):
        raise HTTPException(403, "Invalid verification")
    challenge = params.get("hub.challenge", "")
    if not challenge or len(challenge) > 1024:
        raise HTTPException(400, "Invalid challenge")
    return PlainTextResponse(challenge)


@router.post("/webhook")
async def receive_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    if not settings.whatsapp_enabled:
        raise HTTPException(404, "Not found")
    if not settings.whatsapp_app_secret or not settings.whatsapp_phone_number_id:
        raise HTTPException(503, "WhatsApp is not configured")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "Webhook is too large")
    if not valid_signature(
        bytes(body),
        request.headers.get("x-hub-signature-256", ""),
        settings.whatsapp_app_secret,
    ):
        raise HTTPException(403, "Invalid signature")
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError()
        await receive_messages(db, payload, settings.whatsapp_phone_number_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(400, "Invalid webhook payload") from exc
    return {"received": True}
