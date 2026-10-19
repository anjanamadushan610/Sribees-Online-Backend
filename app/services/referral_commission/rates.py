"""Which level-1 rate a product carries in a branch, read from the database."""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import BranchInventory, Product

ZERO_RATE = Decimal("0")


def effective_rate(branch_rate, product_rate) -> Decimal:
    """Branch override, then product default, then 0.

    0 rather than a platform rate, by the owner's decision: a product earns a
    commission only once someone has decided it should.
    """
    for rate in (branch_rate, product_rate):
        if rate is not None:
            return Decimal(str(rate))
    return ZERO_RATE


class CommissionRateResolver:
    """Batch lookups, so checkout costs one query however long the cart is."""

    @staticmethod
    async def resolve(
        db: AsyncSession,
        product_ids: Iterable[UUID],
        branch_id: Optional[UUID],
    ) -> dict[str, Decimal]:
        """
        Effective level-1 rate per product id for one branch.

        Read at checkout, never from the Redis cart: the customer's order earns
        the rate that is live when it is placed. Every id asked about gets an
        answer -- one the catalog does not know earns 0.
        """
        ids = list(set(product_ids))
        if not ids:
            return {}

        rows = (
            await db.execute(
                select(
                    Product.product_id,
                    Product.referral_commission_percentage,
                    BranchInventory.referral_commission_percentage,
                )
                .outerjoin(
                    BranchInventory,
                    and_(
                        BranchInventory.product_id == Product.product_id,
                        BranchInventory.branch_id == branch_id,
                    ),
                )
                .where(Product.product_id.in_(ids))
            )
        ).all()

        resolved = {
            str(pid): effective_rate(branch_rate, product_rate)
            for pid, product_rate, branch_rate in rows
        }
        for pid in ids:
            resolved.setdefault(str(pid), ZERO_RATE)
        return resolved

    @staticmethod
    async def highest_live_rate(db: AsyncSession) -> Decimal:
        """
        The best level-1 rate any product currently on sale carries.

        Backs the "earn up to X%" line. Only stocked, active rows count -- a
        rate on a product nobody can buy is not an offer.
        """
        value = (
            await db.execute(
                select(
                    func.max(
                        func.coalesce(
                            BranchInventory.referral_commission_percentage,
                            Product.referral_commission_percentage,
                            0,
                        )
                    )
                )
                .select_from(BranchInventory)
                .join(Product, Product.product_id == BranchInventory.product_id)
                .where(BranchInventory.is_active.is_(True), Product.is_active.is_(True))
            )
        ).scalar_one_or_none()
        return Decimal(str(value)) if value is not None else ZERO_RATE
