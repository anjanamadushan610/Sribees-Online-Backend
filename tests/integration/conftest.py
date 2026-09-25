"""
Integration fixtures: the real app against a real Postgres and a real Redis.

Nothing inside the service boundary is stubbed. The single fake is the
SribeesExpress courier -- an external company's API -- and even that is faked
at the HTTP layer (tests/integration/fake_courier.py), so the courier client's
request building, headers and response parsing all still run.

Each test builds its own "world": a province, district and postal area that are
switched on, a branch that serves them, a product stocked there, and customers
and admins with real sessions. Every name carries a random suffix, so tests
never collide with each other or with seed data.

These tests WRITE. They refuse to run unless the database name says it is a
test database, so pointing a developer's .env at them cannot fill a real
database with fixtures.
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import settings
from tests.conftest import requires_db, requires_redis

pytestmark = [requires_db, requires_redis]


def _database_name() -> str:
    return settings.async_database_url.rsplit("/", 1)[-1].split("?", 1)[0]


requires_disposable_db = pytest.mark.skipif(
    "test" not in _database_name().lower(),
    reason=f"integration tests write fixtures; database {_database_name()!r} is not a test database",
)


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/integration" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.integration)
            item.add_marker(requires_disposable_db)
            item.add_marker(requires_db)
            item.add_marker(requires_redis)


@pytest.fixture
async def db_session_factory(db_schema):
    engine = create_async_engine(settings.async_database_url, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_session_factory):
    async with db_session_factory() as session:
        yield session


@pytest.fixture
async def api(app_redis, app_engine_reset):
    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
        timeout=60,
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


@dataclass
class Customer:
    user_id: uuid.UUID
    phone: str
    token: str
    address_id: Optional[uuid.UUID] = None

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


@dataclass
class AdminLogin:
    admin_id: uuid.UUID
    email: str
    password: str
    role: str
    token: str

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


@dataclass
class World:
    suffix: str
    branch_id: uuid.UUID
    province: str
    district: str
    postal_city: str
    courier_postal_city_id: int
    products: dict = field(default_factory=dict)


def _phone() -> str:
    # 07x-xxxxxxx in canonical +94 form; the 9 digits after +94 are random.
    return "+947" + f"{uuid.uuid4().int % 10**8:08d}"


@pytest.fixture
async def world(db):
    from app.models.branch import Branch, PostalCityBranchMapping, PostalCityDirectory
    from app.models.location import LocationDistrict, LocationProvince

    s = uuid.uuid4().hex[:8].upper()
    w = World(
        suffix=s,
        branch_id=uuid.uuid4(),
        province=f"ZZ Province {s}",
        district=f"ZZ District {s}",
        postal_city=f"ZZ Town {s}",
        courier_postal_city_id=900000 + uuid.uuid4().int % 99999,
    )
    db.add(LocationProvince(name=w.province, is_unlocked=True))
    await db.flush()
    db.add(LocationDistrict(name=w.district, province=w.province, is_unlocked=True))
    db.add(
        Branch(
            branch_id=w.branch_id,
            name=f"ZZ Branch {s}",
            code=f"ZZB{s}",
            province=w.province,
            district=w.district,
            is_active=True,
            delivery_runs=[],
        )
    )
    await db.flush()
    db.add(
        PostalCityDirectory(
            postal_city=w.postal_city,
            district=w.district,
            province=w.province,
            is_active=True,
            is_launched=True,
            courier_postal_city_id=w.courier_postal_city_id,
        )
    )
    db.add(
        PostalCityBranchMapping(
            postal_city=w.postal_city,
            branch_id=w.branch_id,
            branch_name=f"ZZ Branch {s}",
            district=w.district,
            province=w.province,
            is_active=True,
        )
    )
    await db.commit()
    return w


@pytest.fixture
def make_product(db, world):
    from app.models.product import BranchInventory, Product

    async def _make(
        *,
        price: str = "250.00",
        stock: int = 20,
        discount: Optional[float] = None,
        branch_price: Optional[str] = None,
        active: bool = True,
    ) -> uuid.UUID:
        pid = uuid.uuid4()
        tag = uuid.uuid4().hex[:6]
        db.add(
            Product(
                product_id=pid,
                name=f"ZZ Test Rice {tag}",
                slug=f"zz-test-rice-{tag}",
                sku=f"ZZ-{tag}",
                price=Decimal(price),
                discount_percentage=discount,
                stock_quantity=stock,
                weight=Decimal("1.000"),
                weight_unit="kg",
                is_active=active,
            )
        )
        await db.flush()
        db.add(
            BranchInventory(
                product_id=pid,
                branch_id=world.branch_id,
                stock_quantity=stock,
                branch_price=Decimal(branch_price) if branch_price else None,
                is_active=True,
            )
        )
        await db.commit()
        world.products[pid] = price
        return pid

    return _make


@pytest.fixture
def make_customer(db, world):
    """A phone-verified customer with a live session and a served address."""
    from app.core.security import create_token_pair
    from app.models.user import Address, Session, User

    async def _make(*, with_address: bool = True) -> Customer:
        user_id = uuid.uuid4()
        phone = _phone()
        db.add(
            User(
                user_id=user_id,
                full_name=f"ZZ Customer {world.suffix}",
                phone=phone,
                is_verified=False,
                is_phone_verified=True,
                is_active=True,
                is_blocked=False,
            )
        )
        await db.flush()
        session_id = uuid.uuid4()
        db.add(
            Session(
                session_id=session_id,
                user_id=user_id,
                refresh_token_hash="integration",
                expires_at=datetime.utcnow() + timedelta(days=1),
            )
        )
        address_id = None
        if with_address:
            address_id = uuid.uuid4()
            db.add(
                Address(
                    address_id=address_id,
                    user_id=user_id,
                    recipient_name="ZZ Customer",
                    phone=phone,
                    address_line1="12 Test Lane",
                    postal_city=world.postal_city,
                    district=world.district,
                    province=world.province,
                    postal_code="00000",
                    is_default=True,
                    courier_postal_city_id=world.courier_postal_city_id,
                )
            )
        await db.commit()
        token = create_token_pair(user_id, "", session_id)["access_token"]
        return Customer(user_id=user_id, phone=phone, token=token, address_id=address_id)

    return _make


@pytest.fixture
def make_admin(db, world, rbac):
    """An admin with a real password, logged in through the real endpoint."""
    from app.core.security import hash_password
    from app.models.admin import Admin, AdminRole

    async def _make(
        role: str = "super_admin",
        *,
        branch_id: Optional[uuid.UUID] = None,
        api=None,
        real_role: bool = True,
    ) -> AdminLogin:
        """Linked to its system role row, exactly as production admins are
        (migration 037 makes admins.role_id NOT NULL)."""
        from sqlalchemy import select as _select

        from app.models.role import Role

        role_id = (await db.execute(_select(Role.role_id).where(Role.system_key == role))).scalar_one()
        admin_id = uuid.uuid4()
        email = f"zz.{role}.{uuid.uuid4().hex[:8]}@sribees-ci.lk"
        password = secrets.token_urlsafe(16)  # per test; nothing fixed to leak or reuse
        scoped = role != "super_admin"
        db.add(
            Admin(
                admin_id=admin_id,
                email=email,
                password_hash=hash_password(password),
                full_name=f"ZZ {role}",
                role=AdminRole(role),
                branch_id=branch_id or (world.branch_id if scoped else None),
                role_id=role_id,
                is_active=True,
            )
        )
        await db.commit()
        assert api is not None, "pass api= so the admin logs in through /admin/auth/login"
        r = await api.post(
            "/api/v1/admin/auth/login",
            json={"email": email, "password": password},
            headers={"X-Forwarded-For": f"198.51.100.{uuid.uuid4().int % 250}"},
        )
        assert r.status_code == 200, r.text
        token = r.json()["data"]["token"]
        return AdminLogin(admin_id=admin_id, email=email, password=password, role=role, token=token)

    return _make


@pytest.fixture
def fake_courier(monkeypatch):
    """Route every SribeesExpress call to the in-process fake. Returns its state."""
    from tests.integration.fake_courier import install

    return install(monkeypatch)


@pytest.fixture
def place_order(api, app_redis, world, fake_courier):
    """Place a real COD order through the API; returns the order as the API shows it."""
    import json

    from app.config.redis import RedisKeys

    async def _place(customer: Customer, product_id, qty: int = 1, **extra) -> dict:
        await app_redis.setex(
            RedisKeys.branch_context(str(customer.user_id)),
            600,
            json.dumps({"branch_id": str(world.branch_id), "branch_name": "ZZ"}),
        )
        r = await api.post(
            "/api/v1/cart/items",
            json={"product_id": str(product_id), "quantity": qty, "price": "1.00", "name": "ZZ item"},
            headers=customer.headers,
        )
        assert r.status_code == 200, r.text
        body = {
            "delivery_address_id": str(customer.address_id),
            "contact_phone": customer.phone,
            "alternate_phone": "+94719999999",
            "payment_method": "COD",
            **extra,
        }
        r = await api.post("/api/v1/orders", json=body, headers=customer.headers)
        assert r.status_code == 201, r.text
        return r.json()["data"]

    return _place


_RBAC_SEEDED = False


@pytest.fixture
async def rbac(db_schema):
    """
    The production permission catalogue: system roles, permissions and their
    grants, from the migrations themselves -- every one that seeds
    permissions, in filename order, as the deploy applies them (all are
    idempotent). Applied once per run.

    Not just 037: later features add their own grants (057 gives support its
    support:* permissions, for instance), and a catalogue missing them would
    test a permission model production does not have.
    """
    global _RBAC_SEEDED
    if not _RBAC_SEEDED:
        import pathlib
        import re

        import asyncpg

        grants = re.compile(r"INSERT INTO (role_permissions|permissions)")
        files = [
            # Anchored to this file, not the working directory, which the
            # app's media setup may have moved.
            f for f in sorted((pathlib.Path(__file__).resolve().parents[2] / "migrations").glob("*.sql"))
            if grants.search(f.read_text(encoding="utf-8"))
        ]
        assert files, "no permission-seeding migrations found"
        conn = await asyncpg.connect(settings.async_database_url.replace("postgresql+asyncpg", "postgresql"))
        try:
            for f in files:
                await conn.execute(f.read_text(encoding="utf-8"))
        finally:
            await conn.close()
        _RBAC_SEEDED = True
