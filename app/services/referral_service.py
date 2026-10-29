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
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config.settings import settings
from app.models.order import Order, OrderItem
from app.models.partner import Partner
from app.models.referral import (
    ReferralCommission,
    ReferralCommissionLine,
    ReferralCommissionStatus,
)
from app.models.user import User
from app.models.wallet import WalletTransactionType
from app.services.finance_outbox import WalletReason
from app.services.referral_commission import (
    CommissionLine,
    LevelPolicy,
    ReferralProgramConfig,
    SaleLine,
    build_lines,
    kept_amount,
)
from app.services.referral_commission.presenter import line_payload
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
            )
        )
        partner = partner_row.scalar_one_or_none()

        if partner is not None:
            if not partner.is_active:
                raise ReferralError("INVALID_CODE", "That invite code is not valid.")
            if partner.user_id == user.user_id:
                raise ReferralError("SELF_REFERRAL", "You cannot use your own invite code.")
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
        lines: list[CommissionLine],
        beneficiary_user_id: Optional[UUID] = None,
        beneficiary_partner_id: Optional[UUID] = None,
    ) -> Optional[ReferralCommission]:
        """
        Write one commission row and its lines, or nothing if it already exists.

        The header is the sum of its lines -- ``amount`` and ``order_subtotal``
        alike -- so the ledger and the per-product breakdown cannot disagree.
        ``rate_percentage`` keeps the blended rate for readers that predate the
        lines; the lines carry the real ones.

        Uses a SAVEPOINT rather than a bare flush: this runs inside the
        caller's order-creation transaction, and letting a duplicate-key error
        roll that back would discard the order itself over a commission we did
        not need to write.
        """
        amount = _money(sum((line.amount for line in lines), Decimal("0")))
        if amount <= 0:
            return None
        basis = _money(sum((line.basis for line in lines), Decimal("0")))
        blended = float((amount * 100 / basis).quantize(Decimal("0.001"))) if basis > 0 else 0.0

        commission = ReferralCommission(
            order_id=order.order_id,
            order_number=order.order_number,
            beneficiary_user_id=beneficiary_user_id,
            beneficiary_partner_id=beneficiary_partner_id,
            source_user_id=source_user_id,
            level=level,
            rate_percentage=blended,
            order_subtotal=basis,
            amount=amount,
            status=ReferralCommissionStatus.PENDING,
            lines=[
                ReferralCommissionLine(
                    order_item_id=line.order_item_id,
                    product_id=line.product_id,
                    product_name=line.product_name,
                    quantity=line.quantity,
                    basis=line.basis,
                    rate_percentage=line.rate,
                    amount=line.amount,
                )
                for line in lines
            ],
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
            f"Referral commission L{level} accrued: {amount} over {len(lines)} "
            f"line(s) on order {order.order_number}"
        )
        return commission

    @staticmethod
    async def _sale_lines(db: AsyncSession, order: Order) -> list[SaleLine]:
        """
        The order's lines with the rate and coupon share frozen at checkout.

        A line without a rate snapshot earns nothing: since 081 checkout stamps
        every line, so a missing one is either an order from before the change
        or a line nobody priced -- and "unset" means 0 by the owner's rule.
        """
        items = (
            await db.execute(select(OrderItem).where(OrderItem.order_id == order.order_id))
        ).scalars().all()
        return [
            SaleLine(
                order_item_id=item.order_item_id,
                product_id=item.product_id,
                product_name=item.product_name,
                quantity=int(item.quantity or 0),
                gross=Decimal(str(item.unit_price or 0)) * int(item.quantity or 0),
                coupon_discount=Decimal(str(item.coupon_discount or 0)),
                rate=Decimal(str(item.referral_commission_percentage or 0)),
            )
            for item in items
        ]

    @staticmethod
    async def accrue_for_order(
        db: AsyncSession,
        order: Order,
        *,
        commit: bool = False,
        policy: Optional[LevelPolicy] = None,
    ) -> list[ReferralCommission]:
        """
        Record what an order owes, to whoever referred the buyer.

        Each line earns its own product's rate (frozen on the order item at
        checkout) on what the customer paid for it, coupon share deducted. A
        line at 0% earns nothing for anyone.

        Two shapes:

        * **Referred by another customer** — one level-1 row to that customer.
        * **Referred by a professional partner** — level 1 to that partner and,
          if the partner was itself recruited, level 2 to their upline at
          ``policy.level2_share`` of each line's rate. Only two levels: the
          depth is fixed here rather than walked, because an unbounded chain is
          a pyramid, not a referral programme.

        No-ops when the buyer has no referrer or nothing earns. Safe to run
        twice; the partial unique indexes are the authority.
        """
        buyer = await db.get(User, order.user_id)
        if buyer is None:
            return []
        if buyer.referred_by_partner_id is None and buyer.referred_by_user_id is None:
            return []

        sale_lines = await ReferralService._sale_lines(db, order)
        if not sale_lines:
            return []

        if policy is None:
            policy = (await ReferralProgramConfig.load(db)).policy
        level1 = build_lines(sale_lines, policy.for_level(1))
        created: list[ReferralCommission] = []

        if buyer.referred_by_user_id is not None:
            from app.services.partner_wallet_service import PartnerWalletService
            # Serialises upgrade with in-flight old-code accruals.
            await db.execute(select(User.user_id).where(User.user_id == buyer.referred_by_user_id).with_for_update())
            linked = await PartnerWalletService.linked(db, buyer.referred_by_user_id, active=False)
            if linked:
                buyer.referred_by_partner_id = linked.partner_id
                buyer.referred_by_user_id = None

        # --- Referred by a professional partner ---------------------------
        if buyer.referred_by_partner_id is not None:
            row = await ReferralService._add_commission(
                db,
                order=order,
                source_user_id=buyer.user_id,
                level=1,
                lines=level1,
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
                    lines=build_lines(sale_lines, policy.for_level(2)),
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
                    lines=level1,
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
        delivered_at=None,
        commit: bool = False,
    ) -> int:
        """
        Schedule an order's commissions to vest once its return window closes.

        Delivery is NOT the moment the money is earned. The customer then has
        twelve hours to send the goods back, and this used to vest on delivery
        — so a referrer could claim, spend, and be clawed back into a negative
        balance because their friend returned a kettle. The clawback works;
        collecting a debt from someone who did nothing wrong is the problem.

        So this sets ``claimable_at`` and leaves the row ``pending``.
        :meth:`vest_due_commissions` moves it when the window has actually
        passed.

        Only ``pending`` rows are touched. A row already reversed stays
        reversed — a delivered-after-refund sequence must not resurrect money.
        """
        from app.services.order_service import RETURN_WINDOW

        result = await db.execute(
            select(ReferralCommission).where(
                ReferralCommission.order_id == order_id,
                ReferralCommission.status == ReferralCommissionStatus.PENDING,
            )
        )
        rows = result.scalars().all()

        base = delivered_at or datetime.now(timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        # A minute past the customer's own deadline, so the two cannot race:
        # a return landing on the last second must still win against a
        # commission vesting on the same second.
        vest_at = base + RETURN_WINDOW + timedelta(minutes=1)

        for row in rows:
            row.claimable_at = vest_at

        if rows and commit:
            await db.commit()
        if rows:
            logger.info(
                f"{len(rows)} referral commission(s) scheduled to vest at {vest_at} "
                f"for order {order_id}"
            )
        return len(rows)

    @staticmethod
    def returned_quantities(order: Order) -> dict[str, int]:
        """
        Units coming back per order item id.

        No ``return_items`` means the whole order came back -- the same reading
        as ``OrderService._compute_return_refund``.
        """
        items = list(order.items or [])
        if not order.return_items:
            return {str(i.order_item_id): int(i.quantity or 0) for i in items}
        ordered = {str(i.order_item_id): int(i.quantity or 0) for i in items}
        returned: dict[str, int] = {}
        for line in order.return_items:
            key = str(line.get("order_item_id"))
            if key in ordered:
                qty = min(int(line.get("quantity", 0) or 0), ordered[key])
                if qty > 0:
                    returned[key] = qty
        return returned

    @staticmethod
    async def reduce_for_return(
        db: AsyncSession,
        order: Order,
        *,
        legacy_returned_value: Decimal = Decimal("0"),
        commit: bool = False,
    ) -> int:
        """
        Take back what the returned units earned, line by line.

        Each line keeps ``(quantity - returned_quantity) / quantity`` of its
        commission, recomputed from its stored basis and rate. The returned
        quantity is the order's return set, not an increment, so looking at
        the same return twice changes nothing. The header follows its lines.

        Already-PAID customer rows are clawed back by the difference only, as
        before. Rows written before 081 have no lines and go through the old
        proportional path with ``legacy_returned_value``.
        """
        returned = ReferralService.returned_quantities(order)
        if not returned:
            return 0

        result = await db.execute(
            select(ReferralCommission)
            .where(
                ReferralCommission.order_id == order.order_id,
                ReferralCommission.status.in_(
                    [
                        ReferralCommissionStatus.PENDING,
                        ReferralCommissionStatus.CLAIMABLE,
                        ReferralCommissionStatus.PAID,
                    ]
                ),
            )
            .options(selectinload(ReferralCommission.lines))
            .with_for_update()
        )
        rows = result.scalars().all()
        touched = 0
        has_legacy = False

        for row in rows:
            if not row.lines:
                has_legacy = True
                continue

            returned_basis = Decimal("0")
            for line in row.lines:
                want = min(returned.get(str(line.order_item_id), 0), line.quantity)
                line.returned_quantity = max(int(line.returned_quantity or 0), want)
                line.amount = kept_amount(
                    line.basis, line.rate_percentage, line.quantity, line.returned_quantity
                )
                returned_basis += (
                    Decimal(str(line.basis)) * line.returned_quantity / line.quantity
                )

            old_amount = Decimal(str(row.amount or 0))
            new_amount = _money(sum((Decimal(str(line.amount)) for line in row.lines), Decimal("0")))
            difference = old_amount - new_amount
            if difference <= 0:
                continue

            row.amount = new_amount
            row.returned_subtotal = _money(returned_basis)

            if row.status == ReferralCommissionStatus.PAID and row.beneficiary_partner_id:
                from app.services.partner_wallet_service import PartnerWalletService
                await PartnerWalletService.adjust_commission(db, row, difference)
            if row.status == ReferralCommissionStatus.PAID and row.beneficiary_user_id:
                await WalletService.apply_transaction(
                    db,
                    user_id=row.beneficiary_user_id,
                    tx_type=WalletTransactionType.SPENT,
                    amount=difference,
                    title="Referral adjusted — items returned",
                    reason=WalletReason.COMMISSION_REVERSAL,
                    order_id=row.order_id,
                    order_number=row.order_number,
                )

            if new_amount <= 0:
                row.status = ReferralCommissionStatus.REVERSED
            touched += 1

        if has_legacy and legacy_returned_value > 0:
            touched += await ReferralService.reduce_for_returned_value(
                db, order.order_id, legacy_returned_value
            )

        if touched and commit:
            await db.commit()
        if touched:
            logger.info(
                f"{touched} referral commission(s) reduced for returned goods "
                f"on order {order.order_number}"
            )
        return touched

    @staticmethod
    async def reduce_for_returned_value(
        db: AsyncSession,
        order_id: UUID,
        returned_subtotal: Decimal,
        *,
        commit: bool = False,
    ) -> int:
        """
        Take back the commission on goods that came back, and only those.

        A customer returning one item out of five should cost the referrer the
        commission on that item. The other four were delivered and kept, and
        the referrer did the same work for them either way. Voiding the whole
        commission over a single returned kettle is the kind of rule that is
        easy to write and impossible to defend to the person it happens to.

        The commission is recomputed against the remaining basis at the rate
        stored on the row, so a rate change since the order does not leak in.

        Already-PAID rows are clawed back for the difference only. That can
        push a wallet negative, and it is meant to -- the ledger records the
        debt rather than quietly forgiving it -- but it is now the difference
        rather than the lot.

        Returning everything leaves the row at zero rather than deleting it:
        the history of what was earned and taken back is the audit trail.
        """
        if returned_subtotal <= 0:
            return 0

        result = await db.execute(
            select(ReferralCommission)
            .where(
                ReferralCommission.order_id == order_id,
                # Rows with lines are reduced per line by reduce_for_return;
                # this proportional path is for the ones written before 081.
                ~ReferralCommission.lines.any(),
                ReferralCommission.status.in_(
                    [
                        ReferralCommissionStatus.PENDING,
                        ReferralCommissionStatus.CLAIMABLE,
                        ReferralCommissionStatus.PAID,
                    ]
                ),
            )
            .with_for_update()
        )
        rows = result.scalars().all()
        touched = 0

        for row in rows:
            basis = Decimal(str(row.order_subtotal or 0))
            if basis <= 0:
                continue

            # Cumulative: a second partial return adds to the first rather
            # than replacing it.
            returned = min(
                Decimal(str(row.returned_subtotal or 0)) + returned_subtotal, basis
            )
            remaining = basis - returned
            new_amount = ReferralService._commission_amount(
                remaining, float(row.rate_percentage)
            )
            old_amount = Decimal(str(row.amount or 0))
            difference = old_amount - new_amount
            if difference <= 0:
                continue

            row.returned_subtotal = returned
            row.amount = new_amount

            # Money already in their wallet has to come back out. Only the
            # difference -- they keep what the delivered goods earned.
            if row.status == ReferralCommissionStatus.PAID and row.beneficiary_partner_id:
                from app.services.partner_wallet_service import PartnerWalletService
                await PartnerWalletService.adjust_commission(db, row, difference)
            if row.status == ReferralCommissionStatus.PAID and row.beneficiary_user_id:
                await WalletService.apply_transaction(
                    db,
                    user_id=row.beneficiary_user_id,
                    tx_type=WalletTransactionType.SPENT,
                    amount=difference,
                    title="Referral adjusted — items returned",
                    reason=WalletReason.COMMISSION_REVERSAL,
                    order_id=row.order_id,
                    order_number=row.order_number,
                )

            if new_amount <= 0:
                row.status = ReferralCommissionStatus.REVERSED
            touched += 1

        if touched and commit:
            await db.commit()
        if touched:
            logger.info(
                f"{touched} referral commission(s) reduced for returned goods "
                f"on order {order_id}"
            )
        return touched

    @staticmethod
    async def vest_due_commissions(
        db: AsyncSession, *, limit: int = 500
    ) -> list[ReferralCommission]:
        """
        Move every commission whose return window has closed to ``claimable``.

        Returns the rows it vested, so the caller can tell the people who just
        earned something. That is the whole reason this is a sweep rather than
        a check at read time: a notification has to fire when the money becomes
        theirs, not the next time they happen to open the app.

        ``FOR UPDATE SKIP LOCKED`` so two app replicas running this loop cannot
        vest the same row twice — whichever gets the lock does the work and the
        other moves on. `limit` keeps one pass bounded; the next tick takes the
        rest.
        """
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(ReferralCommission)
            .where(
                ReferralCommission.status == ReferralCommissionStatus.PENDING,
                ReferralCommission.claimable_at.isnot(None),
                ReferralCommission.claimable_at <= now,
            )
            .order_by(ReferralCommission.claimable_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list(result.scalars().all())
        for row in rows:
            row.status = ReferralCommissionStatus.CLAIMABLE

        if rows:
            await db.flush()
            from app.services.partner_wallet_service import PartnerWalletService
            for partner_id in sorted({r.beneficiary_partner_id for r in rows if r.beneficiary_partner_id}, key=str):
                await PartnerWalletService.credit_available(db, partner_id)
            await db.commit()
            logger.info(f"{len(rows)} referral commission(s) vested")
        return rows

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
            if row.status == ReferralCommissionStatus.PAID and row.beneficiary_partner_id:
                from app.services.partner_wallet_service import PartnerWalletService
                amount = Decimal(str(row.amount))
                row.amount = Decimal("0.00")
                await PartnerWalletService.adjust_commission(db, row, amount)
            if row.status == ReferralCommissionStatus.PAID and row.beneficiary_user_id:
                await WalletService.apply_transaction(
                    db,
                    user_id=row.beneficiary_user_id,
                    tx_type=WalletTransactionType.SPENT,
                    amount=Decimal(str(row.amount)),
                    title="Referral commission reversed",
                    reason=WalletReason.COMMISSION_REVERSAL,
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
        from app.services.partner_wallet_service import PartnerWalletService
        await PartnerWalletService.user_lock(db, user_id)
        linked = await PartnerWalletService.linked(db, user_id)
        if linked:
            count = await PartnerWalletService.credit_available(db, linked.partner_id)
            await db.commit()
            return {"claimed_count": count, "amount": Decimal("0.00"), "destination": "partner_wallet"}
        result = await db.execute(
            select(ReferralCommission)
            .where(
                ReferralCommission.beneficiary_user_id == user_id,
                ReferralCommission.status == ReferralCommissionStatus.CLAIMABLE,
                # Belt and braces. Status is set by the sweeper; this is the
                # underlying fact. A bug that vested a row early must not be
                # able to pay it out early as well -- the two would have to be
                # wrong in the same direction on the same row.
                ReferralCommission.claimable_at.isnot(None),
                ReferralCommission.claimable_at <= datetime.now(timezone.utc),
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
            reason=WalletReason.COMMISSION_CREDIT,
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
            .options(selectinload(ReferralCommission.lines))
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
                # When a pending row becomes theirs. Without it the app can
                # only say "pending", and "pending until when" is the only
                # question anyone actually has about money they can see and
                # cannot touch.
                "claimable_at": (
                    row.claimable_at.isoformat() if row.claimable_at else None
                ),
                "lines": [line_payload(line) for line in row.lines],
            }
            for row in result.scalars().all()
        ]
