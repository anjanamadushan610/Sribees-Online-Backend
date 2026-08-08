"""
Referral Service — codes, binding, commission accrual and payout.

The money path, in order:

  1. **Bind.**   A new user redeems someone's code. Written once, never changed.
  2. **Accrue.** Their order is placed -> a ``pending`` commission row appears.
                 Nothing is owed yet; the order can still be cancelled.
  3. **Vest.**   The order is delivered -> the row becomes ``claimable``.
  4. **Pay.**    The referrer claims -> the balance moves into their wallet.
  5. **Reverse.** Cancelled or refunded -> the row is voided, and if it was
                 already paid the wallet is debited back.

Every step is written to be safe to run twice. Order status transitions get
retried, webhooks get replayed, and users double-tap buttons; none of that may
produce a second payout.
"""
import secrets
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.order import Order
from app.models.partner import Partner
from app.models.referral import ReferralCommission, ReferralCommissionStatus
from app.models.user import User
from app.models.wallet import WalletTransactionType
from app.services.wallet_service import WalletService

# Deliberately excludes 0/O/1/I/L — codes get read aloud and typed by hand, and
# a code that cannot be transcribed is a support ticket.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _money(value: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class ReferralError(ValueError):
    """A referral action the caller is not allowed to take."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ReferralService:
    """Referral codes, bindings and the commission ledger."""

    # ------------------------------------------------------------------
    # Codes
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_code() -> str:
        """
        A random, non-sequential invite code.

        Random rather than derived from the user id: a code that encodes who
        it belongs to leaks user ordering and lets someone walk the space.
        """
        return "".join(
            secrets.choice(_CODE_ALPHABET)
            for _ in range(settings.referral_code_length)
        )

    @staticmethod
    async def get_or_create_code(db: AsyncSession, user: User) -> str:
        """
        Return this user's invite code, generating it on first request.

        Generated lazily so users who never open the invite screen do not get
        a code, and collisions are retried rather than assumed away.
        """
        if user.referral_code:
            return user.referral_code

        for _ in range(5):
            candidate = ReferralService._generate_code()
            exists = await db.execute(
                select(User.user_id).where(User.referral_code == candidate)
            )
            if exists.scalar_one_or_none() is not None:
                continue

            user.referral_code = candidate
            try:
                await db.commit()
            except IntegrityError:
                # Lost a race against a concurrent generation; try again.
                await db.rollback()
                await db.refresh(user)
                if user.referral_code:
                    return user.referral_code
                continue
            await db.refresh(user)
            return candidate

        raise ReferralError(
            "CODE_GENERATION_FAILED",
            "Could not allocate an invite code. Please try again.",
        )

    # ------------------------------------------------------------------
    # Binding
    # ------------------------------------------------------------------

    @staticmethod
    async def bind_referrer(
        db: AsyncSession,
        user: User,
        code: str,
        *,
        commit: bool = True,
    ) -> User:
        """
        Attach *user* to the owner of *code*. Write-once.

        Refuses, in order: an empty code, an unknown code, the user's own code,
        and any user who already has a referrer. The last one is what keeps the
        ledger auditable — a binding that can move invalidates every commission
        already recorded against it.
        """
        cleaned = (code or "").strip().upper()
        if not cleaned:
            raise ReferralError("INVALID_CODE", "Enter an invite code.")

        if user.referred_by_user_id is not None or user.referred_by_partner_id is not None:
            raise ReferralError(
                "ALREADY_REFERRED",
                "This account already used an invite code.",
            )

        # Partners first: a professional agent's code and a customer's invite
        # code look identical to whoever types them, so one field accepts both
        # and the server decides which ledger the commission belongs to.
        partner_row = await db.execute(
            select(Partner).where(
                Partner.referral_code == cleaned,
                Partner.is_active.is_(True),
            )
        )
        partner = partner_row.scalar_one_or_none()

        if partner is not None:
            user.referred_by_partner_id = partner.partner_id
            user.referred_at = datetime.now(timezone.utc)
            if commit:
                await db.commit()
                await db.refresh(user)
            logger.info(
                f"Referral bound: user={user.user_id} partner={partner.partner_id}"
            )
            return user

        result = await db.execute(
            select(User).where(User.referral_code == cleaned)
        )
        referrer = result.scalar_one_or_none()

        if referrer is None:
            # Same message whether the code is wrong or simply does not exist —
            # a distinct "no such code" reply turns this endpoint into an
            # oracle for enumerating valid codes.
            raise ReferralError("INVALID_CODE", "That invite code is not valid.")

        if referrer.user_id == user.user_id:
            raise ReferralError(
                "SELF_REFERRAL", "You cannot use your own invite code."
            )

        user.referred_by_user_id = referrer.user_id
        user.referred_at = datetime.now(timezone.utc)

        if commit:
            await db.commit()
            await db.refresh(user)

        logger.info(
            f"Referral bound: user={user.user_id} referred_by={referrer.user_id}"
        )
        return user

    # ------------------------------------------------------------------
    # Accrual
    # ------------------------------------------------------------------

    @staticmethod
    def _commission_amount(basis: Decimal, rate: float) -> Decimal:
        return _money(basis * Decimal(str(rate)) / Decimal("100"))

    @staticmethod
    async def _add_commission(
        db: AsyncSession,
        *,
        order: Order,
        source_user_id: UUID,
        level: int,
        rate: float,
        basis: Decimal,
        beneficiary_user_id: Optional[UUID] = None,
        beneficiary_partner_id: Optional[UUID] = None,
    ) -> Optional[ReferralCommission]:
        """
        Write one commission row, or nothing if it already exists.

        Uses a SAVEPOINT rather than a bare flush: this runs inside the
        caller's order-creation transaction, and letting a duplicate-key error
        roll that back would discard the order itself over a commission we did
        not need to write.
        """
        amount = ReferralService._commission_amount(basis, rate)
        if amount <= 0:
            return None

        commission = ReferralCommission(
            order_id=order.order_id,
            order_number=order.order_number,
            beneficiary_user_id=beneficiary_user_id,
            beneficiary_partner_id=beneficiary_partner_id,
            source_user_id=source_user_id,
            level=level,
            rate_percentage=rate,
            order_subtotal=basis,
            amount=amount,
            status=ReferralCommissionStatus.PENDING,
        )

        try:
            async with db.begin_nested():
                db.add(commission)
                await db.flush()
        except IntegrityError:
            # Already accrued for this (order, beneficiary, level) — a retried
            # transition, not a bug.
            logger.info(
                f"Referral commission L{level} already exists for order "
                f"{order.order_number}"
            )
            return None

        logger.info(
            f"Referral commission L{level} accrued: {amount} on order "
            f"{order.order_number}"
        )
        return commission

    @staticmethod
    async def accrue_for_order(
        db: AsyncSession,
        order: Order,
        *,
        commit: bool = False,
    ) -> list[ReferralCommission]:
        """
        Record what an order owes, to whoever referred the buyer.

        Two shapes:

        * **Referred by another customer** — one level-1 row to that customer.
        * **Referred by a professional partner** — level 1 to that partner and,
          if the partner was itself recruited, level 2 to their upline. Only
          two levels: the depth is fixed here rather than walked, because an
          unbounded chain is a pyramid, not a referral programme.

        No-ops when the buyer has no referrer or the basis is zero. Safe to run
        twice; the partial unique indexes are the authority.
        """
        buyer = await db.get(User, order.user_id)
        if buyer is None:
            return []

        basis = Decimal(str(order.subtotal or 0))
        if basis <= 0:
            return []

        rate = float(settings.referral_commission_percent)
        created: list[ReferralCommission] = []

        # --- Referred by a professional partner ---------------------------
        if buyer.referred_by_partner_id is not None:
            row = await ReferralService._add_commission(
                db,
                order=order,
                source_user_id=buyer.user_id,
                level=1,
                rate=rate,
                basis=basis,
                beneficiary_partner_id=buyer.referred_by_partner_id,
            )
            if row is not None:
                created.append(row)

            partner = await db.get(Partner, buyer.referred_by_partner_id)
            upline_id = partner.parent_partner_id if partner else None
            if upline_id is not None and upline_id != buyer.referred_by_partner_id:
                row2 = await ReferralService._add_commission(
                    db,
                    order=order,
                    source_user_id=buyer.user_id,
                    level=2,
                    rate=float(settings.referral_level2_percent),
                    basis=basis,
                    beneficiary_partner_id=upline_id,
                )
                if row2 is not None:
                    created.append(row2)

        # --- Referred by another customer ---------------------------------
        elif buyer.referred_by_user_id is not None:
            if buyer.referred_by_user_id == buyer.user_id:
                # Should be impossible (DB check constraint), but paying it
                # would be worse than logging it.
                logger.warning(
                    f"Self-referral on order {order.order_number}; skipped"
                )
            else:
                row = await ReferralService._add_commission(
                    db,
                    order=order,
                    source_user_id=buyer.user_id,
                    level=1,
                    rate=rate,
                    basis=basis,
                    beneficiary_user_id=buyer.referred_by_user_id,
                )
                if row is not None:
                    created.append(row)

        if created and commit:
            await db.commit()
        return created

    # ------------------------------------------------------------------
    # Vesting and reversal
    # ------------------------------------------------------------------

    @staticmethod
    async def mark_claimable_for_order(
        db: AsyncSession,
        order_id: UUID,
        *,
        commit: bool = False,
    ) -> int:
        """
        Vest an order's commissions once it is delivered.

        Only ``pending`` rows move. A row that was already reversed stays
        reversed — a delivered-after-refund sequence must not resurrect money.
        """
        result = await db.execute(
            select(ReferralCommission).where(
                ReferralCommission.order_id == order_id,
                ReferralCommission.status == ReferralCommissionStatus.PENDING,
            )
        )
        rows = result.scalars().all()
        for row in rows:
            row.status = ReferralCommissionStatus.CLAIMABLE

        if rows and commit:
            await db.commit()
        if rows:
            logger.info(f"{len(rows)} referral commission(s) vested for order {order_id}")
        return len(rows)

    @staticmethod
    async def reverse_for_order(
        db: AsyncSession,
        order_id: UUID,
        *,
        reason: str = "Order cancelled",
        commit: bool = False,
    ) -> int:
        """
        Void an order's commissions on cancellation or refund.

        A commission that was already **paid** is clawed back out of the
        referrer's wallet. That can push the balance negative, and it is meant
        to: the alternative is letting someone order, claim, and cancel on a
        loop. The wallet ledger records the reversal either way.
        """
        result = await db.execute(
            select(ReferralCommission).where(
                ReferralCommission.order_id == order_id,
                ReferralCommission.status.in_(
                    [
                        ReferralCommissionStatus.PENDING,
                        ReferralCommissionStatus.CLAIMABLE,
                        ReferralCommissionStatus.PAID,
                    ]
                ),
            )
        )
        rows = result.scalars().all()

        for row in rows:
            if row.status == ReferralCommissionStatus.PAID:
                await WalletService.apply_transaction(
                    db,
                    user_id=row.beneficiary_user_id,
                    tx_type=WalletTransactionType.SPENT,
                    amount=Decimal(str(row.amount)),
                    title="Referral commission reversed",
                    order_id=row.order_id,
                    order_number=row.order_number,
                    notes=reason,
                )
                logger.info(
                    f"Clawed back paid referral commission {row.amount} "
                    f"from {row.beneficiary_user_id} ({reason})"
                )
            row.status = ReferralCommissionStatus.REVERSED

        if rows and commit:
            await db.commit()
        return len(rows)

    # ------------------------------------------------------------------
    # Payout
    # ------------------------------------------------------------------

    @staticmethod
    async def claim(db: AsyncSession, user_id: UUID) -> dict:
        """
        Move every claimable commission into the user's wallet.

        The rows are locked with ``FOR UPDATE SKIP LOCKED`` before anything is
        credited, so two simultaneous taps cannot both read the same row as
        claimable and pay it twice. Whichever request gets the lock pays; the
        other sees nothing left to claim.
        """
        result = await db.execute(
            select(ReferralCommission)
            .where(
                ReferralCommission.beneficiary_user_id == user_id,
                ReferralCommission.status == ReferralCommissionStatus.CLAIMABLE,
            )
            .with_for_update(skip_locked=True)
        )
        rows = result.scalars().all()

        if not rows:
            return {"claimed_count": 0, "amount": Decimal("0.00")}

        total = _money(sum(Decimal(str(r.amount)) for r in rows))
        now = datetime.now(timezone.utc)

        transaction = await WalletService.apply_transaction(
            db,
            user_id=user_id,
            tx_type=WalletTransactionType.EARNED,
            amount=total,
            title="Referral commission",
            notes=f"{len(rows)} order(s)",
        )

        for row in rows:
            row.status = ReferralCommissionStatus.PAID
            row.claimed_at = now
            row.wallet_transaction_id = transaction.transaction_id

        await db.commit()
        logger.info(f"Referral payout {total} to {user_id} over {len(rows)} row(s)")

        return {"claimed_count": len(rows), "amount": total}

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @staticmethod
    async def get_summary(db: AsyncSession, user_id: UUID) -> dict:
        """Totals by status for the invite screen's header."""
        result = await db.execute(
            select(
                ReferralCommission.status,
                func.coalesce(func.sum(ReferralCommission.amount), 0),
            )
            .where(ReferralCommission.beneficiary_user_id == user_id)
            .group_by(ReferralCommission.status)
        )
        totals = {status: Decimal("0.00") for status in ReferralCommissionStatus.ALL}
        for status, amount in result.all():
            totals[status] = _money(Decimal(str(amount)))
        return totals

    @staticmethod
    async def list_friends(db: AsyncSession, user_id: UUID) -> list[dict]:
        """
        The people this user invited, with what each has earned them.

        Returns the friend's name and a short public id only. Their phone,
        email and order values are deliberately absent — the referrer is shown
        enough to recognise who this is and to check their own commission, and
        nothing about what the friend actually buys.
        """
        earned = (
            select(
                ReferralCommission.source_user_id.label("uid"),
                func.coalesce(func.sum(ReferralCommission.amount), 0).label("total"),
                func.count(ReferralCommission.commission_id).label("orders"),
            )
            .where(
                ReferralCommission.beneficiary_user_id == user_id,
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
            .where(User.referred_by_user_id == user_id)
            .order_by(User.referred_at.desc().nullslast())
        )

        return [
            {
                "user_id": str(uid),
                "name": name or "Friend",
                # Short, non-reversible handle so the referrer can tell two
                # friends with the same name apart without us exposing a phone
                # number or email.
                "member_id": str(uid).replace("-", "")[:6].upper(),
                "joined_at": joined.isoformat() if joined else None,
                "orders": int(orders or 0),
                "total_earned": float(_money(Decimal(str(total or 0)))),
            }
            for uid, name, joined, total, orders in result.all()
        ]

    @staticmethod
    async def list_commissions_from_friend(
        db: AsyncSession,
        user_id: UUID,
        friend_user_id: UUID,
    ) -> list[dict]:
        """
        Per-order commissions this user earned from one friend.

        Scoped by ``beneficiary_user_id == user_id`` in the query itself, so a
        caller cannot read someone else's earnings by supplying another
        friend's id.

        ``order_subtotal`` is not returned. The referrer sees the date and what
        they earned — not what their friend spent.
        """
        result = await db.execute(
            select(ReferralCommission)
            .where(
                ReferralCommission.beneficiary_user_id == user_id,
                ReferralCommission.source_user_id == friend_user_id,
            )
            .order_by(ReferralCommission.created_at.desc())
        )

        return [
            {
                "commission_id": str(row.commission_id),
                "order_number": row.order_number,
                "date": row.created_at.isoformat() if row.created_at else None,
                "amount": float(_money(Decimal(str(row.amount)))),
                "rate_percentage": row.rate_percentage,
                "status": row.status,
                "level": row.level,
            }
            for row in result.scalars().all()
        ]
