"""
Partner Portal API — the professional referral team.

  POST /partner/auth/login   — separate login, refuses admin/customer accounts
  GET  /partner/me           — profile, code, commission totals by level
  GET  /partner/customers    — customers this partner signed up
  GET  /partner/recruits     — sub-partners, and the level-2 they generate
  GET  /partner/commissions  — the ledger

**Realm separation is the point of this module.** Every route depends on
``get_current_partner``, which requires the ``is_partner`` claim positively.
An admin token is refused here, and a partner token is refused by
``get_current_admin`` — so the professional team's portal shares no
authenticated surface with the administration dashboard, which is exactly why
it is served as a separate frontend.

Prefix "/partner" is applied by app/api/v1/router.py.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.dependencies import get_current_partner
from app.core.security import create_access_token
from app.schemas.partner import PartnerLoginRequest
from app.services.partner_service import PartnerService

router = APIRouter(tags=["Partner Portal"])


# ============================================================================
# POST /auth/login
# ============================================================================

@router.post("/auth/login", response_model=dict)
async def partner_login(
    body: PartnerLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Authenticate a partner.

    Deliberately a different endpoint from admin and customer login, issuing a
    token marked ``is_partner``. That claim is what every /partner/* route
    checks and what /admin/* refuses, so credentials from one realm are inert
    in the other.
    """
    partner = await PartnerService.authenticate(db, body.email, body.password)

    if partner is None:
        # One message for every failure — unknown email, wrong password,
        # deactivated — so this cannot be used to discover which agents exist.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "success": False,
                "error": {
                    "message": "Invalid email or password.",
                    "code": "INVALID_CREDENTIALS",
                },
            },
        )

    token = create_access_token(
        data={
            "sub": str(partner.partner_id),
            "email": partner.email,
            "is_partner": True,
        },
        expires_delta=timedelta(minutes=settings.jwt_access_token_expire_minutes),
    )

    logger.info(f"Partner login: {partner.email}")
    return {
        "success": True,
        "data": {
            "token": token,
            "partner": {
                "partnerId": str(partner.partner_id),
                "email": partner.email,
                "fullName": partner.full_name,
            },
        },
    }


# ============================================================================
# GET /me
# ============================================================================

@router.get("/me", response_model=dict)
async def get_my_partner_profile(
    db: AsyncSession = Depends(get_db),
    partner=Depends(get_current_partner),
):
    """Profile, invite code and commission totals — the dashboard header."""
    code = await PartnerService.get_or_create_code(db, partner)
    summary = await PartnerService.get_summary(db, partner.partner_id)

    return {
        "success": True,
        "data": {
            "partnerId": str(partner.partner_id),
            "fullName": partner.full_name,
            "email": partner.email,
            "referralCode": code,
            "commissionPercent": settings.referral_commission_percent,
            "currency": settings.wallet_currency,
            "hasUpline": partner.parent_partner_id is not None,
            "totals": {
                "pending": float(summary["totals"]["pending"]),
                "claimable": float(summary["totals"]["claimable"]),
                "paid": float(summary["totals"]["paid"]),
            },
            "byLevel": {
                "level1": float(summary["by_level"][1]),
                "level2": float(summary["by_level"][2]),
            },
        },
    }


# ============================================================================
# GET /customers
# ============================================================================

@router.get("/customers", response_model=dict)
async def get_my_customers(
    db: AsyncSession = Depends(get_db),
    partner=Depends(get_current_partner),
):
    """Customers this partner signed up (level-1 earnings)."""
    customers = await PartnerService.list_customers(db, partner.partner_id)
    return {
        "success": True,
        "data": {"customers": customers, "total": len(customers)},
    }


# ============================================================================
# GET /recruits
# ============================================================================

@router.get("/recruits", response_model=dict)
async def get_my_recruits(
    db: AsyncSession = Depends(get_db),
    partner=Depends(get_current_partner),
):
    """Sub-partners this partner recruited (level-2 earnings)."""
    recruits = await PartnerService.list_recruits(db, partner.partner_id)
    return {
        "success": True,
        "data": {"recruits": recruits, "total": len(recruits)},
    }


# ============================================================================
# GET /commissions
# ============================================================================

@router.get("/commissions", response_model=dict)
async def get_my_commissions(
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    partner=Depends(get_current_partner),
):
    """
    The partner's commission ledger.

    No payout endpoint yet: partners have no wallet, so settlement happens out
    of band. This is the statement they and finance reconcile against.
    """
    rows = await PartnerService.list_commissions(db, partner.partner_id, limit)
    return {
        "success": True,
        "data": {"commissions": rows, "total": len(rows)},
    }
