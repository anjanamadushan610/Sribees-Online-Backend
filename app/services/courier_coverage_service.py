"""
Courier Coverage Sync — populate ``post_office_directory`` from SribeesExpress.

``post_office_directory`` was seeded by hand in migration 018 with three rows,
two of which SribeesExpress cannot match: we typed ``Mathugama`` and
``Meegahathanna``, their directory holds ``Matugama`` and ``Meegahatenna``.
Matching on their side is exact and case-insensitive with no fuzzy fallback, so
each mismatch surfaces at checkout as "that address is not serviceable" —
indistinguishable from a genuinely uncovered area.

Hand-correcting those two names is the wrong fix, because it is the same
keystroke-by-keystroke process that produced them. ``GET /ecommerce/coverage``
exists for exactly this: it returns every post office SribeesExpress can
deliver to, in their spelling, with the ``city_id`` each one resolves to.

Two deliberate decisions in here:

**Only districts SribeesExpress reports are reconciled.** Coverage today
returns one district (Kalutara, 84 post offices) because that is all they have
configured. Deactivating every directory row absent from that response would
empty the directory for every district we serve with our own fleet. So a row
is deactivated only when its own district appears in the response and its post
office does not — inside a district they report on, absence is real.

**A deactivated row that something still points at is reported, not hidden.**
Branch coverage mappings and saved customer addresses reference post offices by
name. Deactivating ``Mathugama`` without saying so would break those silently,
so the sync counts and returns them for the caller to surface.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import PostOfficeDirectory
from app.models.user import Address
from app.services.courier_service import (
    CourierService,
)


@dataclass
class CoverageSyncResult:
    """What one sync run changed, for the admin UI and the logs."""

    fetched: int = 0
    created: int = 0
    updated: int = 0
    reactivated: int = 0
    deactivated: int = 0
    districts_covered: List[str] = field(default_factory=list)
    # Rows this run switched off that something still references by name.
    # ``{"post_office": ..., "district": ..., "addresses": n}``
    orphaned: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "reactivated": self.reactivated,
            "deactivated": self.deactivated,
            "districts_covered": self.districts_covered,
            "orphaned": self.orphaned,
        }


def _normalize(name: Optional[str]) -> str:
    """Their matching key: trimmed and case-folded, never fuzzy."""
    return (name or "").strip().casefold()


async def _count_addresses_by_post_office(db: AsyncSession, names: List[str]) -> Dict[str, int]:
    """How many saved addresses still name each of ``names`` (case-insensitive)."""
    if not names:
        return {}
    lowered = [n.casefold() for n in names]
    rows = (
        await db.execute(
            select(func.lower(Address.post_office), func.count())
            .where(func.lower(Address.post_office).in_(lowered))
            .group_by(func.lower(Address.post_office))
        )
    ).all()
    return dict(rows)


async def sync(db: AsyncSession) -> CoverageSyncResult:
    """
    Fetch ``GET /ecommerce/coverage`` and reconcile ``post_office_directory``.

    Raises ``CourierUnavailableError`` / ``CourierError`` — unlike address
    resolution, this is an operator-triggered action, so a failure is worth
    surfacing as an error rather than swallowing into a partial sync.
    """
    rows = await CourierService.get_coverage()
    result = CoverageSyncResult(fetched=len(rows))
    if not rows:
        # An empty response means they have configured nothing at all. Treating
        # that as "deactivate everything" would take the whole directory
        # offline on a bad deploy of theirs, so it is a no-op instead.
        logger.warning("Courier coverage returned no rows; leaving the directory untouched")
        return result

    now = datetime.now(timezone.utc)

    # Their spelling wins, so the response is keyed on the normalized name and
    # our rows are matched against it -- not the other way round.
    coverage: Dict[str, dict] = {}
    covered_districts: set[str] = set()
    for row in rows:
        post_office = (row.get("post_office") or "").strip()
        if not post_office:
            continue
        coverage[_normalize(post_office)] = row
        district = (row.get("district") or "").strip()
        if district:
            covered_districts.add(_normalize(district))
    result.districts_covered = sorted(
        {(r.get("district") or "").strip() for r in rows if (r.get("district") or "").strip()}
    )

    existing = (await db.execute(select(PostOfficeDirectory))).scalars().all()
    by_name = {_normalize(e.post_office): e for e in existing}

    # 1. Upsert everything they cover.
    for key, row in coverage.items():
        entry = by_name.get(key)
        district = (row.get("district") or "").strip()
        province = (row.get("province") or "").strip()
        city_id = row.get("city_id")

        if entry is None:
            db.add(
                PostOfficeDirectory(
                    post_office=(row.get("post_office") or "").strip(),
                    district=district,
                    province=province,
                    is_active=True,
                    courier_city_id=city_id,
                    courier_synced_at=now,
                )
            )
            result.created += 1
            continue

        changed = False
        # Adopt their exact spelling, casing included -- ours is the copy.
        if entry.post_office != (row.get("post_office") or "").strip():
            entry.post_office = (row.get("post_office") or "").strip()
            changed = True
        if district and entry.district != district:
            entry.district = district
            changed = True
        if province and entry.province != province:
            entry.province = province
            changed = True
        if entry.courier_city_id != city_id:
            entry.courier_city_id = city_id
            changed = True
        if not entry.is_active:
            entry.is_active = True
            result.reactivated += 1
            changed = True
        entry.courier_synced_at = now
        if changed:
            result.updated += 1

    # 2. Deactivate rows inside a district they DO cover but did not list.
    #    Outside those districts we have no signal, so we leave the row alone.
    to_deactivate: List[PostOfficeDirectory] = [
        entry
        for key, entry in by_name.items()
        if key not in coverage
        and _normalize(entry.district) in covered_districts
        and entry.is_active
    ]
    for entry in to_deactivate:
        entry.is_active = False
        entry.courier_synced_at = now
        result.deactivated += 1

    await db.commit()

    # 3. Report what we just switched off that is still referenced. Done after
    #    the commit so the counts describe the state an operator will see.
    if to_deactivate:
        names = [e.post_office for e in to_deactivate]
        address_counts = await _count_addresses_by_post_office(db, names)
        for entry in to_deactivate:
            result.orphaned.append(
                {
                    "post_office": entry.post_office,
                    "district": entry.district,
                    "addresses": address_counts.get(entry.post_office.casefold(), 0),
                }
            )
        logger.warning(
            f"Coverage sync deactivated {len(to_deactivate)} post office(s) "
            f"SribeesExpress does not serve: {', '.join(names)}"
        )

    logger.info(
        f"Coverage sync: fetched={result.fetched} created={result.created} "
        f"updated={result.updated} reactivated={result.reactivated} "
        f"deactivated={result.deactivated} districts={result.districts_covered}"
    )
    return result


async def city_id_for(db: AsyncSession, post_office: str, district: str) -> Optional[int]:
    """
    The ``city_id`` coverage last reported for a post office, or None.

    Lets a quote skip ``POST /locations/resolve`` for an address whose post
    office the last sync already priced. Address-level caching
    (``courier_location_service``) stays the primary path; this is the
    directory-level answer behind it.
    """
    row = (
        await db.execute(
            select(PostOfficeDirectory.courier_city_id).where(
                func.lower(PostOfficeDirectory.post_office) == post_office.casefold(),
                func.lower(PostOfficeDirectory.district) == district.casefold(),
                PostOfficeDirectory.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    return row


async def unserviceable_post_offices(db: AsyncSession) -> List[Tuple[str, str]]:
    """Directory rows a sync switched off — what checkout must refuse honestly."""
    rows = (
        await db.execute(
            select(PostOfficeDirectory.post_office, PostOfficeDirectory.district)
            .where(PostOfficeDirectory.is_active.is_(False))
            .order_by(PostOfficeDirectory.district, PostOfficeDirectory.post_office)
        )
    ).all()
    return [(po, d) for po, d in rows]
