"""
The streaming chat endpoint's wire format.

Everything about SSE that can break is in the bytes: the event names, the
blank line that ends a frame, the JSON on the data line, and the fact that a
failure arrives as an `error` frame rather than an HTTP status the client has
already stopped listening for. These assert the actual response body the app
will parse, so a well-meant refactor of the generator cannot quietly change
the contract.

What this cannot cover is a proxy that buffers the response -- that only
shows up against the real deployment.
"""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import chat as chat_api
from app.config.database import get_db
from app.config.redis import get_redis
from app.core.dependencies import get_current_user_optional
from app.services.chat_service import ChatService


def _client(monkeypatch, *, chat_impl) -> TestClient:
    redis = AsyncMock()
    redis.set.return_value = True  # the busy lock is free
    redis.get.return_value = None
    redis.eval.return_value = 1

    @asynccontextmanager
    async def fake_db_context():
        yield AsyncMock()

    monkeypatch.setattr(chat_api, "get_db_context", fake_db_context)
    monkeypatch.setattr(ChatService, "chat", chat_impl)

    app = FastAPI()
    app.include_router(chat_api.router, prefix="/chat")
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_current_user_optional] = lambda: None
    client = TestClient(app)
    client.headers.update({"X-Device-Id": "test-device"})
    return client


def _frames(body: str):
    """The SSE body, parsed back into (event, data) pairs."""
    out = []
    for block in body.split("\n\n"):
        event, data = None, []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if event:
            out.append((event, json.loads("\n".join(data))))
    return out


@pytest.mark.asyncio
async def test_a_streamed_turn_reports_its_stages_then_the_finished_reply(
    monkeypatch,
):
    async def fake_chat(*args, on_stage=None, **kwargs):
        await on_stage("understanding", {})
        await on_stage("searching", {"terms": ["කිරි"]})
        return {
            "reply": "මෙන්න භාණ්ඩ",
            "products": [],
            "orders": [],
            "actions": [],
            "guide": None,
            "degraded": False,
            "panels": [],
        }

    client = _client(monkeypatch, chat_impl=fake_chat)
    response = client.post("/chat/message/stream", json={"message": "කිරි"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # nginx buffers proxied responses by default, which would hold every
    # stage frame back until the turn had already finished.
    assert response.headers["x-accel-buffering"] == "no"

    frames = _frames(response.text)
    assert [event for event, _ in frames] == ["stage", "stage", "done"]
    assert frames[0][1]["stage"] == "understanding"
    assert frames[1][1] == {"stage": "searching", "terms": ["කිරි"]}
    # The final frame is the same payload the plain POST returns.
    assert frames[2][1]["reply"] == "මෙන්න භාණ්ඩ"
    # Sinhala must survive the frame encoding, not arrive as \uXXXX escapes.
    assert "මෙන්න භාණ්ඩ" in response.text


@pytest.mark.asyncio
async def test_a_failure_mid_turn_arrives_as_an_error_frame(monkeypatch):
    """
    The status line is long gone by the time the model fails, so a failure
    has to be a frame. The app turns this into its "assistant is busy"
    message rather than an empty bubble.
    """
    from app.services.embedding_service import GeminiAPIError

    async def fake_chat(*args, on_stage=None, **kwargs):
        await on_stage("understanding", {})
        raise GeminiAPIError("upstream is down")

    client = _client(monkeypatch, chat_impl=fake_chat)
    response = client.post("/chat/message/stream", json={"message": "hello"})

    assert response.status_code == 200
    frames = _frames(response.text)
    assert [event for event, _ in frames] == ["stage", "error"]
    assert frames[1][1]["code"] == "AI_UNAVAILABLE"
    assert frames[1][1]["message"]


@pytest.mark.asyncio
async def test_a_second_turn_while_one_is_running_is_refused_outright(
    monkeypatch,
):
    """A busy lock rejection is a real HTTP status: nothing has streamed yet."""

    async def fake_chat(*args, on_stage=None, **kwargs):  # pragma: no cover
        raise AssertionError("must not run while another turn holds the lock")

    client = _client(monkeypatch, chat_impl=fake_chat)
    # Re-point the override at a redis whose lock is already taken.
    busy = AsyncMock()
    busy.set.return_value = False
    busy.get.return_value = None
    client.app.dependency_overrides[get_redis] = lambda: busy

    response = client.post("/chat/message/stream", json={"message": "hello"})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_a_guest_without_a_device_id_cannot_open_a_stream(monkeypatch):
    async def fake_chat(*args, on_stage=None, **kwargs):  # pragma: no cover
        raise AssertionError("must not run for an unidentified caller")

    client = _client(monkeypatch, chat_impl=fake_chat)
    client.headers.pop("X-Device-Id")
    response = client.post("/chat/message/stream", json={"message": "hello"})
    assert response.status_code == 400
