"""
Partner Service — the professional referral team.

Partners live in their own table and authenticate through their own endpoint.
Nothing here touches ``admins``: an agent's credentials must be meaningless to
the admin surface, and an admin's meaningless here.

Commission attribution, both levels:

    Partner A recruits Partner B      (B.parent_partner_id = A)
    B signs a customer up             (users.referred_by_partner_id = B)
    That customer orders              -> B level 1, A level 2

Rates come from settings and are snapshotted onto each commission row by
``ReferralService``, so changing them never rewrites history.
"""
import secrets
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.core.security import hash_password, verify_password
from app.models.partner import Partner
from app.models.referral import ReferralCommission, ReferralCommissionStatus
from app.models.user import User


class PartnerEmailTakenError(ValueError):
    """Raised by ``create`` when the email is already registered to a partner."""

# Same alphabet as customer codes: no 0/O/1/I/L, because codes get read aloud.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# A real hash to compare against when the email does not exist, so a missing
# account does not answer measurably faster than a wrong password. Computed
# once at import; the value it encodes is irrelevant and never matches.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def _money(value: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class PartnerService:
    """Authentication and reporting for professional referral partners."""

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @staticmethod
    async def authenticate(
        db: AsyncSession, email: str, password: str
    ) -> Optional[Partner]:
        """
        Verify partner credentials.

        Returns None for every failure mode — unknown email, wrong password,
        deactivated account — so the caller cannot tell them apart and the
        endpoint does not become an account-enumeration oracle.
        """
        result = await db.execute(
            select(Partner).where(func.lower(Partner.email) == email.strip().lower())
        )
        partner = result.scalar_one_or_none()

        if partner is None or not partner.is_active:
            verify_password(password, _DUMMY_HASH)
            return None

        if not verify_password(password, partner.password_hash):
            return None

        partner.last_login = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(partner)
        return partner

    # ------------------------------------------------------------------
    # Codes
    # ------------------------------------------------------------------

    @staticmethod
    async def get_or_create_code(db: AsyncSession, partner: Partner) -> str:
        """Return this partner's code, minting it on first request."""
        if partner.referral_code:
            return partner.referral_code

        for _ in range(5):
            candidate = "".join(
                secrets.choice(_CODE_ALPHABET)
                for _ in range(settings.referral_code_length)
            )
            clash = await db.execute(
                select(Partner.partner_id).where(Partner.referral_code == candidate)
            )
            if clash.scalar_one_or_none() is not None:
                continue

            partner.referral_code = candidate
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                await db.refresh(partner)
                if partner.referral_code:
                    return partner.referral_code
                continue
            await db.refresh(partner)
            return candidate

        raise RuntimeError("Could not allocate a partner referral code")

    # ------------------------------------------------------------------
    # Administration
    # ------------------------------------------------------------------

    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        email: str,
        password: str,
        full_name: str,
        phone: Optional[str] = None,
        parent_partner_id: Optional[UUID] = None,
    ) -> Partner:
        """
        Create a partner account.

        Called from two places, distinguished only by ``parent_partner_id``:
        the admin panel (always ``None`` — admin-created partners are
        top-level, matching the seed data's "lead" shape) and the partner
        portal's recruit endpoint (always the recruiting partner's id). This
        is the only place a partner row is ever inserted, so it is also the
        only place ``parent_partner_id`` is ever set — the column is never
        touched by ``set_fields``, which is what makes it write-once in
        practice rather than just in a comment.
        """
        existing = await db.execute(
            select(Partner.partner_id).where(
                func.lower(Partner.email) == email.strip().lower()
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise PartnerEmailTakenError("A partner with this email already exists")

        partner = Partner(
            partner_id=uuid4(),
            email=email.strip().lower(),
            password_hash=hash_password(password),
            full_name=full_name.strip(),
            phone=phone.strip() if phone else None,
            parent_partner_id=parent_partner_id,
            is_active=True,
        )
        db.add(partner)
        await db.commit()
        await db.refresh(partner)
        logger.info(
            f"Partner created: {partner.email} "
            f"(parent={parent_partner_id or 'none — top level'})"
        )
        return partner

    @staticmethod
    async def list_all(db: AsyncSession) -> list[dict]:
        """
        Every partner for the admin panel, with who recruited them and their
        claimable/paid totals.

        Two queries — partners, then a grouped commission aggregate — rather
        than one summary lookup per row.
        """
        partners = (
            (await db.execute(select(Partner).order_by(Partner.created_at.desc())))
            .scalars()
            .all()
        )

        totals_rows = await db.execute(
            select(
                ReferralCommission.beneficiary_partner_id,
                ReferralCommission.status,
                func.coalesce(func.sum(ReferralCommission.amount), 0),
            )
            .where(ReferralCommission.beneficiary_partner_id.isnot(None))
            .group_by(
                ReferralCommission.beneficiary_partner_id, ReferralCommission.status
            )
        )
        totals: dict[UUID, dict[str, Decimal]] = {}
        for partner_id, comm_status, amount in totals_rows.all():
            bucket = totals.setdefault(
                partner_id, {"claimable": Decimal("0.00"), "paid": Decimal("0.00")}
            )
            if comm_status in bucket:
                bucket[comm_status] += _money(Decimal(str(amount)))

        names = {p.partner_id: p.full_name for p in partners}

        return [
            {
                "partner_id": str(p.partner_id),
                "email": p.email,
                "full_name": p.full_name,
                "phone": p.phone,
                "referral_code": p.referral_code,
                "parent_partner_id": (
                    str(p.parent_partner_id) if p.parent_partner_id else None
                ),
                "recruited_by": names.get(p.parent_partner_id),
                "is_active": p.is_active,
                "last_login": p.last_login.isoformat() if p.last_login else None,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "claimable_total": float(
                    totals.get(p.partner_id, {}).get("claimable", Decimal("0.00"))
                ),
                "paid_total": float(
                    totals.get(p.partner_id, {}).get("paid", Decimal("0.00"))
                ),
            }
            for p in partners
        ]

    @staticmethod
    async def set_fields(db: AsyncSession, partner: Partner, **fields) -> Partner:
        """
        Admin edit: name, phone, password, active status.

        Deliberately has no ``parent_partner_id`` parameter — that column is
        only ever set once, by ``create``.
        """
        if fields.get("full_name"):
            partner.full_name = fields["full_name"].strip()
        if "phone" in fields:
            partner.phone = fields["phone"].strip() if fields["phone"] else None
        if fields.get("password"):
            partner.password_hash = hash_password(fields["password"])
        if "is_active" in fields and fields["is_active"] is not None:
            partner.is_active = fields["is_active"]

        await db.commit()
        await db.refresh(partner)
        return partner

    # ------------------------------------------------------------------
    # Payout
    # ------------------------------------------------------------------

    @staticmethod
    async def mark_commissions_paid(
        db: AsyncSession,
        partner_id: UUID,
        commission_ids: Optional[list[UUID]] = None,
    ) -> dict:
        """
        Record that finance has settled this partner's claimable commissions
        out-of-band (bank transfer, etc).

        Partners have no wallet, so there is nothing to credit — this only
        flips ``status`` and timestamps the settlement, so the ledger stops
        showing money as perpetually "Ready" once it has actually been paid.
        Locked with ``FOR UPDATE SKIP LOCKED`` the same way
        ``ReferralService.claim`` locks a customer's rows, so two admins
        marking the same partner paid at once cannot double-count.
        """
        stmt = select(ReferralCommission).where(
            ReferralCommission.beneficiary_partner_id == partner_id,
            ReferralCommission.status == ReferralCommissionStatus.CLAIMABLE,
        )
        if commission_ids:
            stmt = stmt.where(ReferralCommission.commission_id.in_(commission_ids))
        stmt = stmt.with_for_update(skip_locked=True)

        rows = (await db.execute(stmt)).scalars().all()
        if not rows:
            return {"count": 0, "amount": Decimal("0.00")}

        total = _money(sum(Decimal(str(r.amount)) for r in rows))
        now = datetime.now(timezone.utc)
        for row in rows:
            row.status = ReferralCommissionStatus.PAID
            row.claimed_at = now

        await db.commit()
        logger.info(
            f"Partner payout marked paid: {total} over {len(rows)} row(s), "
            f"partner {partner_id}"
        )
        return {"count": len(rows), "amount": total}

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    @staticmethod
    async def get_summary(db: AsyncSession, partner_id: UUID) -> dict:
        """Commission totals by status, split by level."""
        result = await db.execute(
            select(
                ReferralCommission.status,
                ReferralCommission.level,
                func.coalesce(func.sum(ReferralCommission.amount), 0),
            )
            .where(ReferralCommission.beneficiary_partner_id == partner_id)
            .group_by(ReferralCommission.status, ReferralCommission.level)
        )

        totals = {s: Decimal("0.00") for s in ReferralCommissionStatus.ALL}
        by_level = {1: Decimal("0.00"), 2: Decimal("0.00")}
        for status, level, amount in result.all():
            value = _money(Decimal(str(amount)))
            totals[status] = totals.get(status, Decimal("0.00")) + value
            if level in by_level:
                by_level[level] += value

        return {"totals": totals, "by_level": by_level}

    @staticmethod
    async def list_customers(db: AsyncSession, partner_id: UUID) -> list[dict]:
        """
        Customers this partner signed up, with what each has earned them.

        Name and a short member id only — a partner is an outside contractor,
        and there is no reason for them to hold their customers' phone numbers
        or see what those customers actually buy.
        """
        earned = (
            select(
                ReferralCommission.source_user_id.label("uid"),
                func.coalesce(func.sum(ReferralCommission.amount), 0).label("total"),
                func.count(ReferralCommission.commission_id).label("orders"),
            )
            .where(
                ReferralCommission.beneficiary_partner_id == partner_id,
                ReferralCommission.level == 1,
                ReferralCommission.status != ReferralCommissionStatus.REVERSED,
            )
            .group_by(ReferralCommission.source_user_id)
            .subquery()
        )

        result = await db.execute(
            select(
                User.user_id,
                User.full_name,
                User.referred_at,
                func.coalesce(earned.c.total, 0),
                func.coalesce(earned.c.orders, 0),
            )
            .outerjoin(earned, earned.c.uid == User.user_id)
            .where(User.referred_by_partner_id == partner_id)
            .order_by(User.referred_at.desc().nullslast())
        )

        return [
            {
                "user_id": str(uid),
                "name": name or "Customer",
                "member_id": str(uid).replace("-", "")[:6].upper(),
                "joined_at": joined.isoformat() if joined else None,
                "orders": int(orders or 0),
                "total_earned": float(_money(Decimal(str(total or 0)))),
            }
            for uid, name, joined, total, orders in result.all()
        ]

    @staticmethod
    async def list_recruits(db: AsyncSession, partner_id: UUID) -> list[dict]:
        """
        Sub-partners this partner recruited, and the level-2 commission each
        has generated.
        """
        earned = (
            select(
                ReferralCommission.source_user_id.label("uid"),
                func.coalesce(func.sum(ReferralCommission.amount), 0).label("total"),
            )
            .where(
                ReferralCommission.beneficiary_partner_id == partner_id,
                ReferralCommission.level == 2,
                ReferralCommission.status != ReferralCommissionStatus.REVERSED,
            )
            .group_by(ReferralCommission.source_user_id)
            .subquery()
        )

        # Level-2 rows are keyed by the *customer* who ordered, so roll them up
        # through that customer's partner to get a per-recruit total.
        totals = await db.execute(
            select(
                User.referred_by_partner_id,
                func.coalesce(func.sum(earned.c.total), 0),
            )
            .join(earned, earned.c.uid == User.user_id)
            .group_by(User.referred_by_partner_id)
        )
        per_recruit = {row[0]: Decimal(str(row[1] or 0)) for row in totals.all()}

        result = await db.execute(
            select(Partner)
            .where(Partner.parent_partner_id == partner_id)
            .order_by(Partner.created_at.desc())
        )

        return [
            {
                "partner_id": str(p.partner_id),
                "name": p.full_name,
                "joined_at": p.created_at.isoformat() if p.created_at else None,
                "is_active": p.is_active,
                "total_earned": float(
                    _money(per_recruit.get(p.partner_id, Decimal("0")))
                ),
            }
            for p in result.scalars().all()
        ]

    @staticmethod
    async def list_commissions(
        db: AsyncSession, partner_id: UUID, limit: int = 100
    ) -> list[dict]:
        """
        The partner's commission ledger.

        Scoped by ``beneficiary_partner_id`` in the query itself, so no id from
        the client can widen it. ``order_subtotal`` is not selected — a partner
        sees what they earned, not what the customer spent.
        """
        result = await db.execute(
            select(ReferralCommission)
            .where(ReferralCommission.beneficiary_partner_id == partner_id)
            .order_by(ReferralCommission.created_at.desc())
            .limit(limit)
        )

        return [
            {
                "commission_id": str(row.commission_id),
                "order_number": row.order_number,
                "date": row.created_at.isoformat() if row.created_at else None,
                "amount": float(_money(Decimal(str(row.amount)))),
                "rate_percentage": row.rate_percentage,
                "level": row.level,
                "status": row.status,
            }
            for row in result.scalars().all()
        ]
