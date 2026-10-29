"""Money and ownership invariants. Real Postgres tests roll back all fixtures."""
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1.linked_partner_wallet import AmountBody, WithdrawalBody, rollout_enabled
from app.models.partner import Partner
from app.models.partner_wallet import PartnerWallet, PartnerWalletEntry
from app.models.user import User
from app.models.wallet import Wallet
from app.services.partner_wallet_service import PartnerWalletService as Service
from app.services.partner_wallet_service import money
from app.services.referral_service import ReferralError, ReferralService
from tests.conftest import requires_db


def test_invalid_money_and_reverse_payload_are_rejected():
    for invalid in ['0', '-1', 'NaN', 'Infinity', '1.001', '1000000000']:
        with pytest.raises(HTTPException):
            money(invalid)
    assert money('10.50') == Decimal('10.50')
    with pytest.raises(ValidationError):
        AmountBody(request_id=uuid4(), amount='10', direction='sribees_to_partner')


def test_rollout_is_closed_by_default(monkeypatch):
    monkeypatch.delenv('LINKED_PARTNER_WALLET_ENABLED', raising=False)
    with pytest.raises(HTTPException) as error:
        rollout_enabled()
    assert error.value.status_code == 503


@pytest.fixture
async def money_db(db_schema):
    from app.config.settings import settings
    engine = create_async_engine(settings.async_database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        outer = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False, join_transaction_mode='create_savepoint') as db:
            suffix = uuid4().hex[:8]
            user = User(email=f'{suffix}@example.test', full_name='Kamal Test', referral_code=f'T{suffix}',
                        is_active=True, is_verified=True, is_blocked=False)
            db.add(user)
            await db.flush()
            db.add(Wallet(user_id=user.user_id, balance=Decimal('100.00')))
            await db.flush()
            yield db, user
        await outer.rollback()
    await engine.dispose()


async def linked_partner(db, user):
    partner = Partner(user_id=user.user_id, role='partner', email=user.email, full_name=user.full_name,
                      password_hash='unused-test-hash', referral_code=user.referral_code, is_active=True)
    db.add(partner)
    await db.flush()
    db.add(PartnerWallet(partner_id=partner.partner_id, balance=Decimal('100.00')))
    await db.flush()
    return partner


@requires_db
@pytest.mark.asyncio
async def test_only_approved_upgrade_preserves_code_customers_and_old_credit(money_db):
    db, user = money_db
    friend = User(email=f'{uuid4().hex}@example.test', full_name='Friend', referred_by_user_id=user.user_id)
    db.add(friend)
    await db.flush()
    code = user.referral_code
    application = await Service.apply(db, user.user_id, 'test-application-password')
    assert await Service.linked(db, user.user_id) is None
    partner = await Service.decide_application(db, application.application_id, uuid4(), True)
    await db.refresh(friend)
    assert partner.user_id == user.user_id
    assert partner.role == 'partner'  # no assigned master does NOT grant recruitment
    assert partner.referral_code == code == user.referral_code
    assert friend.referred_by_partner_id == partner.partner_id
    assert friend.referred_by_user_id is None
    assert await db.scalar(select(Wallet.balance).where(Wallet.user_id == user.user_id)) == Decimal('100.00')
    with pytest.raises(HTTPException):
        await Service.decide_application(db, application.application_id, uuid4(), True)
    with pytest.raises(ReferralError):
        await ReferralService.bind_referrer(db, user, code, commit=False)


@requires_db
@pytest.mark.asyncio
async def test_one_way_transfer_is_atomic_idempotent_and_cannot_overspend(money_db):
    db, user = money_db
    partner = await linked_partner(db, user)
    request_id = uuid4()
    await Service.transfer_to_shopping(db, partner, Decimal('30.00'), request_id)
    await Service.transfer_to_shopping(db, partner, Decimal('30.00'), request_id)
    assert await db.scalar(select(Wallet.balance).where(Wallet.user_id == user.user_id)) == Decimal('130.00')
    assert await db.scalar(select(PartnerWallet.balance).where(PartnerWallet.partner_id == partner.partner_id)) == Decimal('70.00')
    with pytest.raises(HTTPException):
        await Service.transfer_to_shopping(db, partner, Decimal('31.00'), request_id)
    with pytest.raises(HTTPException):
        await Service.transfer_to_shopping(db, partner, Decimal('71.00'), uuid4())


@requires_db
@pytest.mark.asyncio
async def test_withdrawal_reserves_money_and_rejection_releases_once(money_db):
    db, user = money_db
    partner = await linked_partner(db, user)
    body = WithdrawalBody(request_id=uuid4(), amount='90.00', bank_name='Test Bank', branch_name='Test',
                          account_name='Kamal Test', account_number='123456')
    withdrawal = await Service.withdraw(db, partner, body)
    assert (await Service.withdraw(db, partner, body)).withdrawal_id == withdrawal.withdrawal_id
    with pytest.raises(HTTPException):
        await Service.transfer_to_shopping(db, partner, Decimal('11.00'), uuid4())
    await Service.decide_withdrawal(db, withdrawal.withdrawal_id, uuid4(), False, None, 'Rejected test')
    assert await db.scalar(select(PartnerWallet.balance).where(PartnerWallet.partner_id == partner.partner_id)) == Decimal('100.00')
    with pytest.raises(HTTPException):
        await Service.decide_withdrawal(db, withdrawal.withdrawal_id, uuid4(), False, None, 'Retry')


@requires_db
@pytest.mark.asyncio
async def test_paid_withdrawal_does_not_debit_twice_and_requires_bank_reference(money_db):
    db, user = money_db
    partner = await linked_partner(db, user)
    row = await Service.withdraw(db, partner, WithdrawalBody(request_id=uuid4(), amount='20.00',
        bank_name='Test', branch_name='Test', account_name='Test', account_number='123456'))
    with pytest.raises(HTTPException):
        await Service.decide_withdrawal(db, row.withdrawal_id, uuid4(), True, '', None)
    await Service.decide_withdrawal(db, row.withdrawal_id, uuid4(), True, 'test-bank-reference', None)
    assert await db.scalar(select(PartnerWallet.balance).where(PartnerWallet.partner_id == partner.partner_id)) == Decimal('80.00')
    assert row.status == 'paid'


@requires_db
@pytest.mark.asyncio
async def test_partner_clawback_never_debits_shopping_credit(money_db):
    db, user = money_db
    partner = await linked_partner(db, user)
    wallet = await Service.wallet_lock(db, partner.partner_id)
    commission = SimpleNamespace(commission_id=uuid4(), beneficiary_partner_id=partner.partner_id, amount=Decimal('25.00'))
    await Service.entry(db, wallet, f'commission:{commission.commission_id}', 'commission', Decimal('30.00'))
    await Service.adjust_commission(db, commission, Decimal('5.00'))
    await Service.adjust_commission(db, commission, Decimal('5.00'))
    assert wallet.balance == Decimal('125.00')
    assert await db.scalar(select(Wallet.balance).where(Wallet.user_id == user.user_id)) == Decimal('100.00')
    entries = (await db.execute(select(PartnerWalletEntry).where(PartnerWalletEntry.partner_id == partner.partner_id))).scalars().all()
    assert len(entries) == 2


@requires_db
@pytest.mark.asyncio
async def test_failed_shopping_credit_rolls_back_partner_debit(money_db, monkeypatch):
    from app.services.wallet_service import WalletService
    db, user = money_db
    partner = await linked_partner(db, user)

    async def fail_credit(*args, **kwargs):
        raise RuntimeError('Simulated shopping ledger failure')

    monkeypatch.setattr(WalletService, 'apply_transaction', fail_credit)
    with pytest.raises(RuntimeError):
        async with db.begin_nested():
            await Service.transfer_to_shopping(db, partner, Decimal('30.00'), uuid4())
    assert await db.scalar(select(PartnerWallet.balance).where(PartnerWallet.partner_id == partner.partner_id)) == Decimal('100.00')
    assert await db.scalar(select(Wallet.balance).where(Wallet.user_id == user.user_id)) == Decimal('100.00')
