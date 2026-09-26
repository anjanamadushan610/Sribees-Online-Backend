"""
Shared plumbing for the security suite.

Everything here runs WITHOUT Postgres or a real Redis. The properties under test
-- "no token, no data", "a customer token never opens an admin route" -- must
hold before the application touches storage at all, so the database is replaced
by a stub that fails the request loudly if anything queries it. A route that
reads the database before it has checked who is asking shows up as a 500 with
"DB touched before auth" in the body, which is exactly the bug worth seeing.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterator

import fakeredis.aioredis
import httpx
import jwt
import pytest
from fastapi.routing import APIRoute

from app.config.settings import settings

# Dependencies that REQUIRE an identity. get_current_user_optional is
# deliberately absent: a route that only has it is public by design.
_REQUIRED_AUTH = (
    "get_current_user",
    "get_current_admin",
    "get_current_partner",
    "require_super_admin",
    "require_base_role_admin",
    "require_roles.<locals>.role_checker",
    "require_permission.<locals>.permission_checker",
    "inject_branch_filter",
)
_ADMIN_MARKERS = (
    "get_current_admin",
    "require_super_admin",
    "require_base_role_admin",
    "role_checker",
    "permission_checker",
    "inject_branch_filter",
)


def _dependency_names(dependant, acc: set[str]) -> set[str]:
    for sub in dependant.dependencies:
        acc.add(getattr(sub.call, "__qualname__", repr(sub.call)))
        _dependency_names(sub, acc)
    return acc


def route_realm(route: APIRoute) -> str:
    """'admin', 'partner', 'customer' or 'public' -- who a route admits."""
    names = _dependency_names(route.dependant, set())
    required = {n for n in names if n in _REQUIRED_AUTH or n.endswith(_REQUIRED_AUTH)}
    if not required:
        return "public"
    if any(m in n for n in required for m in _ADMIN_MARKERS):
        return "admin"
    if any("partner" in n for n in required):
        return "partner"
    return "customer"


def api_routes() -> list[APIRoute]:
    from app.main import app

    return [r for r in app.routes if isinstance(r, APIRoute)]


def route_cases(realm: str) -> list[tuple[str, str]]:
    """(METHOD, path) for every route of one realm, one case per method."""
    out = []
    for r in api_routes():
        if route_realm(r) != realm:
            continue
        for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
            out.append((method, r.path))
    return sorted(set(out))


_PARAM = re.compile(r"{([^}:]+)(:[^}]+)?}")


def concrete(path: str) -> str:
    """Fill every path parameter with a syntactically valid value."""
    return _PARAM.sub(lambda _m: str(uuid.UUID(int=1)), path)


class _NoDatabase:
    """Stands in for AsyncSession. Any real use is a test failure."""

    def __getattr__(self, name):
        def _refuse(*_a, **_k):
            raise AssertionError(f"DB touched before auth (session.{name})")

        return _refuse


def make_token(
    *,
    sub: str | None = None,
    token_type: str = "access",
    is_admin: bool = False,
    is_partner: bool = False,
    role: str | None = None,
    expires_in: timedelta = timedelta(minutes=15),
    key: str | None = None,
    algorithm: str | None = None,
    extra: dict | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict = {
        "sub": sub or str(uuid.uuid4()),
        "email": "probe@example.test",
        "session_id": str(uuid.uuid4()),
        "type": token_type,
        "iat": now,
        "exp": now + expires_in,
    }
    if is_admin:
        payload["is_admin"] = True
        payload["role"] = role or "super_admin"
    if is_partner:
        payload["is_partner"] = True
    payload.update(extra or {})
    return jwt.encode(
        payload,
        key if key is not None else settings.jwt_secret_key,
        algorithm=algorithm or settings.jwt_algorithm,
    )


@pytest.fixture
def storage_free_app() -> Iterator:
    """The real app with the database stubbed out and Redis in memory."""
    from app.config import redis as redis_module
    from app.config.database import get_db
    from app.config.redis import get_redis
    from app.main import app

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def _no_db():
        yield _NoDatabase()

    async def _fake_redis():
        yield fake

    previous_client = redis_module._redis_client
    redis_module._redis_client = fake
    app.dependency_overrides[get_db] = _no_db
    app.dependency_overrides[get_redis] = _fake_redis
    try:
        yield app
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_redis, None)
        redis_module._redis_client = previous_client


@pytest.fixture
async def client(storage_free_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=storage_free_app, raise_app_exceptions=False),
        base_url="http://testserver",
        timeout=30,
    ) as c:
        yield c
