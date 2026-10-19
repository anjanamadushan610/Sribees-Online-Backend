"""
Partner Portal API — the professional referral team.

  POST /partner/auth/login   — separate login, refuses admin/customer accounts
  GET  /partner/me           — profile, code, commission totals by level
  GET  /partner/customers    — customers this partner signed up
  GET  /partner/recruits     — sub-partners, and the level-2 they generate
  POST /partner/recruits     — recruit a new sub-partner
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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.dependencies import get_current_partner
from app.core.security import create_access_token
from app.schemas.partner import PartnerCreateRequest, PartnerLoginRequest
from app.services.partner_service import PartnerEmailTakenError, PartnerService
from app.services.referral_commission import CommissionRateResolver
from app.services.referral_commission.presenter import offer_text

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
    request: Request,
    db: AsyncSession = Depends(get_db),
    partner=Depends(get_current_partner),
):
    """Profile, invite code and commission totals — the dashboard header.

    ``commissionPercent`` is the best rate on offer today (rates are per
    product now); ``commissionText`` is the sentence to show for it.
    """
    code = await PartnerService.get_or_create_code(db, partner)
    summary = await PartnerService.get_summary(db, partner.partner_id)
    highest = await CommissionRateResolver.highest_live_rate(db)

    return {
        "success": True,
        "data": {
            "partnerId": str(partner.partner_id),
            "fullName": partner.full_name,
            "email": partner.email,
            "referralCode": code,
            "commissionPercent": float(highest),
            "commissionText": offer_text(
                highest,
                audience="partner",
                accept_language=request.headers.get("accept-language"),
            ),
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
# POST /recruits
# ============================================================================

@router.post("/recruits", response_model=dict, status_code=status.HTTP_201_CREATED)
async def recruit_partner(
    body: PartnerCreateRequest,
    db: AsyncSession = Depends(get_db),
    partner=Depends(get_current_partner),
):
    """
    Recruit a new sub-partner.

    ``parent_partner_id`` is set to the caller here and nowhere else outside
    admin account creation (which always leaves it null) — this is the
    portal-side replacement for editing the DB directly to build out a
    recruitment tree. The new partner signs in with the email/password given
    here; there is no email service in this codebase to send an invite link,
    so credentials are handed over out of band, the same way the seed data's
    demo partner passwords are.
    """
    if partner.parent_partner_id is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "success": False,
                "error": {
                    "message": "Only Lead partners can recruit sub-partners.",
                    "code": "RECRUIT_NOT_ALLOWED",
                },
            },
        )

    try:
        recruit = await PartnerService.create(
            db,
            email=body.email,
            password=body.password,
            full_name=body.full_name,
            phone=body.phone,
            parent_partner_id=partner.partner_id,
        )
    except PartnerEmailTakenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "error": {"message": str(exc), "code": "EMAIL_TAKEN"},
            },
        )

    logger.info(f"Partner {partner.email} recruited {recruit.email}")
    return {
        "success": True,
        "data": {
            "partnerId": str(recruit.partner_id),
            "email": recruit.email,
            "fullName": recruit.full_name,
        },
        "message": "Partner recruited successfully.",
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


# ============================================================================
# GET /top-products
# ============================================================================

@router.get("/top-products", response_model=dict)
async def get_my_top_products(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    partner=Depends(get_current_partner),
):
    """The products the partner's commission came from, best first."""
    rows = await PartnerService.top_products(db, partner.partner_id, limit)
    return {"success": True, "data": {"products": rows}}
