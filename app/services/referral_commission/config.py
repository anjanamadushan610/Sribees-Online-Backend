"""The two programme-wide knobs, stored in ``app_settings`` (Super Admin)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_settings import AppSetting
from app.services.referral_commission.policy import LevelPolicy

BRANCH_MAX_PERCENT_KEY = "referral_branch_max_percent"
LEVEL2_SHARE_KEY = "referral_level2_share"

DEFAULT_BRANCH_MAX_PERCENT = Decimal("10")
DEFAULT_LEVEL2_SHARE = Decimal("0.5")


def _parse(raw: Optional[str], low: Decimal, high: Decimal, fallback: Decimal, key: str) -> Decimal:
    """A stored value inside [low, high], or the default.

    A malformed row must never become a rate: it falls back loudly instead.
    """
    if raw is None or str(raw).strip() == "":
        return fallback
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        logger.error(f"app_settings.{key} is not a number; using {fallback}")
        return fallback
    if not (low <= value <= high):
        logger.error(f"app_settings.{key}={value} is outside {low}..{high}; using {fallback}")
        return fallback
    return value


@dataclass(frozen=True)
class ReferralProgramConfig:
    """
    ``branch_max_percent``: the highest rate a Branch Manager may set (a
    typo of 50 instead of 5 is real money). ``level2_share``: the upline's
    fraction of the level-1 rate.
    """

    branch_max_percent: Decimal = DEFAULT_BRANCH_MAX_PERCENT
    level2_share: Decimal = DEFAULT_LEVEL2_SHARE

    @property
    def policy(self) -> LevelPolicy:
        return LevelPolicy(level2_share=self.level2_share)

    @staticmethod
    async def load(db: AsyncSession) -> "ReferralProgramConfig":
        rows = {
            key: value
            for key, value, active in (
                await db.execute(
                    select(AppSetting.key, AppSetting.value, AppSetting.is_active).where(
                        AppSetting.key.in_([BRANCH_MAX_PERCENT_KEY, LEVEL2_SHARE_KEY])
                    )
                )
            ).all()
            if active
        }
        return ReferralProgramConfig(
            branch_max_percent=_parse(
                rows.get(BRANCH_MAX_PERCENT_KEY), Decimal("0"), Decimal("100"),
                DEFAULT_BRANCH_MAX_PERCENT, BRANCH_MAX_PERCENT_KEY,
            ),
            level2_share=_parse(
                rows.get(LEVEL2_SHARE_KEY), Decimal("0"), Decimal("1"),
                DEFAULT_LEVEL2_SHARE, LEVEL2_SHARE_KEY,
            ),
        )

    def as_dict(self) -> dict:
        return {
            "branch_max_percent": float(self.branch_max_percent),
            "level2_share": float(self.level2_share),
        }
