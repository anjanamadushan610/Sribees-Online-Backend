"""
SRIBEESonline - Location rollout: where customers may be served.

The business opens district by district, and the Super Admin decides the pace
with three switches (Settings > Service Areas in the admin dashboard):

    province unlocked  ->  district unlocked  ->  postal area active

A postal area is *active* when it is launched
(``PostalCityDirectory.is_launched``, migration 067). Which branch serves it is
decided somewhere else, on purpose: the branch form's coverage picker offers
only active postal areas, and assigning one there writes the
``PostalCityBranchMapping`` that routes its orders. An active postal area with
no branch yet is not offered to customers: there would be nobody to deliver.

**One rule, applied everywhere.** :func:`served_mapping_clause` is the only
definition of "a customer may be served here". The address pickers use it, and
so does address -> branch resolution at checkout, so the admin screen, the
dropdowns and the checkout can never disagree about a district. Locking a
district stops new orders there at once; unlocking it brings its active
postal areas straight back.

**Coming soon is data, not a flag.** Locked provinces and districts are still
listed to the app (``include_upcoming``), marked ``available: false``, and the
app draws them greyed out with a "Coming soon" tag. An unlocked province or
district with nothing active in it is also not available: offering a place
whose next dropdown is empty is a dead end, not a launch. When every province
is live there is simply nothing left to grey out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import PostalCityBranchMapping, PostalCityDirectory
from app.models.location import LocationDistrict, LocationProvince
from app.models.user import Address

_PROVINCE_SUFFIX = re.compile(r"\s+province$", re.IGNORECASE)


class RolloutError(ValueError):
    """A change the rollout rules refuse, with a message for the admin."""


def canonical_province(name: Optional[str]) -> str:
    """``"Western Province"`` -> ``"Western"``: one spelling everywhere.

    SribeesExpress sends the long form; branches, mappings and addresses here
    use the short one. Migration 066 normalised the stored rows and the courier
    sync stores through this, so the two never drift apart again.
    """
    return _PROVINCE_SUFFIX.sub("", (name or "").strip())


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def unlocked_district_names():
    """Lower-cased names of districts that are unlocked in an unlocked province."""
    return (
        select(func.lower(LocationDistrict.name))
        .join(LocationProvince, LocationProvince.name == LocationDistrict.province)
        .where(
            LocationDistrict.is_unlocked.is_(True),
            LocationProvince.is_unlocked.is_(True),
        )
    )


def launched_postal_names():
    """Lower-cased names of postal areas switched on in Service Areas."""
    return select(func.lower(PostalCityDirectory.postal_city)).where(
        PostalCityDirectory.is_launched.is_(True)
    )


def served_mapping_clause():
    """The one definition of "a customer may be served in this postal area":
    a branch serves it, the postal area is on, and its district and province
    are unlocked."""
    return and_(
        PostalCityBranchMapping.is_active.is_(True),
        func.lower(PostalCityBranchMapping.postal_city).in_(launched_postal_names()),
        func.lower(PostalCityBranchMapping.district).in_(unlocked_district_names()),
    )


def _names(row) -> dict:
    return {"en": row.name, "si": row.name_si or row.name, "ta": row.name_ta or row.name}


# ---------------------------------------------------------------------------
# What customers see
# ---------------------------------------------------------------------------


async def _served_counts_by_district(db: AsyncSession) -> dict[str, dict]:
    """{lower(district): {postal, branches}} for every served postal area."""
    rows = await db.execute(
        select(
            func.lower(PostalCityBranchMapping.district),
            func.count(PostalCityBranchMapping.postal_city),
            func.array_agg(func.distinct(PostalCityBranchMapping.branch_name)),
            func.array_agg(func.distinct(PostalCityBranchMapping.branch_id)),
        )
        .where(served_mapping_clause())
        .group_by(func.lower(PostalCityBranchMapping.district))
    )
    return {
        district: {"postal": count, "branch_names": sorted(n for n in names if n), "branch_ids": set(ids)}
        for district, count, names, ids in rows.all()
    }


async def customer_provinces(db: AsyncSession, *, include_upcoming: bool) -> list[dict]:
    provinces = (
        await db.execute(select(LocationProvince).order_by(LocationProvince.sort_order, LocationProvince.name))
    ).scalars().all()
    districts = (await db.execute(select(LocationDistrict))).scalars().all()
    served = await _served_counts_by_district(db)

    result = []
    for p in provinces:
        live = [
            d for d in districts
            if d.province == p.name and d.is_unlocked and served.get(d.name.lower(), {}).get("postal")
        ]
        postal = sum(served[d.name.lower()]["postal"] for d in live)
        branch_ids = set().union(*(served[d.name.lower()]["branch_ids"] for d in live)) if live else set()
        available = p.is_unlocked and postal > 0
        if not available and not include_upcoming:
            continue
        result.append({
            "province": p.name,
            "displayName": _names(p),
            "available": available,
            "comingSoon": not available,
            "districtCount": len(live),
            "postalCityCount": postal,
            "postOfficeCount": postal,  # legacy key
            "branchCount": len(branch_ids),
            "coverageSummary": (
                f"{p.name} is served by {len(branch_ids)} branch(es) covering "
                f"{len(live)} district(s) and {postal} postal city(ies)."
                if available else f"{p.name}: coming soon."
            ),
        })
    return result


async def customer_districts(db: AsyncSession, province: str, *, include_upcoming: bool) -> list[dict]:
    wanted = canonical_province(province).lower()
    prov = (
        await db.execute(select(LocationProvince).where(func.lower(LocationProvince.name) == wanted))
    ).scalar_one_or_none()
    if prov is None:
        return []
    districts = (
        await db.execute(
            select(LocationDistrict)
            .where(LocationDistrict.province == prov.name)
            .order_by(LocationDistrict.sort_order, LocationDistrict.name)
        )
    ).scalars().all()
    served = await _served_counts_by_district(db)

    result = []
    for d in districts:
        s = served.get(d.name.lower(), {})
        postal = s.get("postal", 0)
        available = prov.is_unlocked and d.is_unlocked and postal > 0
        if not available and not include_upcoming:
            continue
        names = s.get("branch_names", [])
        result.append({
            "district": d.name,
            "province": prov.name,
            "displayName": _names(d),
            "available": available,
            "comingSoon": not available,
            "postalCityCount": postal,
            "postOfficeCount": postal,  # legacy key
            "branchNames": names,
            "coverageSummary": (
                f"{d.name} district in {prov.name} has {postal} postal city(ies) "
                f"served by: {', '.join(names)}."
                if available else f"{d.name}: coming soon."
            ),
        })
    return result


# ---------------------------------------------------------------------------
# The Super Admin's view
# ---------------------------------------------------------------------------


async def _grouped(db: AsyncSession, stmt) -> dict[str, int]:
    return {k: v for k, v in (await db.execute(stmt)).all() if k is not None}


async def admin_overview(db: AsyncSession) -> dict:
    """Every province and district with its switch and what is behind it."""
    provinces = (
        await db.execute(select(LocationProvince).order_by(LocationProvince.sort_order, LocationProvince.name))
    ).scalars().all()
    districts = (
        await db.execute(select(LocationDistrict).order_by(LocationDistrict.sort_order, LocationDistrict.name))
    ).scalars().all()

    d_key = func.lower(PostalCityDirectory.district)
    total = await _grouped(db, select(d_key, func.count()).group_by(d_key))
    linked = await _grouped(
        db,
        select(d_key, func.count())
        .where(PostalCityDirectory.is_active.is_(True), PostalCityDirectory.courier_postal_city_id.isnot(None))
        .group_by(d_key),
    )
    active = await _grouped(
        db,
        select(d_key, func.count()).where(PostalCityDirectory.is_launched.is_(True)).group_by(d_key),
    )
    # Active AND a branch serves it: what a customer can actually pick once
    # the district is unlocked.
    m_key = func.lower(PostalCityBranchMapping.district)
    serving = await _grouped(
        db,
        select(m_key, func.count())
        .where(
            PostalCityBranchMapping.is_active.is_(True),
            func.lower(PostalCityBranchMapping.postal_city).in_(launched_postal_names()),
        )
        .group_by(m_key),
    )
    a_key = func.lower(Address.district)
    addresses = await _grouped(db, select(a_key, func.count()).group_by(a_key))

    out = []
    for p in provinces:
        rows = []
        for d in (d for d in districts if d.province == p.name):
            k = d.name.lower()
            n_active = active.get(k, 0)
            n_serving = serving.get(k, 0)
            rows.append({
                "name": d.name,
                "displayName": _names(d),
                "is_unlocked": d.is_unlocked,
                "unlocked_at": d.unlocked_at.isoformat() if d.unlocked_at else None,
                "updated_by": d.updated_by,
                "postal_total": total.get(k, 0),
                "courier_linked": linked.get(k, 0),
                "active": n_active,
                # Active but no branch assigned yet: not offered to customers.
                "needs_branch": max(n_active - n_serving, 0),
                "saved_addresses": addresses.get(k, 0),
                # What a customer actually gets: the switch alone is not enough.
                "live": p.is_unlocked and d.is_unlocked and n_serving > 0,
            })
        out.append({
            "name": p.name,
            "displayName": _names(p),
            "is_unlocked": p.is_unlocked,
            "unlocked_at": p.unlocked_at.isoformat() if p.unlocked_at else None,
            "updated_by": p.updated_by,
            "live": any(r["live"] for r in rows),
            "districts": rows,
        })

    all_d = [r for p in out for r in p["districts"]]
    return {
        "provinces": out,
        "totals": {
            "provinces": len(out),
            "provinces_live": sum(1 for p in out if p["live"]),
            "districts": len(all_d),
            "districts_unlocked": sum(1 for r in all_d if r["is_unlocked"]),
            "districts_live": sum(1 for r in all_d if r["live"]),
            "postal_total": sum(total.values()),
            "postal_active": sum(active.values()),
            "postal_serving": sum(serving.values()),
            "courier_linked": sum(linked.values()),
        },
    }


async def _province(db: AsyncSession, name: str) -> LocationProvince:
    row = (
        await db.execute(
            select(LocationProvince).where(func.lower(LocationProvince.name) == canonical_province(name).lower())
        )
    ).scalar_one_or_none()
    if row is None:
        raise RolloutError(f"Unknown province '{name}'.")
    return row


async def _district(db: AsyncSession, name: str) -> LocationDistrict:
    row = (
        await db.execute(select(LocationDistrict).where(func.lower(LocationDistrict.name) == name.strip().lower()))
    ).scalar_one_or_none()
    if row is None:
        raise RolloutError(f"Unknown district '{name}'.")
    return row


def _stamp(row, unlocked: bool, actor: Optional[str]) -> None:
    if unlocked and not row.is_unlocked:
        row.unlocked_at = datetime.now(timezone.utc)
    row.is_unlocked = unlocked
    row.updated_by = actor


async def set_province_unlocked(db: AsyncSession, name: str, unlocked: bool, actor: Optional[str]) -> None:
    """Locking hides every district in it from customers; their own switches
    are kept, so unlocking again restores exactly what was live."""
    row = await _province(db, name)
    _stamp(row, unlocked, actor)
    await db.commit()


async def set_district_unlocked(db: AsyncSession, name: str, unlocked: bool, actor: Optional[str]) -> None:
    row = await _district(db, name)
    if unlocked:
        province = await _province(db, row.province)
        if not province.is_unlocked:
            raise RolloutError(f"Unlock {province.name} province first; {row.name} is in it.")
    _stamp(row, unlocked, actor)
    await db.commit()


@dataclass
class ActivationResult:
    activated: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"activated": self.activated, "skipped": self.skipped}


async def district_postal_areas(db: AsyncSession, district: str) -> dict:
    """Every postal area of a district, with its courier state, branch and switch."""
    d = await _district(db, district)
    key = d.name.lower()
    directory = (
        await db.execute(
            select(PostalCityDirectory)
            .where(func.lower(PostalCityDirectory.district) == key)
            .order_by(PostalCityDirectory.postal_city)
        )
    ).scalars().all()
    mappings = (
        await db.execute(
            select(PostalCityBranchMapping).where(func.lower(PostalCityBranchMapping.district) == key)
        )
    ).scalars().all()
    by_name = {m.postal_city.lower(): m for m in mappings}
    a_key = func.lower(Address.postal_city)
    addresses = await _grouped(
        db,
        select(a_key, func.count()).where(func.lower(Address.district) == key).group_by(a_key),
    )

    def courier_state(entry: Optional[PostalCityDirectory]) -> str:
        if entry is None:
            return "unknown"
        if not entry.is_active:
            return "not_served"
        return "linked" if entry.courier_postal_city_id is not None else "not_linked"

    rows = []
    for entry in directory:
        m = by_name.get(entry.postal_city.lower())
        serving = m if (m is not None and m.is_active) else None
        rows.append({
            "postal_city": entry.postal_city,
            "courier": courier_state(entry),
            # Read-only here: a branch is given its postal areas on the branch
            # form, which offers only the active ones.
            "branch_id": str(serving.branch_id) if serving else None,
            "branch_name": serving.branch_name if serving else None,
            "is_active": entry.is_launched,
            "saved_addresses": addresses.get(entry.postal_city.lower(), 0),
        })
    rows.sort(key=lambda r: r["postal_city"].lower())
    return {"district": d.name, "province": d.province, "is_unlocked": d.is_unlocked, "postal_areas": rows}


async def activate_postal_areas(
    db: AsyncSession, district: str, postal_cities: Iterable[str]
) -> ActivationResult:
    """Switch postal areas on, so the branch form offers them.

    Refused wholesale when the district is locked. Per-area problems (not in
    this district, SribeesExpress does not deliver there) skip that area with
    a reason and let the rest through.
    """
    d = await _district(db, district)
    if not d.is_unlocked:
        raise RolloutError(f"Unlock {d.name} district before activating its postal areas.")

    wanted = {pc.strip().lower(): pc.strip() for pc in postal_cities if pc and pc.strip()}
    directory = {
        e.postal_city.lower(): e
        for e in (
            await db.execute(
                select(PostalCityDirectory).where(func.lower(PostalCityDirectory.postal_city).in_(list(wanted)))
            )
        ).scalars().all()
    }

    result = ActivationResult()
    for key, given in sorted(wanted.items()):
        entry = directory.get(key)
        if entry is None:
            result.skipped.append({"postal_city": given, "reason": "Not in the postal city directory."})
            continue
        if entry.district.lower() != d.name.lower():
            result.skipped.append({"postal_city": given, "reason": f"It is in {entry.district} district."})
            continue
        if not entry.is_active:
            result.skipped.append(
                {"postal_city": given, "reason": "SribeesExpress does not deliver there yet."}
            )
            continue
        entry.is_launched = True
        result.activated.append(entry.postal_city)

    await db.commit()
    return result


async def deactivate_postal_areas(db: AsyncSession, postal_cities: Iterable[str]) -> int:
    """Switch postal areas off. A branch that covers one keeps it on its list,
    so switching it back on restores service without editing the branch."""
    wanted = [pc.strip().lower() for pc in postal_cities if pc and pc.strip()]
    if not wanted:
        return 0
    entries = (
        await db.execute(
            select(PostalCityDirectory).where(
                func.lower(PostalCityDirectory.postal_city).in_(wanted),
                PostalCityDirectory.is_launched.is_(True),
            )
        )
    ).scalars().all()
    for e in entries:
        e.is_launched = False
    await db.commit()
    return len(entries)
