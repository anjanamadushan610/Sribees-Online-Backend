"""Customer-consented, Super Admin-approved partner upgrades and withdrawals."""
import os
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import (
    get_current_partner,
    get_current_user,
    require_super_admin,
)
from app.models.partner_wallet import PartnerApplication, PartnerWithdrawal
from app.models.user import User
from app.services.partner_wallet_service import PartnerWalletService as Service
from app.services.partner_wallet_service import withdrawal_json


def rollout_enabled():
    from app.services.finance_outbox import enabled as finance_enabled
    if os.getenv('LINKED_PARTNER_WALLET_ENABLED', 'false').lower() != 'true':
        raise HTTPException(404, 'Partner Wallet is not available yet.')
    if finance_enabled() and os.getenv('PARTNER_WALLET_FINANCE_READY', 'false').lower() != 'true':
        raise HTTPException(409, 'Partner Wallet finance integration is awaiting approval.')


router = APIRouter(tags=['Partner Wallet'])


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class ApplicationBody(Body):
    password: SecretStr = Field(min_length=8, max_length=128)


class ApplicationDecision(Body):
    decision: Literal['approve', 'reject']
    master_partner_id: UUID | None = None
    note: str | None = Field(None, max_length=500)


class AmountBody(Body):
    request_id: UUID
    amount: Decimal = Field(gt=0, max_digits=11, decimal_places=2, allow_inf_nan=False)


class WithdrawalBody(AmountBody):
    bank_name: str = Field(min_length=1, max_length=120)
    branch_name: str = Field(min_length=1, max_length=120)
    account_name: str = Field(min_length=1, max_length=150)
    account_number: str = Field(min_length=3, max_length=40)

    @field_validator('account_number')
    @classmethod
    def account_characters(cls, value):
        if not all(c.isdigit() or c in ' -' for c in value):
            raise ValueError('Enter a valid account number.')
        return value


class WithdrawalDecision(Body):
    decision: Literal['paid', 'reject']
    payment_reference: str | None = Field(None, max_length=160)
    note: str | None = Field(None, max_length=500)


def application_json(row):
    return {'id': str(row.application_id), 'userId': str(row.user_id), 'status': row.status,
        'note': row.decision_note, 'date': row.created_at.isoformat()}


def partner_json(partner):
    return {'id': str(partner.partner_id), 'role': partner.role, 'code': partner.referral_code,
        'active': partner.is_active, 'masterPartnerId': str(partner.parent_partner_id) if partner.parent_partner_id else None}


async def linked_or_404(db, user):
    partner = await Service.linked(db, user.user_id)
    if partner is None:
        raise HTTPException(404, 'This account is not linked to a Partner account.')
    return partner


@router.get('/wallet/partner')
async def customer_wallet(db: AsyncSession = Depends(get_db), user=Depends(get_current_user), ready=Depends(rollout_enabled)):
    partner = await Service.linked(db, user.user_id, active=False)
    app = (await db.execute(select(PartnerApplication).where(PartnerApplication.user_id == user.user_id))).scalar_one_or_none()
    data = {'partner': partner_json(partner) if partner else None, 'application': application_json(app) if app else None}
    if partner and partner.is_active:
        data['wallet'] = await Service.snapshot(db, partner)
    await db.commit()
    return {'success': True, 'data': data}


@router.post('/wallet/partner/apply')
async def apply(body: ApplicationBody, db: AsyncSession = Depends(get_db), user=Depends(get_current_user), ready=Depends(rollout_enabled)):
    row = await Service.apply(db, user.user_id, body.password.get_secret_value())
    await db.commit()
    return {'success': True, 'data': application_json(row)}


@router.post('/wallet/partner/transfer-to-sribees')
async def customer_transfer(body: AmountBody, db: AsyncSession = Depends(get_db), user=Depends(get_current_user), ready=Depends(rollout_enabled)):
    partner = await linked_or_404(db, user)
    await Service.transfer_to_shopping(db, partner, body.amount, body.request_id)
    await db.commit()
    return {'success': True, 'message': 'Transferred to Sribees Wallet.'}


@router.post('/wallet/partner/withdrawals')
async def customer_withdraw(body: WithdrawalBody, db: AsyncSession = Depends(get_db), user=Depends(get_current_user), ready=Depends(rollout_enabled)):
    partner = await linked_or_404(db, user)
    await Service.user_lock(db, user.user_id)
    row = await Service.withdraw(db, partner, body)
    await db.commit()
    return {'success': True, 'data': withdrawal_json(row)}


async def portal_linked(db, partner):
    if not partner.user_id:
        raise HTTPException(409, 'This legacy Partner account must be linked by support before using Partner Wallet.')
    await Service.user_lock(db, partner.user_id)


@router.get('/partner/wallet')
async def portal_wallet(db: AsyncSession = Depends(get_db), partner=Depends(get_current_partner), ready=Depends(rollout_enabled)):
    await portal_linked(db, partner)
    data = await Service.snapshot(db, partner)
    await db.commit()
    return {'success': True, 'data': data}


@router.post('/partner/wallet/transfer-to-sribees')
async def portal_transfer(body: AmountBody, db: AsyncSession = Depends(get_db), partner=Depends(get_current_partner), ready=Depends(rollout_enabled)):
    await portal_linked(db, partner)
    await Service.transfer_to_shopping(db, partner, body.amount, body.request_id)
    await db.commit()
    return {'success': True, 'message': 'Transferred to Sribees Wallet.'}


@router.post('/partner/wallet/withdrawals')
async def portal_withdraw(body: WithdrawalBody, db: AsyncSession = Depends(get_db), partner=Depends(get_current_partner), ready=Depends(rollout_enabled)):
    await portal_linked(db, partner)
    row = await Service.withdraw(db, partner, body)
    await db.commit()
    return {'success': True, 'data': withdrawal_json(row)}


@router.get('/admin/partners/applications')
async def applications(db: AsyncSession = Depends(get_db), admin=Depends(require_super_admin), ready=Depends(rollout_enabled)):
    rows = (await db.execute(select(PartnerApplication, User.full_name, User.email, User.phone)
        .join(User, User.user_id == PartnerApplication.user_id).order_by(PartnerApplication.created_at.desc()).limit(500))).all()
    return {'success': True, 'data': [dict(application_json(r), name=n, email=e, phone=p) for r, n, e, p in rows]}


@router.post('/admin/partners/applications/{application_id}/decision')
async def decide_application(application_id: UUID, body: ApplicationDecision, db: AsyncSession = Depends(get_db), admin=Depends(require_super_admin), ready=Depends(rollout_enabled)):
    partner = await Service.decide_application(db, application_id, admin.admin_id, body.decision == 'approve', body.master_partner_id, body.note)
    await db.commit()
    return {'success': True, 'data': partner_json(partner) if partner else None}


@router.get('/admin/partners/withdrawals')
async def withdrawals(db: AsyncSession = Depends(get_db), admin=Depends(require_super_admin), ready=Depends(rollout_enabled)):
    rows = (await db.execute(select(PartnerWithdrawal).order_by(PartnerWithdrawal.created_at.desc()).limit(500))).scalars().all()
    return {'success': True, 'data': [withdrawal_json(r, admin=True) for r in rows]}


@router.post('/admin/partners/withdrawals/{withdrawal_id}/decision')
async def decide_withdrawal(withdrawal_id: UUID, body: WithdrawalDecision, db: AsyncSession = Depends(get_db), admin=Depends(require_super_admin), ready=Depends(rollout_enabled)):
    row = await Service.decide_withdrawal(db, withdrawal_id, admin.admin_id, body.decision == 'paid', body.payment_reference, body.note)
    await db.commit()
    return {'success': True, 'data': withdrawal_json(row, admin=True)}


@router.get('/admin/partners/{partner_id}/wallet')
async def admin_partner_wallet(partner_id: UUID, db: AsyncSession = Depends(get_db), admin=Depends(require_super_admin), ready=Depends(rollout_enabled)):
    from app.models.partner import Partner
    partner = await db.get(Partner, partner_id)
    if partner is None or not partner.user_id:
        raise HTTPException(404, 'Linked Partner Wallet not found.')
    data = await Service.snapshot(db, partner)
    await db.commit()
    return {'success': True, 'data': data}
