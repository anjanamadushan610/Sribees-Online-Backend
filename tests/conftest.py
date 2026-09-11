"""
Shared fixtures for the integration tests.

The rest of the suite is unit-level and needs nothing. The tests added
alongside this file exercise endpoints that genuinely cannot be tested without
a database and a Redis: branch context lives in Redis, and the banner /
delivery / coupon endpoints all resolve the customer's branch from it. Mocking
that away would test the mock — the bugs worth catching here are precisely in
whether the real branch resolution reaches the real query.

CI provides both as service containers (see .github/workflows/deploy.yml).
Locally, `docker run -d -p 6379:6379 redis:7-alpine` plus the dev Postgres is
enough. When either is missing the dependent tests SKIP rather than fail, so a
developer without the services still gets a green unit run — but CI, which has
them, actually runs the assertions.
"""
import os
import socket
from contextlib import closing

import pytest


def _reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _hostport(url: str, default_port: int) -> tuple[str, int]:
    """Pull host/port out of a URL without dragging in a parser."""
    body = url.split("://", 1)[-1]
    if "@" in body:
        body = body.rsplit("@", 1)[1]
    body = body.split("/", 1)[0]
    if ":" in body:
        host, _, port = body.partition(":")
        return host, int(port)
    return body, default_port


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_HOST, REDIS_PORT = _hostport(REDIS_URL, 6379)

# Read the same setting the app does, so a developer's .env is honoured.
try:
    from app.config.settings import settings

    DB_URL = settings.async_database_url
except Exception:  # pragma: no cover - settings import is not the thing under test
    DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5432/postgres")

DB_HOST, DB_PORT = _hostport(DB_URL, 5432)

requires_redis = pytest.mark.skipif(
    not _reachable(REDIS_HOST, REDIS_PORT),
    reason=f"Redis not reachable at {REDIS_HOST}:{REDIS_PORT}",
)

requires_db = pytest.mark.skipif(
    not _reachable(DB_HOST, DB_PORT),
    reason=f"Postgres not reachable at {DB_HOST}:{DB_PORT}",
)


@pytest.fixture
async def app_redis():
    """
    Initialise the app's own Redis client for the duration of a test.

    httpx.ASGITransport does not run the application lifespan, so without this
    `get_redis()` raises "Redis not initialized" the moment a request touches
    branch context. Calling init_redis() directly is narrower than pulling in
    asgi-lifespan, which would also run DB pool warm-up and the scheduler that
    these tests neither need nor want.
    """
    from app.config.redis import close_redis, init_redis

    client = await init_redis()
    if client is None:
        pytest.skip("Redis did not initialise")
    try:
        yield client
    finally:
        await close_redis()


@pytest.fixture
async def app_engine_reset():
    """
    Dispose the application's connection pool after a test that drove HTTP
    requests through it.

    `app.config.database.engine` is module-level and pooled. pytest-asyncio
    runs each test on its own event loop, so without this the next test reuses
    connections bound to a loop that has since closed, and asyncpg fails in
    teardown with "Event loop is closed". Cheap: one reconnect per test.
    """
    yield
    from app.config.database import engine

    await engine.dispose()
