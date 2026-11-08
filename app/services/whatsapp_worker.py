"""Durable sends. Ambiguous sends require reconciliation, never blind retries."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from loguru import logger
from sqlalchemy import select, update

from app.config.database import async_session_maker
from app.config.settings import settings
from app.models.whatsapp import WhatsAppReply


async def send_next(client: httpx.AsyncClient) -> bool:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as db:
        # A process stopped after recording 'sending' might have delivered the
        # message already. Do not resend it without checking Meta's receipts.
        await db.execute(
            update(WhatsAppReply)
            .where(
                WhatsAppReply.status == "sending",
                WhatsAppReply.updated_at < now - timedelta(minutes=2),
            )
            .values(status="uncertain")
        )
        job = (
            await db.execute(
                select(WhatsAppReply)
                .where(
                    WhatsAppReply.status == "pending",
                    WhatsAppReply.next_attempt_at <= now,
                )
                .order_by(WhatsAppReply.created_at, WhatsAppReply.reply_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if job is None:
            await db.commit()
            return False
        if job.expires_at <= now:
            job.status = "expired"
            await db.commit()
            return True
        job.status = "sending"
        job.attempts += 1
        job_id, phone, payload = job.reply_id, job.phone, job.payload
        attempts = job.attempts
        await db.commit()

    result = "uncertain"
    retry_at = now
    try:
        response = await client.post(
            f"https://graph.facebook.com/{settings.whatsapp_graph_version}/{settings.whatsapp_phone_number_id}/messages",
            headers={"Authorization": "Bearer " + settings.whatsapp_access_token},
            json={"messaging_product": "whatsapp", "to": phone, **payload},
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("messages", [{}])[0].get("id"):
                result = "accepted"
        elif response.status_code == 429 and attempts < 5:
            result = "pending"
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=60 * attempts)
        elif 400 <= response.status_code < 500:
            result = "failed"
    except (
        httpx.HTTPError,
        ValueError,
        KeyError,
        IndexError,
        AttributeError,
        TypeError,
    ):
        # Never log response bodies, recipient numbers or access tokens.
        result = "uncertain"
    async with async_session_maker() as db:
        await db.execute(
            update(WhatsAppReply)
            .where(
                WhatsAppReply.reply_id == job_id,
                WhatsAppReply.status == "sending",
            )
            .values(status=result, next_attempt_at=retry_at)
        )
        await db.commit()
    if result in {"failed", "uncertain"}:
        logger.warning("WhatsApp reply {} requires inspection ({})", job_id, result)
    return True


async def run_whatsapp_worker():
    if not settings.whatsapp_enabled:
        return
    if not all(
        (
            settings.whatsapp_access_token,
            settings.whatsapp_phone_number_id,
            settings.whatsapp_graph_version,
        )
    ):
        logger.error("WhatsApp sender configuration is incomplete")
        return
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        while True:
            try:
                processed = await send_next(client)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("WhatsApp queue processing failed; pending work retained")
                processed = False
            await asyncio.sleep(0.1 if processed else 5)
