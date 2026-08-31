"""
AI Shopping Chat API — a Gemini-backed assistant that recommends real,
branch-visible products from a customer's message and/or photo.

  POST   /chat/message  — send a message, get a reply + matching products
  GET    /chat/history  — recent turns, so reopening the chat isn't blank
  DELETE /chat/history  — clear the conversation

Works for guests too (X-Device-Id), same convention as /products/search.

Prefix "/chat" is applied by app/api/v1/router.py — do not repeat it here.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis
from app.core.dependencies import get_current_user_optional
from app.schemas.chat import ChatMessageRequest
from app.services import branch_service
from app.services.chat_service import ChatService
from app.services.embedding_service import GeminiAPIError
from app.utils.i18n import resolve_language

router = APIRouter(tags=["AI Chat"])


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

    try:
        result = await ChatService.chat(
            db,
            redis,
            session_id=session_id,
            branch_id=branch_id,
            message=body.message,
            image_base64=body.image_base64,
            image_mime_type=body.image_mime_type,
            lang=resolve_language(request.headers.get("accept-language")),
        )
    except GeminiAPIError as exc:
        logger.error(f"Chat Gemini error: {exc}")
        detail = (
            "AI chat isn't configured yet — add GEMINI_API_KEY to the backend .env."
            if "GEMINI_API_KEY not configured" in str(exc)
            else "The assistant is unavailable right now. Please try again."
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)

    return {
        "success": True,
        "data": {
            "reply": result["reply"],
            "products": result["products"],
        },
    }


@router.get("/history", response_model=dict)
async def get_chat_history(
    request: Request,
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user_optional),
):
    """Recent turns, so reopening the chat screen isn't a blank slate."""
    session_id = _resolve_session_id(request, current_user)
    history = await ChatService.get_history(redis, session_id)
    return {"success": True, "data": {"history": history}}


@router.delete("/history", response_model=dict)
async def clear_chat_history(
    request: Request,
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user_optional),
):
    """Clear the conversation."""
    session_id = _resolve_session_id(request, current_user)
    await ChatService.clear_history(redis, session_id)
    return {"success": True, "message": "Chat history cleared"}
