"""
The postal city sync runs by itself (app/services/courier_coverage_worker.py).

A server whose directory was never synced prices no address at all, so the
background sync must: skip quietly when no courier key is set, survive a
courier failure, and leave the round to another process holding the lock.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import courier_coverage_worker as worker
from app.services.courier_credentials import CourierNotConfiguredError
from app.services.courier_service import CourierUnavailableError


class _FakeConn:
    def __init__(self, dialect: str, lock_granted: bool = True):
        self.dialect = SimpleNamespace(name=dialect)
        self.lock_granted = lock_granted
        self.statements: list[str] = []

    async def scalar(self, stmt, params=None):
        self.statements.append(str(stmt))
        return self.lock_granted

    async def execute(self, stmt, params=None):
        self.statements.append(str(stmt))

    async def commit(self):
        pass


class _FakeEngine:
    def __init__(self, conn: _FakeConn):
        self.conn = conn

    @asynccontextmanager
    async def connect(self):
        yield self.conn


@asynccontextmanager
async def _fake_session():
    yield object()


@pytest.fixture
def patched(monkeypatch):
    def install(conn: _FakeConn, sync):
        monkeypatch.setattr(worker, "engine", _FakeEngine(conn))
        monkeypatch.setattr(worker, "async_session_maker", _fake_session)
        monkeypatch.setattr(worker.courier_coverage_service, "sync", sync)
        return conn

    return install


@pytest.mark.asyncio
async def test_runs_the_sync_and_releases_the_lock(patched):
    calls = []

    async def sync(db):
        calls.append(db)

    conn = patched(_FakeConn("postgresql"), sync)
    assert await worker.sync_once("test") is True
    assert len(calls) == 1
    assert any("pg_try_advisory_lock" in s for s in conn.statements)
    assert any("pg_advisory_unlock" in s for s in conn.statements)


@pytest.mark.asyncio
async def test_another_process_holding_the_lock_skips_the_round(patched):
    async def sync(db):
        raise AssertionError("must not sync without the lock")

    conn = patched(_FakeConn("postgresql", lock_granted=False), sync)
    assert await worker.sync_once("test") is False
    assert not any("pg_advisory_unlock" in s for s in conn.statements)


@pytest.mark.asyncio
async def test_no_courier_key_yet_is_skipped_quietly(patched):
    async def sync(db):
        raise CourierNotConfiguredError("no key")

    conn = patched(_FakeConn("postgresql"), sync)
    assert await worker.sync_once("test") is False
    assert any("pg_advisory_unlock" in s for s in conn.statements)


@pytest.mark.asyncio
async def test_courier_outage_does_not_raise(patched):
    async def sync(db):
        raise CourierUnavailableError("down")

    patched(_FakeConn("postgresql"), sync)
    assert await worker.sync_once("test") is False


@pytest.mark.asyncio
async def test_non_postgres_databases_sync_without_a_lock(patched):
    calls = []

    async def sync(db):
        calls.append(db)

    conn = patched(_FakeConn("sqlite"), sync)
    assert await worker.sync_once("test") is True
    assert calls and conn.statements == []


@pytest.mark.asyncio
async def test_saving_settings_starts_no_sync_under_tests(monkeypatch):
    started = []
    monkeypatch.setattr(worker.settings, "app_env", "testing")
    monkeypatch.setattr(worker, "sync_once", lambda reason: started.append(reason))
    worker.sync_soon("courier settings changed")
    assert started == []
