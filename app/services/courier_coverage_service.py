"""
Courier Coverage Sync — fill ``post_office_directory`` from SribeesExpress's
postal city list.

``GET /ecommerce/postal-cities`` (contract section 5) returns every postal city
SribeesExpress delivers to today, each with its id. That id is what every
delivery quote is keyed on, so this sync is what makes a customer's address
quotable at all: an address matches a directory row by name and district
(``courier_location_service``), and the row carries the id.

Three deliberate decisions:

**Their spelling wins.** A row is matched on case-folded name *and* district,
then adopts their exact casing. Name alone is not enough — four real postal
cities share a name with one in another district — and hand-correcting
spellings is how ``Mathugama`` and ``Matugama`` came to disagree in the first
place.

**Only districts they report are reconciled.** A row is switched off only when
its own district appears in the response and its name does not; inside a
district they report on, absence is real. Outside one we have no signal and
leave the row alone.

**A name the directory cannot hold is reported, not forced.** The directory's
postal city name is unique (branch mappings and addresses reference it by
name), so a second same-named town in another district cannot be added. It is
returned in ``skipped`` for an operator to see, rather than silently
overwriting the first town's district and id.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import PostalCityDirectory
from app.models.user import Address
from app.services import courier_credentials
from app.services.courier_service import CourierService
from app.services.location_rollout_service import canonical_province


@dataclass
class CoverageSyncResult:
    """What one sync run changed, for the admin UI and the logs."""

    fetched: int = 0
    created: int = 0
    updated: int = 0
    reactivated: int = 0
    deactivated: int = 0
    addresses_matched: int = 0
    districts_covered: List[str] = field(default_factory=list)
    # Rows this run switched off that saved addresses still name.
    # ``{"postal_city": ..., "district": ..., "addresses": n}``
    orphaned: List[dict] = field(default_factory=list)
    # Postal cities this run could not add because the name is already taken
    # by a row in another district. ``{"postal_city", "district", "held_by_district"}``
    skipped: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "reactivated": self.reactivated,
            "deactivated": self.deactivated,
            "addresses_matched": self.addresses_matched,
            "districts_covered": self.districts_covered,
            "orphaned": self.orphaned,
            "skipped": self.skipped,
        }


def _norm(value: Optional[str]) -> str:
    """Their matching key: trimmed and case-folded, never fuzzy."""
    return (value or "").strip().casefold()


async def _count_addresses_by_postal_city(db: AsyncSession, names: List[str]) -> Dict[str, int]:
    """How many saved addresses still name each of ``names`` (case-insensitive)."""
    if not names:
        return {}
    lowered = [n.casefold() for n in names]
    rows = (
        await db.execute(
            select(func.lower(Address.postal_city), func.count())
            .where(func.lower(Address.postal_city).in_(lowered))
            .group_by(func.lower(Address.postal_city))
        )
    ).all()
    return dict(rows)


async def _rematch_unmatched_addresses(db: AsyncSession) -> int:
    """
    Give every address that has no courier id another look after a sync.

    One set-based UPDATE rather than a loop: a directory sync that adds a
    district should make every customer already living there quotable in the
    same breath, not one checkout at a time.
    """
    directory = (
        select(PostalCityDirectory.courier_postal_city_id)
        .where(
            func.lower(PostalCityDirectory.postal_city) == func.lower(Address.postal_city),
            func.lower(PostalCityDirectory.district) == func.lower(Address.district),
            PostalCityDirectory.is_active.is_(True),
            PostalCityDirectory.courier_postal_city_id.isnot(None),
        )
        .scalar_subquery()
    )
    result = await db.execute(
        update(Address)
        .where(Address.courier_postal_city_id.is_(None))
        .where(directory.isnot(None))
        .values(courier_postal_city_id=directory)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def sync(db: AsyncSession) -> CoverageSyncResult:
    """
    Fetch ``GET /ecommerce/postal-cities`` and reconcile ``post_office_directory``.

    Raises ``CourierUnavailableError`` / ``CourierError`` /
    ``CourierNotConfiguredError`` — this is an operator-triggered action, so a
    failure is surfaced rather than swallowed into a partial sync.
    """
    # Any configured account will do: the postal city list describes where
    # SribeesExpress delivers, which is the same answer for every merchant.
    target = await courier_credentials.any_target(db)
    async with courier_credentials.using(target):
        rows = await CourierService.list_postal_cities()
    result = CoverageSyncResult(fetched=len(rows))
    if not rows:
        # An empty response means they have configured nothing at all. Treating
        # that as "deactivate everything" would take the whole directory
        # offline on a bad deploy of theirs, so it is a no-op instead.
        logger.warning("Courier postal-cities returned no rows; leaving the directory untouched")
        return result

    now = datetime.now(timezone.utc)

    offered: Dict[Tuple[str, str], dict] = {}
    covered_districts: set[str] = set()
    for row in rows:
        name = (row.get("name") or "").strip()
        district = (row.get("district") or "").strip()
        if not name or row.get("id") is None:
            continue
        offered[(_norm(name), _norm(district))] = row
        if district:
            covered_districts.add(_norm(district))
    result.districts_covered = sorted(
        {(r.get("district") or "").strip() for r in rows if (r.get("district") or "").strip()}
    )

    existing = (await db.execute(select(PostalCityDirectory))).scalars().all()
    by_key = {(_norm(e.postal_city), _norm(e.district)): e for e in existing}
    by_name = {_norm(e.postal_city): e for e in existing}

    # 1. Upsert everything they deliver to.
    for key, row in offered.items():
        name = (row.get("name") or "").strip()
        district = (row.get("district") or "").strip()
        # "Western Province" -> "Western": the spelling used everywhere here.
        province = canonical_province(row.get("province"))
        courier_id = row.get("id")
        entry = by_key.get(key)

        if entry is None:
            holder = by_name.get(key[0])
            if holder is not None:
                result.skipped.append(
                    {"postal_city": name, "district": district, "held_by_district": holder.district}
                )
                continue
            entry = PostalCityDirectory(
                postal_city=name,
                district=district,
                province=province,
                is_active=True,
                courier_postal_city_id=courier_id,
                courier_synced_at=now,
            )
            db.add(entry)
            by_key[key] = entry
            by_name[key[0]] = entry
            result.created += 1
            continue

        changed = False
        if entry.postal_city != name:
            entry.postal_city = name
            changed = True
        if district and entry.district != district:
            entry.district = district
            changed = True
        if province and entry.province != province:
            entry.province = province
            changed = True
        if entry.courier_postal_city_id != courier_id:
            entry.courier_postal_city_id = courier_id
            changed = True
        if not entry.is_active:
            entry.is_active = True
            result.reactivated += 1
            changed = True
        entry.courier_synced_at = now
        if changed:
            result.updated += 1

    # 2. Deactivate rows inside a district they DO report on but did not list.
    to_deactivate: List[PostalCityDirectory] = [
        entry
        for key, entry in by_key.items()
        if key not in offered and key[1] in covered_districts and entry.is_active
    ]
    for entry in to_deactivate:
        entry.is_active = False
        entry.courier_synced_at = now
        result.deactivated += 1

    await db.flush()
    result.addresses_matched = await _rematch_unmatched_addresses(db)
    await db.commit()

    # 3. Report what we just switched off that is still referenced.
    if to_deactivate:
        names = [e.postal_city for e in to_deactivate]
        address_counts = await _count_addresses_by_postal_city(db, names)
        for entry in to_deactivate:
            result.orphaned.append(
                {
                    "postal_city": entry.postal_city,
                    "district": entry.district,
                    "addresses": address_counts.get(entry.postal_city.casefold(), 0),
                }
            )
        logger.warning(
            f"Postal city sync deactivated {len(to_deactivate)} row(s) "
            f"SribeesExpress does not serve: {', '.join(names)}"
        )

    logger.info(
        f"Postal city sync: fetched={result.fetched} created={result.created} "
        f"updated={result.updated} reactivated={result.reactivated} "
        f"deactivated={result.deactivated} addresses_matched={result.addresses_matched} "
        f"skipped={len(result.skipped)} districts={result.districts_covered}"
    )
    return result


async def unserviceable_postal_cities(db: AsyncSession) -> List[Tuple[str, str]]:
    """Directory rows a sync switched off — what checkout must refuse honestly."""
    rows = (
        await db.execute(
            select(PostalCityDirectory.postal_city, PostalCityDirectory.district)
            .where(PostalCityDirectory.is_active.is_(False))
            .order_by(PostalCityDirectory.district, PostalCityDirectory.postal_city)
        )
    ).all()
    return list(rows)
