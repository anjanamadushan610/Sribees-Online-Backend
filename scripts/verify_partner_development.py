"""Development-only business-service probe. All synthetic rows are rolled back."""
import asyncio
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import settings


async def main():
    if settings.app_env != 'development':
        raise RuntimeError('This probe is permitted only in development')
    logger.remove()
    logging.disable(logging.CRITICAL)
    settings.database_echo = False
    settings.finance_outbox_enabled = True  # this process only; no server rollout flag changes
    from app.api.v1.linked_partner_wallet import WithdrawalBody
    from app.models.finance_outbox import FinanceOutbox
    from app.models.order import Order
    from app.models.referral import ReferralCommission
    from app.models.user import User
    from app.models.wallet import Wallet
    from app.services import finance_outbox
    from app.services.partner_wallet_service import PartnerWalletService as service

    finance_outbox.install()
    engine = create_async_engine(settings.async_database_url, poolclass=NullPool)
    uid, friend_id, transfer_id = uuid4(), uuid4(), uuid4()
    code = 'P' + uuid4().hex[:7].upper()
    bundle = None
    async with engine.connect() as conn:
        outer = await conn.begin()
        try:
            async with AsyncSession(bind=conn, expire_on_commit=False,
                    join_transaction_mode='create_savepoint') as db:
                user = User(user_id=uid, email=f'{uid}@example.invalid', full_name='Development probe',
                    referral_code=code, is_active=True, is_verified=True, is_blocked=False)
                friend = User(user_id=friend_id, email=f'{friend_id}@example.invalid',
                    full_name='Development probe friend', referred_by_user_id=uid)
                db.add(user)
                await db.flush()
                db.add_all([Wallet(user_id=uid, balance=0), friend])
                await db.flush()
                order = Order(user_id=friend_id, order_number=f'PROBE-{uuid4().hex}',
                    subtotal=1000, total_amount=1000, status='delivered')
                db.add(order)
                await db.flush()
                commission = ReferralCommission(order_id=order.order_id, beneficiary_user_id=uid,
                    source_user_id=friend_id, level=1, rate_percentage=10, order_subtotal=1000,
                    amount=100, status='claimable',
                    claimable_at=datetime.now(timezone.utc) - timedelta(days=1))
                db.add(commission)
                await db.flush()
                application = await service.apply(db, uid, secrets.token_urlsafe(24))
                partner = await service.decide_application(db, application.application_id, uuid4(), True)
                await db.refresh(friend)
                assert partner.user_id == uid and partner.referral_code == code
                assert partner.role == 'partner' and friend.referred_by_partner_id == partner.partner_id
                await service.transfer_to_shopping(db, partner, Decimal('30.00'), transfer_id)
                await service.transfer_to_shopping(db, partner, Decimal('30.00'), transfer_id)
                def request(amount):
                    return WithdrawalBody(request_id=uuid4(), amount=amount, bank_name='Synthetic only',
                        branch_name='Synthetic', account_name='Synthetic', account_number='000000')
                rejected = await service.withdraw(db, partner, request('20.00'))
                await service.decide_withdrawal(db, rejected.withdrawal_id, uuid4(), False, None, 'Probe')
                paid = await service.withdraw(db, partner, request('10.00'))
                await service.decide_withdrawal(db, paid.withdrawal_id, uuid4(), True, 'PROBE-NOT-A-PAYMENT', None)
                await db.refresh(commission)
                commission.amount = Decimal('85.00')
                await service.adjust_commission(db, commission, Decimal('15.00'))
                await db.flush()
                wallet = await service.wallet_lock(db, partner.partner_id)
                assert wallet.balance == Decimal('45.00')
                assert await db.scalar(select(Wallet.balance).where(Wallet.user_id == uid)) == Decimal('30.00')
                rows = list((await db.scalars(select(FinanceOutbox).where(or_(
                    FinanceOutbox.aggregate_id.in_([partner.partner_id, order.order_id]),
                    FinanceOutbox.payload['partner_transfer_id'].astext == str(transfer_id)
                )).order_by(FinanceOutbox.seq))).all())
                rows = [e for e in rows if e.event_type != 'order.status']
                assert len(rows) == 11
                bundle = {'events': [{'seq': e.seq, 'event_id': str(e.event_id), 'type': e.event_type,
                    'aggregate_id': str(e.aggregate_id) if e.aggregate_id else None,
                    'occurred_at': e.occurred_at.isoformat(), 'payload': e.payload} for e in rows],
                    'source_checks': ['customer_upgrade', 'invite_code_preserved', 'referrals_migrated',
                        'one_way_transfer_and_retry', 'withdrawal_rejected', 'synthetic_paid_record',
                        'commission_clawback'], 'source_rolled_back': False}
        finally:
            await outer.rollback()
        assert await conn.scalar(select(User.user_id).where(User.user_id == uid)) is None
        assert await conn.scalar(select(User.user_id).where(User.user_id == friend_id)) is None
    await engine.dispose()
    bundle['source_rolled_back'] = True
    print(json.dumps(bundle))


if __name__ == '__main__':
    asyncio.run(main())
