"""
Who may set a branch's cashback override, and how high.

Cashback is money paid into customers' wallets, so one role owns it: the
Branch Manager for their own branch, and the Super Admin. Marketing and
Inventory Managers (and custom staff roles holding ``inventory:update``) see
the rate but cannot change it.

Every branch override is capped by ``app_settings.cashback_branch_max_percent``
(default 30) -- a typo of 50 instead of 5 is real money. The Super Admin is
capped too; they raise the setting if a higher rate is really meant.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_settings import AppSetting
from app.services.referral_commission.config import _parse

BRANCH_MAX_KEY = "cashback_branch_max_percent"
DEFAULT_BRANCH_MAX = Decimal("30")
EDITOR_ROLES = frozenset({"super_admin", "branch_manager"})


def _role(role) -> str:
    return role.value if hasattr(role, "value") else str(role or "")


def can_edit(role) -> bool:
    """True for the roles that may change a branch's cashback."""
    return _role(role) in EDITOR_ROLES


async def branch_max_percent(db: AsyncSession) -> Decimal:
    row = (
        await db.execute(
            select(AppSetting.value, AppSetting.is_active).where(AppSetting.key == BRANCH_MAX_KEY)
        )
    ).first()
    raw = row.value if row is not None and row.is_active else None
    return _parse(raw, Decimal("0"), Decimal("100"), DEFAULT_BRANCH_MAX, BRANCH_MAX_KEY)


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return Decimal(str(a)) == Decimal(str(b))


async def guard(db: AsyncSession, role, fields: dict, current=None) -> None:
    """
    Refuse a branch-inventory write whose ``cashback_percentage`` the caller
    may not make. *fields* is the request's set fields; *current* is the
    row's rate today (None for a new row). A write that leaves cashback as it
    is passes for anyone -- the admin forms send every field on each save, so
    an Inventory Manager saving stock must not be refused.
    """
    if "cashback_percentage" not in fields:
        return
    rate = fields["cashback_percentage"]
    if _same(rate, current):
        return
    if not can_edit(role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "Only a Branch Manager or Super Admin can change cashback.",
                "code": "CASHBACK_NOT_ALLOWED",
            },
        )
    if rate is None:
        return
    cap = await branch_max_percent(db)
    if Decimal(str(rate)) > cap:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": f"The highest cashback a branch can set is {cap.normalize():f}%.",
                "code": "CASHBACK_ABOVE_CAP",
                "cap": float(cap),
            },
        )


def describe(role, cap: Optional[Decimal]) -> dict:
    """What the admin UI needs to draw the cashback field."""
    return {
        "can_edit_cashback": can_edit(role),
        "cashback_branch_max_percent": float(cap) if cap is not None else None,
    }
