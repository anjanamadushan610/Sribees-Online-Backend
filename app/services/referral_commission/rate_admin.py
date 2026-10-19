"""
Setting commission rates: who may set what, how high, and the audit trail.

Every write goes through here so the cap, the branch boundary and the audit
row cannot be skipped by a second code path. Rates only affect orders placed
after the change -- checkout freezes the rate onto each order line.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_settings import AppSetting
from app.models.product import BranchInventory, Product
from app.models.referral import ReferralRateChange
from app.services.referral_commission.config import (
    BRANCH_MAX_PERCENT_KEY,
    LEVEL2_SHARE_KEY,
    ReferralProgramConfig,
)
from app.services.referral_commission.rates import effective_rate

TWO_PLACES = Decimal("0.01")


class RateChangeError(ValueError):
    """A rate change the caller may not make. ``code`` is stable for clients."""

    def __init__(self, code: str, message: str, status: int = 400, **params):
        self.code = code
        self.status = status
        self.params = params
        super().__init__(message)


def parse_rate(value) -> Optional[Decimal]:
    """None clears the rate; anything else must be 0..100 to two places."""
    if value is None:
        return None
    try:
        rate = Decimal(str(value))
    except InvalidOperation:
        raise RateChangeError("INVALID_RATE", "Enter a percentage between 0 and 100.")
    if not rate.is_finite() or rate < 0 or rate > 100:
        raise RateChangeError("INVALID_RATE", "Enter a percentage between 0 and 100.")
    if rate != rate.quantize(TWO_PLACES):
        raise RateChangeError("INVALID_RATE", "Use at most two decimal places.")
    return rate.quantize(TWO_PLACES)


def _same(a: Optional[Decimal], b: Optional[Decimal]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return Decimal(str(a)) == Decimal(str(b))


class RateAdminService:
    """Product defaults (Super Admin) and branch overrides (Branch Manager)."""

    @staticmethod
    async def _audit(
        db: AsyncSession,
        *,
        product_id: UUID,
        branch_id: Optional[UUID],
        old: Optional[Decimal],
        new: Optional[Decimal],
        admin_id: Optional[UUID],
    ) -> None:
        db.add(
            ReferralRateChange(
                product_id=product_id,
                branch_id=branch_id,
                old_percentage=old,
                new_percentage=new,
                changed_by=admin_id,
            )
        )

    @staticmethod
    async def set_product_default(
        db: AsyncSession,
        product_id: UUID,
        raw_rate,
        *,
        admin_id: Optional[UUID],
    ) -> Product:
        """Set or clear a product's default rate. Super Admin only (router)."""
        rate = parse_rate(raw_rate)
        product = await db.get(Product, product_id, with_for_update=True)
        if product is None:
            raise RateChangeError("PRODUCT_NOT_FOUND", "Product not found.", status=404)

        old = product.referral_commission_percentage
        if not _same(old, rate):
            product.referral_commission_percentage = rate
            await RateAdminService._audit(
                db, product_id=product_id, branch_id=None, old=old, new=rate, admin_id=admin_id
            )
            await db.commit()
            await db.refresh(product)
            logger.info(
                f"[referral] product {product_id} default rate {old} -> {rate} by admin {admin_id}"
            )
        return product

    @staticmethod
    async def set_branch_override(
        db: AsyncSession,
        inventory_id: UUID,
        raw_rate,
        *,
        admin_id: Optional[UUID],
        is_super_admin: bool,
        admin_branch_id: Optional[UUID],
    ) -> tuple[BranchInventory, Product]:
        """
        Set or clear one branch's rate for one product.

        A Branch Manager may only touch their own branch, and only up to the
        cap. A Super Admin is not capped -- the cap exists to catch a typo
        made without anyone else seeing it, and the Super Admin is the one who
        sets it.
        """
        rate = parse_rate(raw_rate)
        inv = await db.get(BranchInventory, inventory_id, with_for_update=True)
        if inv is None:
            raise RateChangeError("INVENTORY_NOT_FOUND", "Inventory row not found.", status=404)
        if not is_super_admin and inv.branch_id != admin_branch_id:
            raise RateChangeError(
                "OTHER_BRANCH",
                "You can only change commission for your own branch.",
                status=403,
            )

        if rate is not None and not is_super_admin:
            cap = (await ReferralProgramConfig.load(db)).branch_max_percent
            if rate > cap:
                raise RateChangeError(
                    "ABOVE_CAP",
                    f"The highest commission a branch can set is {cap.normalize():f}%.",
                    cap=float(cap),
                )

        product = await db.get(Product, inv.product_id)
        old = inv.referral_commission_percentage
        if not _same(old, rate):
            inv.referral_commission_percentage = rate
            await RateAdminService._audit(
                db,
                product_id=inv.product_id,
                branch_id=inv.branch_id,
                old=old,
                new=rate,
                admin_id=admin_id,
            )
            await db.commit()
            await db.refresh(inv)
            logger.info(
                f"[referral] branch {inv.branch_id} product {inv.product_id} rate "
                f"{old} -> {rate} by admin {admin_id}"
            )
        return inv, product

    @staticmethod
    async def update_config(
        db: AsyncSession,
        *,
        branch_max_percent=None,
        level2_share=None,
        admin_id: Optional[UUID] = None,
    ) -> ReferralProgramConfig:
        """Change the cap and/or the level-2 share (Super Admin, router)."""
        updates: dict[str, Decimal] = {}
        if branch_max_percent is not None:
            cap = parse_rate(branch_max_percent)
            updates[BRANCH_MAX_PERCENT_KEY] = cap
        if level2_share is not None:
            try:
                share = Decimal(str(level2_share))
            except InvalidOperation:
                share = Decimal("-1")
            if not share.is_finite() or share < 0 or share > 1:
                raise RateChangeError("INVALID_SHARE", "The level-2 share must be between 0 and 1.")
            updates[LEVEL2_SHARE_KEY] = share.quantize(Decimal("0.001"))

        for key, value in updates.items():
            row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
            text = f"{value.normalize():f}"
            if row is None:
                db.add(AppSetting(key=key, value=text, is_active=True))
            else:
                row.value = text
                row.is_active = True
        if updates:
            await db.commit()
            logger.info(f"[referral] programme settings {sorted(updates)} changed by admin {admin_id}")
        return await ReferralProgramConfig.load(db)

    @staticmethod
    async def history(db: AsyncSession, product_id: UUID, limit: int = 50) -> list[dict]:
        rows = (
            await db.execute(
                select(ReferralRateChange)
                .where(ReferralRateChange.product_id == product_id)
                .order_by(ReferralRateChange.changed_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "branch_id": str(r.branch_id) if r.branch_id else None,
                "old_percentage": float(r.old_percentage) if r.old_percentage is not None else None,
                "new_percentage": float(r.new_percentage) if r.new_percentage is not None else None,
                "changed_by": str(r.changed_by) if r.changed_by else None,
                "changed_at": r.changed_at.isoformat() if r.changed_at else None,
            }
            for r in rows
        ]


def describe_rates(product: Product, inv: Optional[BranchInventory]) -> dict:
    """The three numbers an editor needs: override, default, and what applies."""
    branch = inv.referral_commission_percentage if inv is not None else None
    default = product.referral_commission_percentage
    return {
        "referral_commission_percentage": float(branch) if branch is not None else None,
        "global_referral_commission_percentage": float(default) if default is not None else None,
        "effective_referral_commission": float(effective_rate(branch, default)),
    }
