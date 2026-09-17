"""
Coupon Service — authoritative coupon validation & redemption.

All discount values come from the ``coupons`` table; client-supplied discounts
are never trusted. Used by the cart apply-coupon endpoint (validate only) and
by order creation (validate + atomically increment ``used_count``).
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coupon import Coupon


class CouponValidationError(ValueError):
    """Raised when a coupon is invalid or cannot be applied.

    Subclasses ``ValueError`` so existing endpoint handlers map it to a
    400 with the (user-friendly) message.
    """


# Orders in these states never actually cost the branch money, so they release
# their coupon redemption and their share of the campaign budget.
_VOID_STATUSES = ("cancelled", "refunded", "rto_initiated")


def _as_aware_utc(dt: datetime) -> datetime:
    """Coerce a possibly-naive datetime to timezone-aware UTC for comparison."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class CouponService:
    """Business logic for validating and redeeming coupons."""

    # ------------------------------------------------------------------
    # Eligibility
    # ------------------------------------------------------------------

    @staticmethod
    def eligible_subtotal(coupon: Coupon, items: list[dict]) -> Decimal:
        """
        The part of the cart this coupon may actually discount.

        Two independent restrictions narrow it, and both deliberately affect
        ``min_order_value`` as well as the discount base — otherwise a customer
        could clear a Rs 3,000 minimum with Rs 2,900 of excluded clearance stock
        and Rs 100 of eligible goods, which is the loophole this exists to close.

        1. ``exclude_quick_sale`` — clearance lines are already at or below
           cost, so stacking a coupon on them is the double-discounting that
           turns a promotion into a loss.
        2. Catalog eligibility — when the coupon names categories and/or
           products, only lines matching one of them count. Naming NEITHER
           means the entire catalog is eligible, which is why every coupon that
           predates the feature keeps working unchanged.

        Each item needs ``price``, ``quantity``, and (for the restrictions to
        bite) ``is_on_sale``, ``product_id`` and ``category_id``. A caller that
        cannot supply those gets the full subtotal back rather than a silently
        wrong zero — checkout always supplies them; see
        ``CartService.items_for_coupon``.
        """
        category_ids = {str(c.category_id) for c in (coupon.eligible_categories or [])}
        product_ids = {str(pr.product_id) for pr in (coupon.eligible_products or [])}
        restricted = bool(category_ids or product_ids)

        total = Decimal("0")
        for item in items:
            if restricted:
                pid = str(item.get("product_id") or "")
                cid = str(item.get("category_id") or "")
                if pid not in product_ids and cid not in category_ids:
                    continue
            total += Decimal(str(item["price"])) * Decimal(str(item["quantity"]))
        return total

    @staticmethod
    async def enrich_items(
        db: AsyncSession, items: list[dict], branch_id: Optional[UUID]
    ) -> list[dict]:
        """
        Attach ``is_on_sale`` and ``category_id`` to cart lines.

        The Redis cart stores only what the customer chose — product, quantity,
        price — and deliberately not whether the item is on clearance, because
        that changes while the cart sits there. Eligibility must be decided
        against the CURRENT state of the branch's inventory, not a snapshot
        taken when the item was added, or a customer could add an item before a
        Quick Sale started and still stack a coupon on it afterwards.

        Returns copies; the caller's list is left alone.
        """
        from app.models.product import BranchInventory, Product

        ids = []
        for item in items:
            raw = item.get("product_id")
            if not raw:
                continue
            try:
                ids.append(UUID(str(raw)))
            except (ValueError, TypeError):
                continue
        if not ids:
            return [dict(i) for i in items]

        rows = (
            await db.execute(
                select(
                    Product.product_id,
                    Product.category_id,
                    BranchInventory.is_on_sale,
                )
                .select_from(Product)
                .outerjoin(
                    BranchInventory,
                    (BranchInventory.product_id == Product.product_id)
                    & (BranchInventory.branch_id == branch_id),
                )
                .where(Product.product_id.in_(ids))
            )
        ).all()
        meta = {
            str(r.product_id): {
                "category_id": str(r.category_id) if r.category_id else None,
                "is_on_sale": bool(r.is_on_sale),
            }
            for r in rows
        }

        enriched = []
        for item in items:
            copy = dict(item)
            copy.update(meta.get(str(item.get("product_id")), {}))
            enriched.append(copy)
        return enriched

    # ------------------------------------------------------------------
    # Redemption history (used by the per-customer and first-order rules)
    # ------------------------------------------------------------------

    @staticmethod
    async def redemptions_by_user(
        db: AsyncSession, coupon_code: str, user_id: UUID
    ) -> int:
        """
        How many times this customer has already redeemed this code.

        Counted from ``orders`` rather than a counter, and cancelled/refunded
        orders are excluded: a customer whose order was cancelled did not
        actually consume one of their allowed uses, and telling them otherwise
        makes support fix it by hand.
        """
        from app.models.order import Order

        return int(
            await db.scalar(
                select(func.count(Order.order_id)).where(
                    func.lower(Order.coupon_code) == coupon_code.strip().lower(),
                    Order.user_id == user_id,
                    Order.status.notin_(_VOID_STATUSES),
                )
            )
            or 0
        )

    @staticmethod
    async def has_completed_order(
        db: AsyncSession, user_id: UUID, branch_id: Optional[UUID] = None
    ) -> bool:
        """
        Whether this customer has ever completed an order (optionally, at one
        branch). Backs ``first_order_only``.

        "Completed" is deliberately DELIVERED-only: a pending or cancelled
        order is not a relationship with the branch, and an acquisition coupon
        should still work for someone whose first attempt never arrived.
        """
        from app.models.order import Order, OrderStatus

        filters = [
            Order.user_id == user_id,
            Order.status == OrderStatus.DELIVERED.value,
        ]
        if branch_id is not None:
            filters.append(Order.branch_id == branch_id)
        return bool(await db.scalar(select(Order.order_id).where(*filters).limit(1)))

    @staticmethod
    async def get_by_code(db: AsyncSession, code: str) -> Optional[Coupon]:
        """Look up a coupon by code (case-insensitive)."""
        result = await db.execute(
            select(Coupon).where(func.lower(Coupon.code) == code.strip().lower())
        )
        return result.scalar_one_or_none()

    @staticmethod
    def validate(
        coupon: Coupon,
        subtotal: Decimal,
        now: Optional[datetime] = None,
        branch_id: Optional[UUID] = None,
        *,
        user_id: Optional[UUID] = None,
        user_redemptions: Optional[int] = None,
        user_has_completed_order: Optional[bool] = None,
    ) -> None:
        """
        Enforce all coupon rules. Raises :class:`CouponValidationError` with a
        user-friendly message on the first failure.

        ``branch_id`` is the branch fulfilling the order. A coupon owned by a
        branch is only redeemable there; ``coupon.branch_id IS NULL`` means
        network-wide and is accepted everywhere. Callers that genuinely have
        no branch context yet (a pre-cart code preview) may omit it, but the
        checkout path must always pass it — otherwise a Colombo promotion
        could be burned against a Galle order.

        ``subtotal`` must be the ELIGIBLE subtotal (see :meth:`eligible_subtotal`),
        not the raw cart total, so ``min_order_value`` is measured against the
        goods the coupon can actually discount.

        The three keyword arguments carry facts this synchronous method cannot
        look up itself. Each is checked only when supplied, so a caller with no
        customer context (the pre-cart preview) still gets every rule that does
        not need one — and ``OrderService`` supplies all of them, which is what
        makes checkout the authoritative gate:

        - ``user_id`` — checked against a targeted (1-to-1) coupon.
        - ``user_redemptions`` — this customer's prior redemptions, for
          ``per_user_limit``.
        - ``user_has_completed_order`` — for ``first_order_only``.
        """
        now = now or datetime.now(timezone.utc)
        now = _as_aware_utc(now)

        # Checked BEFORE is_active: auto_stop_on_budget switches the coupon off
        # the moment the budget runs out, so a depleted campaign is also
        # inactive and the generic message would win the race. A customer
        # holding a code from a finished campaign should be told the promotion
        # ended, not handed something that reads like their code is broken.
        #
        # Distinct from "usage limit" for the same reason: the offer is over,
        # the customer did not do anything wrong.
        if coupon.is_budget_depleted:
            raise CouponValidationError("This promotion has ended.")

        if not coupon.is_active:
            raise CouponValidationError("This coupon is no longer active.")

        coupon_branch = getattr(coupon, "branch_id", None)
        if coupon_branch is not None and branch_id is not None and coupon_branch != branch_id:
            raise CouponValidationError(
                "This coupon is not valid for your delivery branch."
            )

        if coupon.valid_from and now < _as_aware_utc(coupon.valid_from):
            raise CouponValidationError("This coupon is not valid yet.")

        if coupon.valid_until and now > _as_aware_utc(coupon.valid_until):
            raise CouponValidationError("This coupon has expired.")

        # A targeted coupon belongs to one named customer. Checked before the
        # generic rules so the wrong person is told it isn't theirs rather than
        # being walked through why it "expired".
        if coupon.target_user_id is not None:
            if user_id is None or coupon.target_user_id != user_id:
                raise CouponValidationError(
                    "This coupon was issued to a specific customer."
                )

        if (
            coupon.usage_limit is not None
            and (coupon.used_count or 0) >= coupon.usage_limit
        ):
            raise CouponValidationError("This coupon has reached its usage limit.")

        if (
            coupon.per_user_limit is not None
            and user_redemptions is not None
            and user_redemptions >= coupon.per_user_limit
        ):
            raise CouponValidationError(
                "You have already used this coupon the maximum number of times."
            )

        if coupon.first_order_only and user_has_completed_order:
            raise CouponValidationError(
                "This coupon is only valid on a customer's first order."
            )


        # Measured against the ELIGIBLE subtotal (see the docstring): otherwise
        # a cart of excluded clearance stock could clear the minimum for a few
        # eligible rupees of goods.
        min_order = Decimal(str(coupon.min_order_value or 0))
        if min_order > 0 and subtotal < min_order:
            raise CouponValidationError(
                f"This coupon requires a minimum order of Rs {min_order:.2f}."
            )

    @staticmethod
    async def atomic_increment_usage(db: AsyncSession, coupon_id: UUID) -> bool:
        """
        Atomically bump ``used_count`` iff the coupon is not already maxed out.

        The ``used_count < usage_limit`` guard lives in the WHERE clause, so
        Postgres re-checks it under a row lock — concurrent redemptions of the
        last slot cannot both succeed. Returns True if incremented, False if the
        coupon was already at its limit. Does NOT commit (caller owns the txn).
        """
        stmt = (
            update(Coupon)
            .where(Coupon.coupon_id == coupon_id)
            .where(
                or_(
                    Coupon.usage_limit.is_(None),
                    Coupon.used_count < Coupon.usage_limit,
                )
            )
            .values(used_count=Coupon.used_count + 1)
        )
        result = await db.execute(stmt)
        return (result.rowcount or 0) == 1

    @staticmethod
    async def atomic_consume_budget(
        db: AsyncSession, coupon_id: UUID, discount: Decimal
    ) -> bool:
        """
        Atomically charge ``discount`` against the campaign budget.

        The ``budget_spent + discount <= budget_cap`` test lives in the WHERE
        clause so Postgres re-evaluates it under a row lock. Two checkouts
        racing for the last Rs 200 of budget therefore cannot both succeed —
        which is the entire point of a cap, and is not something an
        application-level "read then write" could guarantee.

        Returns True if the budget absorbed the charge (or the coupon is
        uncapped), False if this order would overrun the cap and must be
        refused. Does NOT commit (caller owns the txn).
        """
        amount = Decimal(str(discount or 0))
        if amount <= 0:
            return True

        stmt = (
            update(Coupon)
            .where(Coupon.coupon_id == coupon_id)
            .where(
                or_(
                    Coupon.budget_cap.is_(None),
                    Coupon.budget_spent + amount <= Coupon.budget_cap,
                )
            )
            .values(budget_spent=Coupon.budget_spent + amount)
        )
        result = await db.execute(stmt)
        if (result.rowcount or 0) != 1:
            return False

        # Auto-stop: flip the coupon off in the same transaction that exhausted
        # it, so it stops being offered the instant it stops being affordable.
        # Guarded on auto_stop_on_budget so a manager who wants to be alerted
        # rather than cut off still gets refusals at checkout (validate() tests
        # is_budget_depleted regardless) without the row flipping under them.
        await db.execute(
            update(Coupon)
            .where(
                Coupon.coupon_id == coupon_id,
                Coupon.auto_stop_on_budget.is_(True),
                Coupon.budget_cap.isnot(None),
                Coupon.budget_spent >= Coupon.budget_cap,
            )
            .values(is_active=False)
        )
        return True

    @staticmethod
    async def atomic_release_budget(
        db: AsyncSession, coupon_id: UUID, discount: Decimal
    ) -> None:
        """
        Return ``discount`` to the campaign budget when a redeeming order is
        cancelled or refunded.

        Money the branch never actually gave away must not keep a campaign
        switched off; ``GREATEST(..., 0)`` keeps a double-release (a cancel
        followed by a refund on the same order) from driving the figure
        negative. Deliberately does NOT re-activate the coupon: a manager who
        saw their campaign stop should decide whether it restarts, rather than
        having it silently resume because one customer cancelled.
        """
        amount = Decimal(str(discount or 0))
        if amount <= 0:
            return
        await db.execute(
            update(Coupon)
            .where(Coupon.coupon_id == coupon_id)
            .values(
                budget_spent=func.greatest(Coupon.budget_spent - amount, 0)
            )
        )

    @staticmethod
    async def atomic_decrement_usage(db: AsyncSession, coupon_id: UUID) -> bool:
        """
        Atomically release a redemption slot: ``used_count = used_count - 1``,
        guarded by ``used_count > 0`` so it can never go negative. Used when a
        redeeming order is cancelled. Returns True if a slot was released.
        Does NOT commit (caller owns the txn).
        """
        stmt = (
            update(Coupon)
            .where(Coupon.coupon_id == coupon_id)
            .where(Coupon.used_count > 0)
            .values(used_count=Coupon.used_count - 1)
        )
        result = await db.execute(stmt)
        return (result.rowcount or 0) == 1
