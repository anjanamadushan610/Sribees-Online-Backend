"""
AI Shopping Chat API — a Gemini-backed assistant that recommends real,
branch-visible products from a customer's message and/or photo.

  POST   /chat/message  — send a message, get a reply + matching products
  POST   /chat/message/stream — the same turn, reporting progress over SSE
  GET    /chat/history  — recent turns, so reopening the chat isn't blank
  DELETE /chat/history  — clear the conversation

Works for guests too (X-Device-Id), same convention as /products/search.

Prefix "/chat" is applied by app/api/v1/router.py — do not repeat it here.
"""

import asyncio
import json
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db, get_db_context
from app.config.redis import RedisKeys, get_redis
from app.core.dependencies import get_current_user, get_current_user_optional
from app.core.rate_limiter import rate_limit
from app.schemas.assistant_commerce import ConfirmAssistantRequest
from app.schemas.chat import ChatMessageRequest
from app.services import branch_service, chat_language
from app.services.chat_service import ChatService
from app.services.embedding_service import GeminiAPIError
from app.utils.i18n import resolve_language

router = APIRouter(tags=["AI Chat"])


@router.post("/confirm", response_model=dict)
@rate_limit(requests_per_minute=12)
async def confirm_assistant_action(
    body: ConfirmAssistantRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user),
):
    from app.models.assistant import AssistantReceipt
    from app.services.assistant_personal import confirm_personal
    from app.services.assistant_shopping import confirm_cart, proposal_key
    from app.services.assistant_tools import tr

    user_id = current_user.user_id
    # A confirmation answers in the language the conversation is in, which
    # is the customer's, not necessarily the app's.
    lang = chat_language.last_language(
        await ChatService.get_history(redis, str(user_id))
    ) or resolve_language(request.headers.get("accept-language"))
    key = proposal_key(user_id, body.token)
    raw = await redis.get(key)
    if not raw:
        receipt = await db.get(AssistantReceipt, UUID(body.token))
        if receipt and receipt.user_id == user_id:
            return {"success": True, "data": receipt.response}
        raise HTTPException(409, "PREVIEW_EXPIRED_REQUEST_AGAIN")
    data = json.loads(raw)
    if data.get("done"):
        return {"success": True, "data": data["done"]}
    lock_key = RedisKeys.chat_history(str(user_id)) + ":busy"
    token = str(uuid4())
    if not await redis.set(lock_key, token, nx=True, ex=90):
        raise HTTPException(409, "A chat request is already in progress")
    try:
        if data["kind"] == "cart":
            branch = await _resolve_branch_id(redis, str(user_id))
            result = await confirm_cart(
                db, redis, user_id, branch, key, raw, data["payload"], lang
            )
        else:
            result = await confirm_personal(
                db, user_id, body.token, data["kind"], data["payload"], lang
            )
            await redis.set(key, json.dumps({"done": result}), ex=86400)
        history = await ChatService.get_history(redis, str(user_id))
        await ChatService._append_turn(
            redis,
            str(user_id),
            history,
            tr(
                lang,
                "Confirm my selection",
                "මගේ තේරීම තහවුරු කරන්න",
                "என் தேர்வை உறுதிசெய்யவும்",
            ),
            result["reply"],
            response=result,
            language=lang,
        )
        return {"success": True, "data": result}
    finally:
        await redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
            1,
            lock_key,
            token,
        )


def _resolve_session_id(request: Request, current_user) -> str:
    """
    Same session-identity convention branch context uses: a logged-in
    shopper is keyed by user_id, a guest by their X-Device-Id header.
    """
    if current_user is not None:
        return str(current_user.user_id)

    device_id = request.headers.get("X-Device-Id") or request.headers.get("x-device-id")
    if not device_id or not device_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "error": {
                    "message": "X-Device-Id header required for guest requests",
                    "code": "DEVICE_ID_REQUIRED",
                },
            },
        )
    return f"guest:{device_id.strip()}"


async def _resolve_branch_id(redis: Redis, session_id: str) -> Optional[UUID]:
    """
    Branch narrows product matches to what's actually nearby/in-stock — but
    unlike browsing the catalog, a chat reply is still useful without one,
    so this returns None instead of failing the request.
    """
    context = await branch_service.get_branch_context(redis, session_id)
    if context is None:
        return None
    try:
        return UUID(context["branch_id"])
    except (KeyError, ValueError, TypeError):
        return None


@router.post("/message", response_model=dict)
@rate_limit(requests_per_minute=12)
async def send_chat_message(
    body: ChatMessageRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user_optional),
):
    """Send one message (+ optional photo) to the shopping assistant."""
    if body.image_base64 and not body.image_mime_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="image_mime_type is required when image_base64 is set",
        )

    session_id = _resolve_session_id(request, current_user)
    branch_id = await _resolve_branch_id(redis, session_id)
    lock_key = RedisKeys.chat_history(session_id) + ":busy"
    lock_token = str(uuid4())
    if not await redis.set(lock_key, lock_token, nx=True, ex=90):
        raise HTTPException(
            status_code=409, detail="A chat request is already in progress"
        )
    try:
        result = await asyncio.wait_for(
            ChatService.chat(
                db,
                redis,
                session_id=session_id,
                branch_id=branch_id,
                message=body.message,
                image_base64=body.image_base64,
                image_mime_type=body.image_mime_type,
                lang=resolve_language(request.headers.get("accept-language")),
                user_id=current_user.user_id if current_user else None,
                task=body.task,
                target=body.target,
                order_reference=body.order_reference,
            ),
            timeout=55,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The assistant took too long. Please try again.",
        )
    except GeminiAPIError as exc:
        logger.error(f"Chat Gemini error: {exc}")
        detail = (
            "AI chat isn't configured yet — add GEMINI_API_KEY to the backend .env."
            if "GEMINI_API_KEY not configured" in str(exc)
            else "The assistant is unavailable right now. Please try again."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
        )
    finally:
        await redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
            1,
            lock_key,
            lock_token,
        )

    return {
        "success": True,
        "data": result,
    }


@router.post("/message/stream")
@rate_limit(requests_per_minute=12)
async def stream_chat_message(
    body: ChatMessageRequest,
    request: Request,
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user_optional),
):
    """
    The same turn as POST /chat/message, reported as it happens.

    This streams PROGRESS, not tokens, and that is deliberate. The model is
    asked for a structured plan (`responseJsonSchema`), not prose, and
    `_ground_reply` overrules its wording whenever real stock disagrees — so
    streaming the model's own words would show the customer sentences the
    server is about to discard. Generating prose separately, purely to have
    something to stream, would mean a second Gemini call on every message.
    This endpoint therefore costs exactly the same as the plain POST: one
    Gemini call, the same tokens. What it buys is that the 5-50 seconds stop
    being a silent wait.

    Frames (text/event-stream):
      event: stage  data: {"stage": "understanding" | "searching" | "orders"
                           | "preparing", "terms": [...]}
      event: done   data: <the same payload POST /chat/message returns>
      event: error  data: {"code": "...", "message": "..."}
    A `: keep-alive` comment goes out every 10s so proxies hold the connection.
    """
    session_id = _resolve_session_id(request, current_user)
    branch_id = await _resolve_branch_id(redis, session_id)
    lang = resolve_language(request.headers.get("accept-language"))
    user_id = current_user.user_id if current_user else None

    lock_key = RedisKeys.chat_history(session_id) + ":busy"
    lock_token = str(uuid4())
    if not await redis.set(lock_key, lock_token, nx=True, ex=90):
        raise HTTPException(
            status_code=409, detail="A chat request is already in progress"
        )

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def on_stage(stage: str, detail: dict) -> None:
            await queue.put(("stage", {"stage": stage, **detail}))

        async def run() -> None:
            try:
                # FastAPI >= 0.106 tears down `yield` dependencies before the
                # streaming body runs, so a session from Depends(get_db) would
                # already be closed here. This opens its own.
                async with get_db_context() as db:
                    result = await asyncio.wait_for(
                        ChatService.chat(
                            db,
                            redis,
                            session_id=session_id,
                            branch_id=branch_id,
                            message=body.message,
                            image_base64=body.image_base64,
                            image_mime_type=body.image_mime_type,
                            lang=lang,
                            user_id=user_id,
                            task=body.task,
                            target=body.target,
                            order_reference=body.order_reference,
                            on_stage=on_stage,
                        ),
                        timeout=55,
                    )
                await queue.put(("done", result))
            except asyncio.TimeoutError:
                await queue.put(
                    (
                        "error",
                        {
                            "code": "TIMEOUT",
                            "message": "The assistant took too long. Please try again.",
                        },
                    )
                )
            except GeminiAPIError as exc:
                logger.error(f"Chat Gemini error: {exc}")
                await queue.put(
                    (
                        "error",
                        {
                            "code": "AI_UNAVAILABLE",
                            "message": (
                                "AI chat isn't configured yet — add GEMINI_API_KEY "
                                "to the backend .env."
                                if "GEMINI_API_KEY not configured" in str(exc)
                                else "The assistant is unavailable right now. "
                                "Please try again."
                            ),
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001 - reported to the client
                logger.exception(f"Chat stream failed: {exc}")
                await queue.put(
                    (
                        "error",
                        {
                            "code": "INTERNAL",
                            "message": "Something went wrong. Please try again.",
                        },
                    )
                )
            finally:
                await queue.put((None, None))

        worker = asyncio.create_task(run())
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if kind is None:
                    break
                yield (
                    f"event: {kind}\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
        finally:
            # A customer who backs out of the screen mid-turn stops the work
            # rather than leaving it to write a turn they will never see.
            worker.cancel()
            await redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                1,
                lock_key,
                lock_token,
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx buffers proxied responses by default, which would hold
            # every stage frame back until the turn finished.
            "X-Accel-Buffering": "no",
        },
    )


async def _already_confirmed(
    db: AsyncSession, redis: Redis, user_id, tokens: set
) -> set:
    """
    Which of these proposal tokens the customer has already confirmed.

    Whether a preview was acted on is the server's fact, not the app's: the
    app used to track it in memory only, so reopening the chat offered every
    past "Confirm basket changes" button again as if nothing had happened.
    Redis holds the answer for a day; the receipt table is the durable record
    once that has expired.
    """
    if not user_id or not tokens:
        return set()

    from sqlalchemy import select

    from app.models.assistant import AssistantReceipt
    from app.services.assistant_shopping import proposal_key

    ordered = sorted(tokens)
    confirmed: set = set()
    unresolved: list = []
    for token, raw in zip(
        ordered,
        await redis.mget([proposal_key(user_id, t) for t in ordered]),
        # MGET answers with exactly one entry per key (None for a miss), so the
        # two sides cannot fall out of step -- strict=True states that rather
        # than silently truncating if it ever stopped being true.
        strict=True,
    ):
        try:
            if raw and json.loads(raw).get("done"):
                confirmed.add(token)
                continue
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        unresolved.append(token)

    receipt_ids = []
    for token in unresolved:
        try:
            receipt_ids.append(UUID(token))
        except (ValueError, AttributeError):
            continue
    if receipt_ids:
        rows = await db.execute(
            select(AssistantReceipt.token).where(
                AssistantReceipt.token.in_(receipt_ids),
                AssistantReceipt.user_id == user_id,
            )
        )
        confirmed.update(row[0].hex for row in rows)
    return confirmed


@router.get("/history", response_model=dict)
async def get_chat_history(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user_optional),
):
    """Recent turns, so reopening the chat screen isn't a blank slate."""
    session_id = _resolve_session_id(request, current_user)
    history = await ChatService.get_history(redis, session_id)
    # Search context stays server-side; restored order cards are snapshots.
    public_history = [
        {key: value for key, value in turn.items() if key != "search_context"}
        for turn in history
    ]
    commands = [
        command
        for turn in public_history
        for panel in turn.get("panels", [])
        for command in panel.get("commands", [])
        if isinstance(command.get("token"), str)
    ]
    confirmed = await _already_confirmed(
        db,
        redis,
        current_user.user_id if current_user else None,
        {command["token"] for command in commands},
    )
    for command in commands:
        command["confirmed"] = command["token"] in confirmed
    for turn in public_history:
        for order in turn.get("orders", []):
            order["freshness"] = "saved"
    return {"success": True, "data": {"history": public_history}}


@router.delete("/history", response_model=dict)
async def clear_chat_history(
    request: Request,
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user_optional),
):
    """Clear the conversation."""
    session_id = _resolve_session_id(request, current_user)
    lock_key = RedisKeys.chat_history(session_id) + ":busy"
    lock_token = str(uuid4())
    if not await redis.set(lock_key, lock_token, nx=True, ex=90):
        raise HTTPException(
            status_code=409, detail="A chat request is already in progress"
        )
    try:
        await ChatService.clear_history(redis, session_id)
    finally:
        await redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
            1,
            lock_key,
            lock_token,
        )
    return {"success": True, "message": "Chat history cleared"}
