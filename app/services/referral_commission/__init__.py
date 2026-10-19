"""
Per-product referral commission.

Every product carries its own level-1 rate (branch override -> product
default -> 0); a partner's upline earns a fixed share of it. The arithmetic
is in :mod:`calculator` and takes plain values only, so the money rules are
tested without a database. See docs/product-referral-commission.md.
"""
from app.services.referral_commission.calculator import (
    CommissionLine,
    SaleLine,
    allocate_discount,
    build_lines,
    kept_amount,
    money,
)
from app.services.referral_commission.config import ReferralProgramConfig
from app.services.referral_commission.policy import LevelPolicy
from app.services.referral_commission.rates import CommissionRateResolver

__all__ = [
    "CommissionLine",
    "CommissionRateResolver",
    "LevelPolicy",
    "ReferralProgramConfig",
    "SaleLine",
    "allocate_discount",
    "build_lines",
    "kept_amount",
    "money",
]
