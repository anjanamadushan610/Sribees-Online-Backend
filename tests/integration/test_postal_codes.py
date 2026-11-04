"""
Postal codes on the postal city directory (migration 103): the customer picker
shows them, a saved address takes its town's code, and the two misspelt
Kalutara towns hand-typed by migration 018 fold into the official spelling.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import uuid
from urllib.parse import unquote, urlparse

from sqlalchemy import select, text

from app.config.settings import settings

MIGRATION = pathlib.Path(__file__).resolve().parents[2] / "migrations" / "103_postal_codes.sql"


def _replay_103() -> None:
    from tests.integration.conftest import _psql

    url = urlparse(settings.async_database_url.replace("postgresql+asyncpg", "postgresql"))
    env = {
        **os.environ,
        "PGHOST": url.hostname or "localhost",
        "PGPORT": str(url.port or 5432),
        "PGUSER": unquote(url.username or ""),
        "PGPASSWORD": unquote(url.password or ""),
        "PGDATABASE": url.path.lstrip("/"),
    }
    done = subprocess.run([_psql(), "-q", "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION)],
                          env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]


async def test_every_seeded_town_has_its_code(db, migrated):
    from app.models.branch import PostalCityDirectory

    rows = {
        r.postal_city: r.postal_code
        for r in (await db.execute(select(PostalCityDirectory).where(
            PostalCityDirectory.district == "Kalutara"))).scalars()
    }
    assert rows["Matugama"] == "12100"
    assert rows["Meegahatenna"] == "12130"
    assert "Mathugama" not in rows and "Meegahathanna" not in rows, "018 no longer seeds old spellings"


async def test_old_spelling_folds_into_the_official_one(db, migrated, world, make_customer):
    from app.models.branch import PostalCityBranchMapping, PostalCityDirectory
    from app.models.user import Address

    # The production picture of 2026-11-04: the old spelling launched, mapped,
    # and named by a saved address.
    await db.execute(text(
        "INSERT INTO post_office_directory (id, post_office, district, province, is_active, is_launched) "
        "VALUES (gen_random_uuid(), 'Meegahathanna', 'Kalutara', 'Western', TRUE, TRUE)"))
    await db.execute(text("UPDATE post_office_directory SET is_launched = FALSE WHERE post_office = 'Meegahatenna'"))
    db.add(PostalCityBranchMapping(postal_city="Meegahathanna", branch_id=world.branch_id,
                                   branch_name="ZZ", district="Kalutara", province="Western", is_active=True))
    customer = await make_customer(with_address=False)
    address_id = uuid.uuid4()
    db.add(Address(address_id=address_id, user_id=customer.user_id, address_line1="1 Old Road",
                   postal_city="Meegahathanna", district="Kalutara", province="Western", postal_code=""))
    await db.commit()

    _replay_103()
    _replay_103()  # every deploy replays it

    db.expire_all()
    names = [r.postal_city for r in (await db.execute(select(PostalCityDirectory).where(
        PostalCityDirectory.postal_city.in_(["Meegahathanna", "Meegahatenna"])))).scalars()]
    assert names == ["Meegahatenna"]
    official = (await db.execute(select(PostalCityDirectory).where(
        PostalCityDirectory.postal_city == "Meegahatenna"))).scalar_one()
    assert official.is_launched is True, "switched on carries across"

    mapping = (await db.execute(select(PostalCityBranchMapping).where(
        PostalCityBranchMapping.postal_city.in_(["Meegahathanna", "Meegahatenna"])))).scalars().all()
    assert [m.postal_city for m in mapping] == ["Meegahatenna"]

    addr = (await db.execute(select(Address).where(Address.address_id == address_id))).scalar_one()
    assert addr.postal_city == "Meegahatenna"
    assert addr.postal_code == "12130", "a blank code is backfilled"


async def test_picker_and_saved_address_carry_the_code(api, db, world, make_customer):
    from app.models.branch import PostalCityDirectory

    await db.execute(
        PostalCityDirectory.__table__.update()
        .where(PostalCityDirectory.postal_city == world.postal_city)
        .values(postal_code="99999")
    )
    await db.commit()

    r = await api.get("/api/v1/locations/postal-cities", params={"district": world.district})
    assert r.status_code == 200, r.text
    rows = [p for p in r.json()["data"] if p["postalCity"] == world.postal_city]
    assert rows and rows[0]["postalCode"] == "99999"

    c = await make_customer(with_address=False)
    base = "/api/v1/user/addresses"
    body = {"address_line1": "42/A Flower Road", "postal_city": world.postal_city,
            "district": world.district, "province": world.province}
    r = await api.post(base, json=body, headers=c.headers)
    assert r.status_code == 201, r.text
    assert "99999" in r.text, "the app sends no code; the town's is used"

    r = await api.post(base, json={**body, "postal_code": "12345"}, headers=c.headers)
    assert r.status_code == 201, r.text
    assert "12345" in r.text, "a code the customer typed is kept"
