"""
Admin Coupons API (Promotions & Coupons Management)

CRUD over promotional coupon codes. Restricted to super_admin and
marketing_manager (enforced at the router level).

Branch scoping: a coupon belongs to one branch, or to the whole network when
``branch_id IS NULL``. A branch's Marketing Manager sees their own branch's
coupons plus the network-wide ones, but may only create or modify their own —
otherwise one branch's promotion would be redeemable against, and paid for by,
every other branch. Only a Super Admin publishes network-wide.

DELETE is a soft delete (sets is_active = False) so redemption history and any
orders referencing the code stay intact.

Prefix "/admin/coupons" is applied by app/api/v1/router.py.
"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import String, case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import BranchScope, inject_branch_filter, require_roles
from app.models.coupon import (
    Coupon,
    CouponCategory,
    CouponProduct,
    CouponWhitelistEntry,
)
from app.models.loyalty import LoyaltyTier
from app.models.order import Order, OrderStatus
from app.schemas.coupon import CouponCreate, CouponUpdate

router = APIRouter(
    dependencies=[Depends(require_roles("super_admin", "marketing_manager"))],
    tags=["Admin Coupons"],
)


# Orders in these states cost the branch nothing, so they contribute to
# neither the budget spent nor the revenue a campaign is credited with.
_VOID_STATUSES = (
    OrderStatus.CANCELLED.value,
    OrderStatus.REFUNDED.value,
    OrderStatus.RTO_INITIATED.value,
)


def _derive_status(c: Coupon, now: datetime) -> str:
    """
    The single status the admin table filters and badges on.

    Four mutually exclusive outcomes, ordered by what a manager needs to know
    first. The distinction that matters most is 'depleted' vs 'inactive':
    a depleted campaign worked so well it spent its allowance, an inactive one
    was switched off by a person. Collapsing them into "not running" loses the
    only signal that says "consider funding this again".
    """
    if c.is_budget_depleted:
        return "depleted"
    if not c.is_active:
        return "inactive"
    if c.valid_until is not None and _aware(c.valid_until) < now:
        return "expired"
    if c.valid_from is not None and _aware(c.valid_from) > now:
        return "scheduled"
    return "active"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _format_coupon(c: Coupon, performance: dict | None = None) -> dict:
    now = datetime.now(timezone.utc)
    budget_cap = float(c.budget_cap) if c.budget_cap is not None else None
    budget_spent = float(c.budget_spent or 0)
    perf = performance or {}
    return {
        "coupon_id": str(c.coupon_id),
        "code": c.code,
        "branch_id": str(c.branch_id) if c.branch_id else None,
        "branch_name": c.branch.name if getattr(c, "branch", None) else None,
        "is_network_wide": c.branch_id is None,
        # Derived, not stored: a coupon becomes expired or depleted by the
        # passage of time and the spending of money, not by anyone writing a
        # column. Storing it would mean a row that is wrong until a job runs.
        "status": _derive_status(c, now),
        "budget_cap": budget_cap,
        "budget_spent": budget_spent,
        "budget_used_percent": (
            round(budget_spent / budget_cap * 100, 1)
            if budget_cap and budget_cap > 0
            else None
        ),
        "auto_stop_on_budget": c.auto_stop_on_budget,
        "exclude_quick_sale": c.exclude_quick_sale,
        "first_order_only": c.first_order_only,
        "per_user_limit": c.per_user_limit,
        "is_public": c.is_public,
        "show_in_popup": bool(getattr(c, "show_in_popup", False)),
        "notify_customers": bool(getattr(c, "notify_customers", False)),
        # Customers the announcement push has gone to so far.
        "notified_count": perf.get("notified", 0),
        "popup_priority": getattr(c, "popup_priority", 0) or 0,
        "min_tier_id": str(c.min_tier_id) if getattr(c, "min_tier_id", None) else None,
        "target_customer_type": _target_type(c),
        "whitelisted_customers": [
            _whitelist_out(e) for e in (getattr(c, "whitelist", None) or [])
        ],
        "target_user_id": str(c.target_user_id) if c.target_user_id else None,
        "category_ids": [str(x.category_id) for x in (c.eligible_categories or [])],
        "product_ids": [str(x.product_id) for x in (c.eligible_products or [])],
        # Revenue the code actually brought in, and over how many orders. This
        # is the other half of the budget figure: Rs 30,000 of discount that
        # pulled in Rs 612,000 of sales is a success, and the spend column
        # alone cannot say so.
        "revenue_generated": perf.get("revenue", 0.0),
        "orders_count": perf.get("orders", 0),
        "description": c.description,
        "discount_type": c.discount_type,
        "discount_value": float(c.discount_value or 0),
        "min_order_value": float(c.min_order_value or 0),
        "max_discount_amount": float(c.max_discount_amount) if getattr(c, "max_discount_amount", None) is not None else None,
        "usage_limit": c.usage_limit,
        "used_count": c.used_count or 0,
        "valid_from": c.valid_from.isoformat() if c.valid_from else None,
        "valid_until": c.valid_until.isoformat() if c.valid_until else None,
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _whitelist_out(e: CouponWhitelistEntry) -> dict:
    """One listed customer as the admin chips show it."""
    from app.utils.identity import format_customer_code

    user = getattr(e, "user", None)
    return {
        "name": e.name or e.phone or (user.full_name if user else None) or "Customer",
        "phone": e.phone,
        "user_id": str(e.user_id) if e.user_id else None,
        "email": user.email if user else None,
        "customer_code": format_customer_code(user.customer_no) if user else None,
    }


async def _performance_map(db: AsyncSession, coupons: list[Coupon]) -> dict[str, dict]:
    """
    Revenue and order count per coupon code, in one grouped query.

    Aggregated from ``orders`` rather than kept as a counter because, unlike
    the budget, nothing at checkout has to test it under a lock — so the
    reconcilable source of truth can be read directly and there is no
    denormalised copy to drift.

    Cancelled, refunded and RTO'd orders are excluded: a campaign should not be
    credited with revenue that was handed back.
    """
    codes = [c.code for c in coupons if c.code]
    if not codes:
        return {}

    rows = (
        await db.execute(
            select(
                func.lower(Order.coupon_code).label("code"),
                func.coalesce(
                    func.sum(Order.subtotal - func.coalesce(Order.discount_amount, 0)),
                    0,
                ).label("revenue"),
                func.count(Order.order_id).label("orders"),
            )
            .where(
                func.lower(Order.coupon_code).in_([c.lower() for c in codes]),
                Order.status.notin_(_VOID_STATUSES),
            )
            .group_by(func.lower(Order.coupon_code))
        )
    ).all()
    perf = {
        r.code: {"revenue": float(r.revenue or 0), "orders": int(r.orders or 0)}
        for r in rows
    }
    # How many customers each coupon's announcement push has reached.
    from app.models.coupon import CouponAnnouncement

    by_id = {c.coupon_id: c.code.lower() for c in coupons if c.code}
    notified = (
        await db.execute(
            select(CouponAnnouncement.coupon_id, func.count())
            .where(CouponAnnouncement.coupon_id.in_(list(by_id)))
            .group_by(CouponAnnouncement.coupon_id)
        )
    ).all()
    for cid, n in notified:
        perf.setdefault(by_id[cid], {})["notified"] = int(n)
    return perf


async def _sync_eligibility(
    db: AsyncSession,
    coupon: Coupon,
    category_ids: list | None,
    product_ids: list | None,
) -> None:
    """
    Replace a coupon's catalog-eligibility rows.

    ``None`` means "not mentioned in this request" and leaves the set alone; an
    empty list means "no restriction", which is the only way to widen a coupon
    back to the whole catalog once it has been narrowed.
    """
    if category_ids is not None:
        await db.execute(
            delete(CouponCategory).where(CouponCategory.coupon_id == coupon.coupon_id)
        )
        for cid in category_ids:
            db.add(CouponCategory(coupon_id=coupon.coupon_id, category_id=cid))
    if product_ids is not None:
        await db.execute(
            delete(CouponProduct).where(CouponProduct.coupon_id == coupon.coupon_id)
        )
        for pid in product_ids:
            db.add(CouponProduct(coupon_id=coupon.coupon_id, product_id=pid))


def _visible_to(scope: BranchScope):
    """
    Rows a caller may *see*: their own branch's coupons plus the network-wide
    ones. An unscoped caller (Super Admin) sees everything.
    """
    if scope.is_unscoped:
        return None
    return or_(Coupon.branch_id == scope.branch_id, Coupon.branch_id.is_(None))


async def _get_or_404(
    db: AsyncSession, coupon_id: UUID, scope: BranchScope, for_write: bool = False
) -> Coupon:
    """
    Fetch a coupon the caller is allowed to act on.

    A scoped caller gets 404 (not 403) for another branch's coupon: the
    existence of a rival branch's promo code is itself information they have no
    business confirming. ``for_write`` additionally refuses the network-wide
    rows they can see but must not edit — that refusal *is* explained, since
    they can see the row and would otherwise be told it vanished.
    """
    stmt = select(Coupon).where(Coupon.coupon_id == coupon_id)
    visible = _visible_to(scope)
    if visible is not None:
        stmt = stmt.where(visible)

    coupon = (await db.execute(stmt)).scalar_one_or_none()
    if coupon is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Coupon not found")

    if for_write and not scope.is_unscoped and coupon.branch_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Network-wide coupons can only be modified by a Super Admin.",
        )
    return coupon


def _target_type(c: Coupon) -> str:
    """Who the coupon is for. A level on it makes it 'tier' whatever the
    column says -- the level is what checkout enforces."""
    if getattr(c, "min_tier_id", None) is not None:
        return "tier"
    return getattr(c, "target_customer_type", None) or "all"


async def _whitelist_rows(db: AsyncSession, customers: list) -> list[CouponWhitelistEntry]:
    """
    The listed customers as rows, keyed on the canonical mobile number.

    Every number is checked before anything is saved: one mistyped number in a
    CSV of two hundred should be named, not silently dropped -- a dropped
    customer is someone told their coupon does not work.
    """
    from app.models.user import User
    from app.utils.identity import normalize_mobile

    bad_numbers: list[str] = []
    bad_accounts: list[str] = []
    entries: list[CouponWhitelistEntry] = []

    # A CSV row may carry only an email -- how a Google customer with no
    # number is exported -- so an email with neither number nor account is
    # resolved to the live account that holds it.
    by_email_wanted = {
        c.email.strip().lower()
        for c in customers
        if c.email and c.email.strip() and not (c.phone or "").strip() and c.user_id is None
    }
    by_email: dict[str, object] = {}
    if by_email_wanted:
        by_email = {
            em.lower(): uid
            for em, uid in (
                await db.execute(
                    select(User.email, User.user_id).where(
                        func.lower(User.email).in_(list(by_email_wanted)),
                        User.deleted_at.is_(None),
                    )
                )
            ).all()
        }

    for c in customers:
        name = (c.name or "").strip()[:255] or None
        raw = (c.phone or "").strip()
        user_id = c.user_id
        phone = None
        if raw:
            try:
                phone = normalize_mobile(raw)
            except ValueError:
                bad_numbers.append(f"{c.name or '?'} ({c.phone})")
                continue
        elif user_id is None:
            email = (c.email or "").strip().lower()
            user_id = by_email.get(email) if email else None
            if user_id is None:
                bad_numbers.append(f"{c.name or c.email or '?'} (no number" + (
                    ", and no customer has that email)" if email else ")"
                ))
                continue
        entries.append(CouponWhitelistEntry(phone=phone, name=name, user_id=user_id))

    # An account picked from the search must still be a live customer. Its
    # own number is listed with it, so the row also matches by number, as a
    # typed one would.
    picked = {e.user_id for e in entries if e.user_id is not None}
    if picked:
        found = {
            uid: (phone, full_name)
            for uid, phone, full_name in (
                await db.execute(
                    select(User.user_id, User.phone, User.full_name).where(
                        User.user_id.in_(list(picked)),
                        User.deleted_at.is_(None),
                    )
                )
            ).all()
        }
        for e in entries:
            if e.user_id is None:
                continue
            if e.user_id not in found:
                bad_accounts.append(e.name or str(e.user_id))
                continue
            acct_phone, acct_name = found[e.user_id]
            if e.phone is None and acct_phone:
                try:
                    e.phone = normalize_mobile(acct_phone)
                except ValueError:
                    pass  # an odd stored number: list the account alone
            e.name = e.name or (acct_name or "").strip()[:255] or None

    if bad_numbers or bad_accounts:
        parts = []
        if bad_numbers:
            shown = ", ".join(bad_numbers[:10]) + (
                f" and {len(bad_numbers) - 10} more" if len(bad_numbers) > 10 else ""
            )
            parts.append(f"These are not Sri Lankan mobile numbers: {shown}")
        if bad_accounts:
            parts.append(
                "These customers' accounts no longer exist: " + ", ".join(bad_accounts[:10])
            )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=". ".join(parts))

    # One row per customer: the same number or the same account twice is one.
    rows: dict[str, CouponWhitelistEntry] = {}
    seen_users: set = set()
    for e in entries:
        if (e.phone and e.phone in rows) or (e.user_id and e.user_id in seen_users):
            continue
        rows[e.phone or f"user:{e.user_id}"] = e
        if e.user_id:
            seen_users.add(e.user_id)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add at least one customer to the whitelist.",
        )

    # Attach the account each typed number belongs to now, where there is
    # one -- looked up in every shape an account may have stored it in.
    missing = [r for r in rows.values() if r.user_id is None]
    if missing:
        shapes = {}
        for r in missing:
            local = r.phone[3:]  # +94771234567 -> 771234567
            for form in (r.phone, f"0{local}", f"94{local}", local):
                shapes[form] = r.phone
        found = (
            await db.execute(
                select(User.phone, User.user_id).where(
                    User.phone.in_(list(shapes)),
                    User.deleted_at.is_(None),
                )
            )
        ).all()
        by_phone = {shapes[phone]: uid for phone, uid in found}
        for r in missing:
            r.user_id = by_phone.get(r.phone)
    return list(rows.values())


async def _apply_audience(
    db: AsyncSession,
    coupon: Coupon,
    target: Optional[str],
    customers: Optional[list],
) -> None:
    """
    Set who the coupon is for. ``target`` None means "not in this request".

    Choosing 'specific' requires the list; choosing anything else empties it,
    so a coupon that is no longer for selected customers cannot keep a stale
    list that would come back if someone flipped it again.
    """
    if target is None:
        if customers is not None and coupon.target_customer_type == "specific":
            target = "specific"
        else:
            return
    if target == "tier" and coupon.min_tier_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose the loyalty level this coupon is for.",
        )
    if target != "tier":
        coupon.min_tier_id = None

    if target == "specific":
        if customers is None:
            listed = await db.scalar(
                select(func.count()).where(
                    CouponWhitelistEntry.coupon_id == coupon.coupon_id
                )
            )
            if not listed:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Add at least one customer to the whitelist.",
                )
        else:
            rows = await _whitelist_rows(db, customers)
            await db.execute(
                delete(CouponWhitelistEntry).where(
                    CouponWhitelistEntry.coupon_id == coupon.coupon_id
                )
            )
            for r in rows:
                r.coupon_id = coupon.coupon_id
                db.add(r)
    else:
        await db.execute(
            delete(CouponWhitelistEntry).where(
                CouponWhitelistEntry.coupon_id == coupon.coupon_id
            )
        )
    coupon.target_customer_type = target


def _check_notify(coupon: Coupon) -> None:
    """The announcement push carries the code, so a hidden code may only be
    sent to the customers it was made for (owner, 2026-11-13)."""
    from app.services.coupon_announce_worker import can_announce

    if coupon.notify_customers and not can_announce(coupon):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Notify customers works only for a Public coupon or a Specific "
                "Customers coupon: a hidden code is never pushed to everyone."
            ),
        )


async def _require_tier(db: AsyncSession, tier_id: Optional[UUID]) -> None:
    """A members-only coupon must name a level that exists."""
    if tier_id is not None and await db.get(LoyaltyTier, tier_id) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That loyalty level does not exist",
        )


async def _code_taken(db: AsyncSession, code: str, exclude_id: Optional[UUID] = None) -> bool:
    stmt = select(Coupon.coupon_id).where(func.lower(Coupon.code) == code.lower())
    if exclude_id is not None:
        stmt = stmt.where(Coupon.coupon_id != exclude_id)
    return (await db.execute(stmt)).scalar_one_or_none() is not None


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/customer-search", response_model=dict)
async def search_customers_for_whitelist(
    q: str = Query(..., min_length=1, max_length=60, description="What the admin has typed so far"),
    limit: int = Query(8, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
):
    """
    Type-ahead for the "Specific Customers" list: suggestions from the first
    character typed.

    Matches the start of any word of the name ("ni" finds Nimal Perera and
    Kasun Nirmal), the mobile number however it is typed (077..., 77...,
    +9477... or its last digits), the start of the email, the SB customer
    number, and the NIC. Every live customer can be found -- a coupon is often
    meant for someone who has not ordered from the branch yet (owner,
    2026-11-13) -- and the router already limits this to Super Admins and
    Marketing Managers. The NIC is searched, never sent back.
    """
    from app.models.user import User
    from app.utils.identity import format_customer_code

    term = " ".join(q.split())
    if not term:
        return {"success": True, "data": {"customers": []}}
    like = _like_escape(term)
    digits = "".join(ch for ch in term if ch.isdigit())
    # The local part of a mobile number, so 0771..., 771... and +94771...
    # all mean the same customer. Sri Lankan mobiles are 9 local digits.
    local = digits
    if local.startswith("94") and len(local) > 2:
        local = local[2:]
    elif local.startswith("0"):
        local = local[1:]
    all_digits = func.regexp_replace(func.coalesce(User.phone, ""), r"\D", "", "g")
    phone_digits = func.right(all_digits, 9)
    compact = "".join(ch for ch in term.upper() if ch not in " -+")
    looks_numeric = compact.isdigit()
    upper = term.upper().replace(" ", "")

    name_start = User.full_name.ilike(f"{like}%", escape="\\")
    name_word = User.full_name.ilike(f"% {like}%", escape="\\")
    conds = [name_start, name_word, User.email.ilike(f"{like}%", escape="\\")]
    phone_start = None
    if looks_numeric and local:
        phone_start = phone_digits.like(f"{local}%")
        conds.append(phone_start)
        if len(digits) >= 4:
            # The last digits, which is how people often read a number out.
            conds.append(all_digits.like(f"%{digits}"))
    # SB-12345: "SB-12", "sb12" or just the digits.
    code_digits = None
    if upper.startswith("SB"):
        code_digits = "".join(ch for ch in upper[2:] if ch.isdigit()) or None
        if code_digits is None and upper in ("SB", "SB-"):
            conds.append(User.customer_no.isnot(None))
    elif looks_numeric and len(digits) <= 5 and not term.startswith(("0", "+")):
        code_digits = digits
    if code_digits:
        conds.append(func.cast(User.customer_no, String).like(f"{code_digits}%"))
    # NIC: 9 digits + V/X (old) or 12 digits (new). Three characters before
    # it is worth matching, so short numbers do not flood the list.
    if len(compact) >= 3 and (compact.isdigit() or (compact[:-1].isdigit() and compact[-1] in "VX")):
        conds.append(func.upper(User.nic).like(f"{compact}%"))

    rank = case(
        (func.lower(User.full_name) == term.lower(), 0),
        *(((phone_start, 1),) if phone_start is not None else ()),
        (name_start, 2),
        (name_word, 3),
        else_=4,
    )
    rows = (
        await db.execute(
            select(
                User.user_id,
                User.full_name,
                User.phone,
                User.email,
                User.customer_no,
                User.auth_provider,
                User.is_blocked,
            )
            .where(User.deleted_at.is_(None), or_(*conds))
            .order_by(rank, User.full_name, User.created_at.desc())
            .limit(limit)
        )
    ).all()
    return {
        "success": True,
        "data": {
            "customers": [
                {
                    "user_id": str(r.user_id),
                    "name": (r.full_name or "").strip() or "Customer",
                    "phone": r.phone,
                    "email": r.email,
                    "customer_code": format_customer_code(r.customer_no),
                    "auth_provider": r.auth_provider,
                    "is_blocked": bool(r.is_blocked),
                }
                for r in rows
            ]
        },
    }


@router.get("", response_model=dict)
async def list_coupons(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None, description="Match code or description"),
    branch_id: Optional[str] = Query(None, description="Filter by branch (Super Admin only, or 'network' for network-wide)"),
    is_active: Optional[bool] = Query(None, description="Filter by the raw is_active flag"),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        pattern="^(active|scheduled|expired|inactive|depleted)$",
        description=(
            "Filter by derived lifecycle status. 'depleted' means the campaign "
            "budget is exhausted, which is distinct from someone switching it off."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Paginated coupon list.

    Branch-scoped: a Marketing Manager sees their own branch's coupons and the
    network-wide ones, never another branch's.
    """
    query = select(Coupon)
    visible = _visible_to(scope)
    if visible is not None:
        query = query.where(visible)
    elif branch_id:
        if branch_id == "network":
            query = query.where(Coupon.branch_id.is_(None))
        else:
            try:
                query = query.where(Coupon.branch_id == UUID(branch_id))
            except (ValueError, TypeError):
                pass
    if is_active is not None:
        query = query.where(Coupon.is_active == is_active)
    if search:
        pattern = f"%{search}%"
        query = query.where(
            or_(Coupon.code.ilike(pattern), Coupon.description.ilike(pattern))
        )

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0

    offset = (page - 1) * limit
    query = query.order_by(Coupon.created_at.desc()).limit(limit).offset(offset)
    coupons = (await db.execute(query)).scalars().all()

    # One grouped query for the whole page rather than one per row.
    perf = await _performance_map(db, list(coupons))
    formatted = [_format_coupon(c, perf.get(c.code.lower())) for c in coupons]

    # `status` is derived (see _derive_status), so it cannot be a SQL WHERE
    # clause without duplicating that logic in two languages. Filtering the
    # page in Python keeps one definition; the page size cap (100) bounds it.
    if status_filter:
        formatted = [row for row in formatted if row["status"] == status_filter]

    return {
        "success": True,
        "data": {
            "coupons": formatted,
            "pagination": {
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": (total + limit - 1) // limit if limit else 1,
            },
        },
    }


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_coupon(
    data: CouponCreate,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Create a coupon.

    A scoped creator's coupon is always pinned to their own branch — a
    ``branch_id`` in the body is ignored rather than trusted, mirroring how
    admin_banners.py pins a banner. Only a Super Admin may create a
    network-wide coupon, by omitting ``branch_id``.
    """
    if await _code_taken(db, data.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A coupon with this code already exists",
        )

    branch_id = data.branch_id if scope.is_unscoped else scope.branch_id
    await _require_tier(db, data.min_tier_id)

    coupon = Coupon(
        code=data.code,
        branch_id=branch_id,
        budget_cap=data.budget_cap,
        auto_stop_on_budget=data.auto_stop_on_budget,
        exclude_quick_sale=data.exclude_quick_sale,
        first_order_only=data.first_order_only,
        per_user_limit=data.per_user_limit,
        is_public=data.is_public,
        show_in_popup=data.show_in_popup,
        popup_priority=data.popup_priority,
        notify_customers=data.notify_customers,
        min_tier_id=data.min_tier_id,
        description=data.description,
        discount_type=data.discount_type.value,
        discount_value=data.discount_value,
        min_order_value=data.min_order_value,
        max_discount_amount=data.max_discount_amount,
        usage_limit=data.usage_limit,
        valid_from=data.valid_from,
        valid_until=data.valid_until,
        is_active=data.is_active,
    )
    db.add(coupon)
    await db.flush()
    await _sync_eligibility(db, coupon, data.category_ids, data.product_ids)
    # A level alone still means 'tier', as it did before the choice was stored.
    target = "tier" if data.min_tier_id is not None else data.target_customer_type
    await _apply_audience(db, coupon, target, data.whitelisted_customers)
    _check_notify(coupon)
    await db.commit()
    await db.refresh(coupon)
    logger.info(
        f"[admin] Coupon created: {coupon.code} "
        f"({'network-wide' if branch_id is None else f'branch {branch_id}'})"
    )
    return {"success": True, "data": _format_coupon(coupon), "message": "Coupon created successfully"}


@router.put("/{coupon_id}", response_model=dict)
async def update_coupon(
    coupon_id: UUID,
    data: CouponUpdate,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Edit a coupon. A scoped admin may only edit their own branch's."""
    coupon = await _get_or_404(db, coupon_id, scope, for_write=True)
    # branch_id is not in CouponUpdate at all, so a coupon can never change
    # owner through this route — there is no legitimate reason to hand a live
    # promo code to a different branch mid-campaign.
    fields = data.model_dump(exclude_unset=True)

    if "code" in fields and fields["code"]:
        if await _code_taken(db, fields["code"], exclude_id=coupon_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A coupon with this code already exists",
            )

    # Resolve the effective values for cross-field validation (a single-sided
    # date/type/value change must still be consistent with what's stored).
    new_from = fields.get("valid_from", coupon.valid_from)
    new_until = fields.get("valid_until", coupon.valid_until)
    if new_until <= new_from:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="valid_until must be after valid_from",
        )

    new_type = fields.get("discount_type")
    new_type_val = new_type.value if new_type is not None else coupon.discount_type
    new_value = fields.get("discount_value", coupon.discount_value)
    if new_value is not None and new_value <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="discount_value must be greater than 0",
        )
    if new_type_val == "percentage" and new_value is not None and new_value > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Percentage discount cannot exceed 100",
        )

    # Same rule CouponCreate enforces at creation time: a fixed discount can
    # never exceed the order it applies to. A partial update can change either
    # side without resending the other, so check the value the row *ends up*
    # with, not just what this request happened to include.
    new_min_order_value = fields.get("min_order_value", coupon.min_order_value)
    if new_type_val == "fixed" and new_value is not None and new_min_order_value < new_value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="min_order_value must be greater than or equal to discount_value for fixed discounts",
        )

    # The eligibility sets are join tables, not columns — pop them out before
    # the generic setattr loop so it does not try to assign a list to the ORM
    # relationship and silently detach the existing rows.
    category_ids = fields.pop("category_ids", None)
    product_ids = fields.pop("product_ids", None)
    # Applied after the plain columns, below, because it reads min_tier_id.
    target = fields.pop("target_customer_type", None)
    fields.pop("whitelisted_customers", None)
    # NOT NULL columns: an explicit null means "leave as is".
    for key in ("show_in_popup", "popup_priority", "notify_customers"):
        if key in fields and fields[key] is None:
            fields.pop(key)
    if fields.get("min_tier_id") is not None:
        await _require_tier(db, fields["min_tier_id"])

    for key, value in fields.items():
        if key == "discount_type" and value is not None:
            coupon.discount_type = value.value
        elif hasattr(coupon, key):
            setattr(coupon, key, value)

    await _sync_eligibility(db, coupon, category_ids, product_ids)
    if target is None and "min_tier_id" in fields:
        target = "tier" if coupon.min_tier_id is not None else None
    await _apply_audience(db, coupon, target, data.whitelisted_customers)
    _check_notify(coupon)

    await db.commit()
    await db.refresh(coupon)
    logger.info(f"[admin] Coupon updated: {coupon.coupon_id}")
    return {"success": True, "data": _format_coupon(coupon), "message": "Coupon updated successfully"}


@router.delete("/{coupon_id}", response_model=dict)
async def deactivate_or_delete_coupon(
    coupon_id: UUID,
    hard: bool = Query(False, description="Hard delete from database"),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Deactivate (soft delete) or hard delete if hard=True."""
    coupon = await _get_or_404(db, coupon_id, scope, for_write=True)
    if hard:
        try:
            await db.delete(coupon)
            await db.commit()
            logger.info(f"[admin] Coupon hard deleted: {coupon_id}")
            return {"success": True, "message": "Coupon deleted permanently"}
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot permanently delete coupon as it has been redeemed in past orders. Deactivate it instead.",
            )
    else:
        coupon.is_active = False
        await db.commit()
        logger.info(f"[admin] Coupon deactivated: {coupon_id}")
        return {"success": True, "message": "Coupon deactivated successfully"}
