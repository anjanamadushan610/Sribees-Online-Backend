"""
SRIBEESonline - Branch Resolution & Location Management Service

Handles:
  1. Hierarchical location discovery  (province → district → postal city)
  2. Branch resolution via saved address  OR  location triplet
  3. Redis branch-context session management
  4. Admin CRUD for PostalCityBranchMapping

**Legacy keys in customer-facing payloads.** The discovery and context payloads
below carry ``postalCity`` / ``postal_city`` and, beside them, the ``postOffice``
/ ``post_office`` keys the mobile app read before the rename. Installed builds
parse those keys; dropping them would blank the address dropdowns for every
customer who has not updated. Remove them once the minimum supported app
version reads the new names.
"""
import json
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.config.redis import RedisKeys, RedisTTL
from app.core.exceptions import (
    DuplicateError,
    LocationNotServedError,
    NotFoundError,
)
from app.models.branch import (
    Branch,
    PostalCityBranchMapping,
    PostalCityDirectory,
)
from app.models.user import Address
from app.utils.logger import logger


# ============================================================================
# Legacy key mapper
# ============================================================================

def context_payload(ctx: Optional[dict]) -> Optional[dict]:
    """
    Produce the branch context payload, injecting legacy keys for older clients.
    """
    if ctx is None:
        return None
    out = dict(ctx)
    if "postalCity" in out and "postOffice" not in out:
        out["postOffice"] = out["postalCity"]
    if "postal_city" in out and "post_office" not in out:
        out["post_office"] = out["postal_city"]
    return out


def _delivery_info(m: PostalCityBranchMapping) -> dict:
    return {
        "postalCity": m.postal_city,
        "postOffice": m.postal_city,
        "district": m.district,
        "province": m.province,
    }


def _build_branch_context(
    branch_id: UUID,
    branch_name: str,
    postal_city: str,
    delivery_info: Optional[dict] = None,
) -> dict:
    return {
        "branch_id": str(branch_id),
        "branchId": str(branch_id),
        "branch_name": branch_name,
        "branchName": branch_name,
        "postal_city": postal_city,
        "postalCity": postal_city,
        "post_office": postal_city,
        "postOffice": postal_city,
        "delivery_info": delivery_info or {},
    }


# ============================================================================
# Hierarchical Location Discovery  (for cascading dropdowns)
# ============================================================================

async def list_provinces(db: AsyncSession) -> list[dict]:
    """
    Return every province that has at least one active mapping,
    together with AI-friendly coverage metadata.
    """
    rows = await db.execute(
        select(
            PostalCityBranchMapping.province,
            func.count(distinct(PostalCityBranchMapping.district)).label("district_count"),
            func.count(PostalCityBranchMapping.postal_city).label("postal_city_count"),
            func.count(distinct(PostalCityBranchMapping.branch_id)).label("branch_count"),
        )
        .where(
            PostalCityBranchMapping.is_active.is_(True),
            PostalCityBranchMapping.province.isnot(None),
        )
        .group_by(PostalCityBranchMapping.province)
        .order_by(PostalCityBranchMapping.province)
    )

    result = []
    for row in rows.all():
        province, dist_ct, pc_ct, br_ct = row
        result.append({
            "province": province,
            "districtCount": dist_ct,
            "postalCityCount": pc_ct,
            "postOfficeCount": pc_ct,  # legacy key, see module docstring
            "branchCount": br_ct,
            "coverageSummary": (
                f"{province} is served by {br_ct} branch(es) covering "
                f"{dist_ct} district(s) and {pc_ct} postal city(ies)."
            ),
        })
    return result


async def list_districts(db: AsyncSession, province: str) -> list[dict]:
    """
    Return districts within a province together with coverage metadata.
    """
    normalized = province.strip().title()

    rows = await db.execute(
        select(
            PostalCityBranchMapping.district,
            PostalCityBranchMapping.province,
            func.count(PostalCityBranchMapping.postal_city).label("postal_city_count"),
            func.array_agg(distinct(PostalCityBranchMapping.branch_name)).label("branch_names"),
        )
        .where(
            PostalCityBranchMapping.is_active.is_(True),
            PostalCityBranchMapping.province == normalized,
            PostalCityBranchMapping.district.isnot(None),
        )
        .group_by(PostalCityBranchMapping.district, PostalCityBranchMapping.province)
        .order_by(PostalCityBranchMapping.district)
    )

    result = []
    for row in rows.all():
        district, prov, pc_ct, branch_names = row
        names_list = sorted(branch_names) if branch_names else []
        result.append({
            "district": district,
            "province": prov,
            "postalCityCount": pc_ct,
            "postOfficeCount": pc_ct,  # legacy key, see module docstring
            "branchNames": names_list,
            "coverageSummary": (
                f"{district} district in {prov} has {pc_ct} postal city(ies) "
                f"served by: {', '.join(names_list)}."
            ),
        })
    return result


async def list_postal_cities_for_district(
    db: AsyncSession,
    district: str,
) -> list[dict]:
    """
    Return postal cities within a district with branch details.
    Uses ``joinedload`` on Branch for optimized fetching.
    """
    normalized = district.strip().title()

    result = await db.execute(
        select(PostalCityBranchMapping)
        .options(joinedload(PostalCityBranchMapping.branch))
        .where(
            PostalCityBranchMapping.is_active.is_(True),
            PostalCityBranchMapping.district == normalized,
        )
        .order_by(PostalCityBranchMapping.postal_city)
    )

    mappings = result.unique().scalars().all()
    return [
        {
            "postalCity": m.postal_city,
            "postOffice": m.postal_city,  # legacy key, see module docstring
            "district": m.district,
            "province": m.province,
            "branchId": str(m.branch_id),
            "branchName": m.branch_name,
            "isActive": m.is_active,
        }
        for m in mappings
    ]


# ============================================================================
# Data-layer look-ups
# ============================================================================

async def get_address_for_user(
    db: AsyncSession,
    address_id: UUID,
    user_id: UUID,
) -> Optional[Address]:
    """Fetch an address that belongs to the given user."""
    result = await db.execute(
        select(Address).where(
            Address.address_id == address_id,
            Address.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def resolve_branch_by_postal_city(
    db: AsyncSession,
    postal_city_name: str,
) -> Optional[PostalCityBranchMapping]:
    """Look up which branch serves the given postal city (active only)."""
    normalized = postal_city_name.strip().lower()
    result = await db.execute(
        select(PostalCityBranchMapping)
        .options(joinedload(PostalCityBranchMapping.branch))
        .where(
            func.lower(PostalCityBranchMapping.postal_city) == normalized,
            PostalCityBranchMapping.is_active.is_(True),
        )
    )
    return result.unique().scalar_one_or_none()


async def resolve_branch_by_location_triplet(
    db: AsyncSession,
    province: str,
    district: str,
    postal_city: str,
) -> Optional[PostalCityBranchMapping]:
    """
    Resolve a branch using the full ``{province, district, postal_city}`` triplet.
    Uses case-insensitive matching with flexible province comparison, and
    falls back directly to postal_city resolution for complete parity with
    registered address resolution.
    """
    prov = province.strip().lower()
    dist = district.strip().lower()
    pc = postal_city.strip().lower()

    # 1. Try case-insensitive triplet match with flexible province name
    stmt = (
        select(PostalCityBranchMapping)
        .options(joinedload(PostalCityBranchMapping.branch))
        .where(
            func.lower(PostalCityBranchMapping.postal_city) == pc,
            PostalCityBranchMapping.is_active.is_(True),
        )
    )
    if dist:
        stmt = stmt.where(func.lower(PostalCityBranchMapping.district) == dist)
    if prov:
        stmt = stmt.where(
            or_(
                func.lower(PostalCityBranchMapping.province) == prov,
                func.lower(PostalCityBranchMapping.province).like(f"{prov}%"),
                PostalCityBranchMapping.province.is_(None),
            )
        )

    result = await db.execute(stmt)
    mapping = result.unique().scalar_one_or_none()
    if mapping is not None:
        return mapping

    # 2. Fallback to direct postal city resolution (same as registered address lookup)
    return await resolve_branch_by_postal_city(db, postal_city)


async def list_served_postal_cities(db: AsyncSession) -> list[str]:
    """Return a sorted list of all actively-served postal city names."""
    result = await db.execute(
        select(PostalCityBranchMapping.postal_city)
        .where(PostalCityBranchMapping.is_active.is_(True))
        .order_by(PostalCityBranchMapping.postal_city)
    )
    return [row[0] for row in result.all()]


# ============================================================================
# Redis branch-context helpers
# ============================================================================

def _branch_session_id(user_id: UUID) -> str:
    """Normalise user_id to Redis session id."""
    return str(user_id)


async def set_branch_context(
    redis: Redis,
    user_id: UUID,
    branch_id: UUID,
    branch_name: str,
    postal_city: str,
    delivery_info: Optional[dict] = None,
) -> dict:
    """
    Store the resolved branch context in the user's Redis session.

    Key pattern: ``session:{session_id}:branch_context``
    TTL: 30 days

    The ``delivery_info`` dict may contain ``province``, ``district``,
    and any other address metadata useful for the frontend.
    """
    return await set_branch_context_for_session(
        redis,
        session_id=_branch_session_id(user_id),
        branch_id=branch_id,
        branch_name=branch_name,
        postal_city=postal_city,
        delivery_info=delivery_info,
    )


def _build_branch_context(
    branch_id: UUID,
    branch_name: str,
    postal_city: str,
    delivery_info: Optional[dict] = None,
) -> dict:
    """Build the branch context dict (same shape as stored in Redis)."""
    now = datetime.now(timezone.utc)
    return {
        "branch_id": str(branch_id),
        "branch_name": branch_name,
        "postal_city": postal_city,
        "delivery_info": delivery_info or {},
        "resolved_at": now.isoformat(),
    }


def context_postal_city(context: dict) -> Optional[str]:
    """
    The postal city stored in a branch context.

    A context written before the rename lives in Redis for up to thirty days
    under the old ``post_office`` key; reading only the new key would make every
    such customer look as if they had never chosen a location.
    """
    return context.get("postal_city") or context.get("post_office")


def context_payload(context: dict) -> dict:
    """The wire shape of a branch context, shared by every route that returns one."""
    postal_city = context_postal_city(context)
    return {
        "branchId": context["branch_id"],
        "branchName": context["branch_name"],
        "postalCity": postal_city,
        "postOffice": postal_city,  # legacy key, see module docstring
        "deliveryInfo": context.get("delivery_info", {}),
        "resolvedAt": context["resolved_at"],
    }


def _delivery_info(mapping: PostalCityBranchMapping) -> dict:
    return {
        "province": mapping.province,
        "district": mapping.district,
        "postal_city": mapping.postal_city,
        "post_office": mapping.postal_city,  # legacy key, see module docstring
    }


async def set_branch_context_for_session(
    redis: Redis,
    session_id: str,
    branch_id: UUID,
    branch_name: str,
    postal_city: str,
    delivery_info: Optional[dict] = None,
) -> dict:
    """
    Store branch context by session id (str(user_id) or "guest:{device_id}").
    Key: session:{session_id}:branch_context
    """
    context = _build_branch_context(
        branch_id=branch_id,
        branch_name=branch_name,
        postal_city=postal_city,
        delivery_info=delivery_info,
    )
    key = RedisKeys.branch_context(session_id)
    await redis.set(key, json.dumps(context), ex=RedisTTL.BRANCH_CONTEXT)
    logger.info(
        f"Branch context set for session {session_id}: "
        f"{postal_city} → {branch_name} ({branch_id})"
    )
    return context


async def get_branch_context(
    redis: Redis,
    user_id: UUID,
) -> Optional[dict]:
    """Retrieve the current branch context from Redis."""
    key = RedisKeys.branch_context(str(user_id))
    raw = await redis.get(key)
    if raw is None:
        return None

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"Corrupt branch context for user {user_id}, clearing key")
        await redis.delete(key)
        return None


async def clear_branch_context(
    redis: Redis,
    user_id: UUID,
) -> bool:
    """Remove the branch context from Redis, forcing re-selection."""
    key = RedisKeys.branch_context(str(user_id))
    deleted = await redis.delete(key)
    if deleted:
        logger.info(f"Branch context cleared for user {user_id}")
    return bool(deleted)


# ============================================================================
# High-level orchestration — address-based
# ============================================================================

async def resolve_address_to_branch(
    db: AsyncSession,
    redis: Redis,
    address_id: UUID,
    user_id: UUID,
) -> dict:
    """
    Full resolution flow via saved address:
      1. Validate address ownership.
      2. Extract ``postal_city`` (+ district, province).
      3. Look up ``PostalCityBranchMapping``.
      4. Store branch context (with delivery_info) in Redis.
    """
    address = await get_address_for_user(db, address_id, user_id)
    if address is None:
        raise NotFoundError(
            resource="Address",
            identifier=str(address_id),
            message="Address not found or does not belong to this user",
        )

    postal_city = address.postal_city
    if not postal_city:
        raise NotFoundError(
            resource="Postal City",
            message="The selected address does not have a postal city set",
        )

    mapping = await resolve_branch_by_postal_city(db, postal_city)
    if mapping is None:
        raise LocationNotServedError(
            postal_city=postal_city,
            district=getattr(address, "district", None),
            province=getattr(address, "province", None),
        )

    context = await set_branch_context(
        redis,
        user_id=user_id,
        branch_id=mapping.branch_id,
        branch_name=mapping.branch_name,
        postal_city=mapping.postal_city,
        delivery_info=_delivery_info(mapping),
    )
    return context


# ============================================================================
# High-level orchestration — triplet-based
# ============================================================================

async def resolve_location_to_branch(
    db: AsyncSession,
    redis: Optional[Redis],
    user_id: UUID,
    province: str,
    district: str,
    postal_city: str,
) -> dict:
    """Resolve location triplet and optionally store branch context for a user."""
    return await resolve_location_to_branch_for_session(
        db, redis, _branch_session_id(user_id), province, district, postal_city,
    )


async def resolve_location_to_branch_for_session(
    db: AsyncSession,
    redis: Optional[Redis],
    session_id: str,
    province: str,
    district: str,
    postal_city: str,
) -> dict:
    """
    Resolve a branch using a manually-selected location triplet
    (from the cascading dropdown UI) and optionally store the context in Redis.
    session_id: str(user_id) for authenticated users, or "guest:{device_id}" for guests.
    If Redis is None or storage fails, still returns the resolved context (no 500).
    """
    mapping = await resolve_branch_by_location_triplet(
        db, province, district, postal_city,
    )
    if mapping is None:
        raise LocationNotServedError(
            postal_city=postal_city,
            district=district,
            province=province,
        )

    delivery_info = _delivery_info(mapping)
    context = _build_branch_context(
        branch_id=mapping.branch_id,
        branch_name=mapping.branch_name,
        postal_city=mapping.postal_city,
        delivery_info=delivery_info,
    )

    if redis is not None:
        try:
            await set_branch_context_for_session(
                redis,
                session_id=session_id,
                branch_id=mapping.branch_id,
                branch_name=mapping.branch_name,
                postal_city=mapping.postal_city,
                delivery_info=delivery_info,
            )
        except Exception as e:
            logger.warning(
                "Failed to store branch context in Redis for session %s: %s",
                session_id,
                e,
                exc_info=True,
            )

    return context


# ============================================================================
# Admin CRUD for PostalCityBranchMapping
# ============================================================================

async def get_mapping_by_id(
    db: AsyncSession,
    mapping_id: UUID,
) -> Optional[PostalCityBranchMapping]:
    """Fetch a single mapping by its primary key (with branch eager-loaded)."""
    result = await db.execute(
        select(PostalCityBranchMapping)
        .options(joinedload(PostalCityBranchMapping.branch))
        .where(PostalCityBranchMapping.mapping_id == mapping_id)
    )
    return result.unique().scalar_one_or_none()


async def list_all_mappings(
    db: AsyncSession,
    province: Optional[str] = None,
    district: Optional[str] = None,
    active_only: bool = False,
) -> list[PostalCityBranchMapping]:
    """Return mappings with optional province/district filters (admin use)."""
    stmt = (
        select(PostalCityBranchMapping)
        .options(joinedload(PostalCityBranchMapping.branch))
        .order_by(
            PostalCityBranchMapping.province,
            PostalCityBranchMapping.district,
            PostalCityBranchMapping.postal_city,
        )
    )

    if province:
        stmt = stmt.where(PostalCityBranchMapping.province == province.strip().title())
    if district:
        stmt = stmt.where(PostalCityBranchMapping.district == district.strip().title())
    if active_only:
        stmt = stmt.where(PostalCityBranchMapping.is_active.is_(True))

    result = await db.execute(stmt)
    return list(result.unique().scalars().all())


async def create_mapping(
    db: AsyncSession,
    *,
    postal_city: str,
    branch_id: UUID,
    branch_name: str,
    district: str,
    province: str,
    is_active: bool = True,
) -> PostalCityBranchMapping:
    """
    Create a new PostalCityBranchMapping.

    Raises ``DuplicateError`` if the postal city already exists.
    Raises ``NotFoundError`` if the referenced branch doesn't exist.
    """
    branch_result = await db.execute(
        select(Branch).where(Branch.branch_id == branch_id)
    )
    if branch_result.scalar_one_or_none() is None:
        raise NotFoundError(resource="Branch", identifier=str(branch_id))

    dup = await db.execute(
        select(PostalCityBranchMapping.mapping_id).where(
            PostalCityBranchMapping.postal_city == postal_city.strip().title()
        )
    )
    if dup.scalar_one_or_none() is not None:
        raise DuplicateError(
            field="postal_city",
            message=f"A mapping for postal city '{postal_city}' already exists",
        )

    mapping = PostalCityBranchMapping(
        postal_city=postal_city.strip().title(),
        branch_id=branch_id,
        branch_name=branch_name,
        district=district.strip().title(),
        province=province.strip().title(),
        is_active=is_active,
    )
    db.add(mapping)
    await db.commit()
    await db.refresh(mapping)

    logger.info(f"Mapping created: {mapping.postal_city} → {mapping.branch_name}")
    return mapping


async def update_mapping(
    db: AsyncSession,
    mapping_id: UUID,
    **fields,
) -> PostalCityBranchMapping:
    """
    Update an existing mapping.

    Only non-``None`` values in *fields* are applied.
    """
    mapping = await get_mapping_by_id(db, mapping_id)
    if mapping is None:
        raise NotFoundError(resource="Mapping", identifier=str(mapping_id))

    for key, value in fields.items():
        if value is not None and hasattr(mapping, key):
            setattr(mapping, key, value)

    await db.commit()
    await db.refresh(mapping)

    logger.info(f"Mapping updated: {mapping.mapping_id}")
    return mapping


async def delete_mapping(
    db: AsyncSession,
    mapping_id: UUID,
) -> bool:
    """
    Hard-delete a mapping.  Returns ``True`` if a row was actually removed.
    """
    mapping = await get_mapping_by_id(db, mapping_id)
    if mapping is None:
        raise NotFoundError(resource="Mapping", identifier=str(mapping_id))

    await db.delete(mapping)
    await db.commit()

    logger.info(f"Mapping deleted: {mapping_id}")
    return True


# ============================================================================
# Postal City Directory (Master List / "Delivery Zones") CRUD
# ============================================================================

async def list_directory(
    db: AsyncSession,
    province: Optional[str] = None,
    district: Optional[str] = None,
    active_only: bool = False,
) -> list[PostalCityDirectory]:
    """Return master postal city directory entries with optional filters."""
    stmt = select(PostalCityDirectory).order_by(
        PostalCityDirectory.province,
        PostalCityDirectory.district,
        PostalCityDirectory.postal_city,
    )
    if province:
        stmt = stmt.where(PostalCityDirectory.province == province.strip().title())
    if district:
        stmt = stmt.where(PostalCityDirectory.district == district.strip().title())
    if active_only:
        stmt = stmt.where(PostalCityDirectory.is_active.is_(True))

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_directory_entry(
    db: AsyncSession,
    location_id: UUID,
) -> Optional[PostalCityDirectory]:
    """Fetch a single directory entry by primary key."""
    result = await db.execute(
        select(PostalCityDirectory).where(PostalCityDirectory.id == location_id)
    )
    return result.scalar_one_or_none()


async def create_directory_entry(
    db: AsyncSession,
    *,
    postal_city: str,
    district: str,
    province: str,
    is_active: bool = True,
) -> PostalCityDirectory:
    """
    Add a postal city to the master directory.

    Raises ``DuplicateError`` if the postal city already exists (names are
    unique in this directory).
    """
    name = postal_city.strip().title()
    dup = await db.execute(
        select(PostalCityDirectory.id).where(PostalCityDirectory.postal_city == name)
    )
    if dup.scalar_one_or_none() is not None:
        raise DuplicateError(
            field="postal_city",
            message=f"Postal city '{name}' already exists in the directory",
        )

    entry = PostalCityDirectory(
        postal_city=name,
        district=district.strip().title(),
        province=province.strip().title(),
        is_active=is_active,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    logger.info(f"Directory entry created: {entry.postal_city} ({entry.district})")
    return entry


async def update_directory_entry(
    db: AsyncSession,
    location_id: UUID,
    **fields,
) -> PostalCityDirectory:
    """Update a directory entry. Only non-``None`` fields are applied.

    If the postal city name changes, guards against colliding with another
    existing entry.
    """
    entry = await get_directory_entry(db, location_id)
    if entry is None:
        raise NotFoundError(resource="Postal City", identifier=str(location_id))

    new_name = fields.get("postal_city")
    if new_name and new_name != entry.postal_city:
        dup = await db.execute(
            select(PostalCityDirectory.id).where(
                PostalCityDirectory.postal_city == new_name,
                PostalCityDirectory.id != location_id,
            )
        )
        if dup.scalar_one_or_none() is not None:
            raise DuplicateError(
                field="postal_city",
                message=f"Postal city '{new_name}' already exists in the directory",
            )

    for key, value in fields.items():
        if value is not None and hasattr(entry, key):
            setattr(entry, key, value)

    await db.commit()
    await db.refresh(entry)
    logger.info(f"Directory entry updated: {entry.id}")
    return entry


async def delete_directory_entry(
    db: AsyncSession,
    location_id: UUID,
) -> bool:
    """Hard-delete a directory entry. Returns ``True`` if a row was removed."""
    entry = await get_directory_entry(db, location_id)
    if entry is None:
        raise NotFoundError(resource="Postal City", identifier=str(location_id))

    await db.delete(entry)
    await db.commit()
    logger.info(f"Directory entry deleted: {location_id}")
    return True


# ============================================================================
# Branch coverage sync (Postal City → Branch mappings)
# ============================================================================

async def get_branch_coverage(db: AsyncSession, branch_id: UUID) -> list[str]:
    """Return the sorted postal city names currently mapped to a branch."""
    result = await db.execute(
        select(PostalCityBranchMapping.postal_city)
        .where(PostalCityBranchMapping.branch_id == branch_id)
        .order_by(PostalCityBranchMapping.postal_city)
    )
    return [row[0] for row in result.all()]


async def get_coverage_map(db: AsyncSession) -> dict[UUID, list[str]]:
    """Return {branch_id: [postal_city, ...]} for all mappings (batch, for lists)."""
    result = await db.execute(
        select(
            PostalCityBranchMapping.branch_id,
            PostalCityBranchMapping.postal_city,
        ).order_by(PostalCityBranchMapping.postal_city)
    )
    coverage: dict[UUID, list[str]] = {}
    for branch_id, postal_city in result.all():
        coverage.setdefault(branch_id, []).append(postal_city)
    return coverage


async def sync_branch_coverage(
    db: AsyncSession,
    branch: Branch,
    postal_cities: list[str],
) -> None:
    """
    Make ``PostalCityBranchMapping`` reflect exactly *postal_cities* for *branch*.

    Idempotent set-reconciliation, executed **inside the caller's transaction**
    (no commit here — the branch create/update endpoint commits once so the
    branch row and its coverage move together):

      * A postal city already mapped to this branch → kept / refreshed.
      * A postal city mapped to a *different* branch → reassigned to this branch
        (the name is globally unique, so the latest branch to claim it wins).
      * A postal city in the branch's old coverage but not in the new set → its
        mapping is removed.

    District/Province for each mapping are looked up from the master directory,
    falling back to the branch's own district/province.
    """
    desired = {pc.strip().title() for pc in postal_cities if pc and pc.strip()}

    directory: dict[str, PostalCityDirectory] = {}
    if desired:
        rows = await db.execute(
            select(PostalCityDirectory).where(
                PostalCityDirectory.postal_city.in_(desired)
            )
        )
        directory = {e.postal_city: e for e in rows.scalars().all()}

    existing_by_name: dict[str, PostalCityBranchMapping] = {}
    if desired:
        rows = await db.execute(
            select(PostalCityBranchMapping).where(
                PostalCityBranchMapping.postal_city.in_(desired)
            )
        )
        existing_by_name = {m.postal_city: m for m in rows.scalars().all()}

    owned_rows = await db.execute(
        select(PostalCityBranchMapping).where(
            PostalCityBranchMapping.branch_id == branch.branch_id
        )
    )
    owned = list(owned_rows.scalars().all())

    for name in desired:
        entry = directory.get(name)
        district = entry.district if entry else branch.district
        province = entry.province if entry else branch.province
        mapping = existing_by_name.get(name)
        if mapping is not None:
            mapping.branch_id = branch.branch_id
            mapping.branch_name = branch.name
            mapping.district = district
            mapping.province = province
            mapping.is_active = True
        else:
            db.add(
                PostalCityBranchMapping(
                    postal_city=name,
                    branch_id=branch.branch_id,
                    branch_name=branch.name,
                    district=district,
                    province=province,
                    is_active=True,
                )
            )

    for mapping in owned:
        if mapping.postal_city not in desired:
            await db.delete(mapping)

    await db.flush()
    logger.info(
        f"Synced coverage for branch {branch.branch_id}: "
        f"{len(desired)} postal city(ies)."
    )
