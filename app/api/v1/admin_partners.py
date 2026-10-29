"""
Admin Partner Management API

CRUD for the professional referral team (the ``partners`` table), plus
commission-ledger visibility and settlement tracking. Restricted to Super
Admins (enforced at the router level) — this is the replacement for the raw
DB inserts migration 028's seed data used to use.

Deliberately separate from ``admin_users.py``: partners are not admins, and
nothing here ever grants an ``is_partner`` or admin token.

Prefix "/admin/partners" is applied by app/api/v1/router.py.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import require_roles
from app.models.partner import Partner
from app.schemas.partner import (
    PartnerCreateRequest,
    PartnerMarkPaidRequest,
    PartnerUpdateRequest,
)
from app.services.partner_service import PartnerEmailTakenError, PartnerService

router = APIRouter(
    dependencies=[Depends(require_roles("super_admin"))],
    tags=["Admin Partners"],
)


async def _get_partner_or_404(db: AsyncSession, partner_id: UUID) -> Partner:
    result = await db.execute(select(Partner).where(Partner.partner_id == partner_id))
    partner = result.scalar_one_or_none()
    if partner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Partner not found",
        )
    return partner


def _format_partner(partner: Partner) -> dict:
    return {
        "partner_id": str(partner.partner_id),
        "email": partner.email,
        "role": partner.role,
        "linked_user_id": str(partner.user_id) if partner.user_id else None,
        "full_name": partner.full_name,
        "phone": partner.phone,
        "referral_code": partner.referral_code,
        "parent_partner_id": (
            str(partner.parent_partner_id) if partner.parent_partner_id else None
        ),
        "is_active": partner.is_active,
        "last_login": partner.last_login.isoformat() if partner.last_login else None,
        "created_at": partner.created_at.isoformat() if partner.created_at else None,
    }


@router.get("", response_model=dict)
async def list_partners(db: AsyncSession = Depends(get_db)):
    """Every partner, most recently created first, with recruiter and totals."""
    partners = await PartnerService.list_all(db)
    return {"success": True, "data": {"partners": partners, "total": len(partners)}}


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_partner(
    data: PartnerCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Create a top-level partner account.

    Always ``parent_partner_id=None`` — an admin-created partner has no
    upline, matching migration 028's "lead" partner. Existing partners grow
    the tree themselves via POST /partner/recruits.
    """
    try:
        partner = await PartnerService.create(
            db,
            email=data.email,
            password=data.password,
            full_name=data.full_name,
            phone=data.phone,
            parent_partner_id=None,
        )
    except PartnerEmailTakenError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    logger.info(f"[admin] Partner created: {partner.email}")
    return {
        "success": True,
        "data": _format_partner(partner),
        "message": "Partner created successfully",
    }


@router.patch("/{partner_id}", response_model=dict)
async def update_partner(
    partner_id: UUID,
    data: PartnerUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Edit a partner (name, phone, password, active status)."""
    partner = await _get_partner_or_404(db, partner_id)
    fields = data.model_dump(exclude_unset=True)
    partner = await PartnerService.set_fields(db, partner, **fields)

    logger.info(f"[admin] Partner updated: {partner_id}")
    return {
        "success": True,
        "data": _format_partner(partner),
        "message": "Partner updated successfully",
    }


@router.get("/{partner_id}/commissions", response_model=dict)
async def get_partner_commissions(
    partner_id: UUID,
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """This partner's full commission ledger, for finance reconciliation."""
    await _get_partner_or_404(db, partner_id)
    rows = await PartnerService.list_commissions(db, partner_id, limit)
    return {"success": True, "data": {"commissions": rows, "total": len(rows)}}


@router.post("/{partner_id}/commissions/mark-paid", response_model=dict)
async def mark_partner_commissions_paid(
    partner_id: UUID,
    data: PartnerMarkPaidRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Record that finance has settled this partner's commissions out-of-band.

    No wallet exists to credit — this only moves ``claimable`` rows to
    ``paid`` so the ledger reflects reality. Omit ``commission_ids`` to
    settle everything currently claimable for this partner.
    """
    await _get_partner_or_404(db, partner_id)
    result = await PartnerService.mark_commissions_paid(
        db, partner_id, data.commission_ids
    )

    if result["count"] == 0:
        return {
            "success": True,
            "data": {"count": 0, "amount": 0.0},
            "message": "Nothing claimable to mark as paid.",
        }

    logger.info(
        f"[admin] Marked {result['amount']} paid over {result['count']} "
        f"commission(s) for partner {partner_id}"
    )
    return {
        "success": True,
        "data": {"count": result["count"], "amount": float(result["amount"])},
        "message": f"{result['count']} commission(s) marked paid.",
    }
