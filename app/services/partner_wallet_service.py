"""Account upgrade and one-way partner money operations; callers own commit/rollback.

Lock order: user -> commission rows -> partner wallet -> shopping wallet.
Never infer account ownership from a matching email. Applications originate
from an authenticated customer, and only a Super Admin can approve them.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.core.security import hash_password
from app.models.partner import Partner
from app.models.partner_wallet import (
    PartnerApplication,
    PartnerWallet,
    PartnerWalletEntry,
    PartnerWithdrawal,
)
from app.models.referral import ReferralCommission, ReferralCommissionStatus
from app.models.user import User
from app.models.wallet import WalletTransactionType
from app.services.wallet_service import WalletService


def money(value) -> Decimal:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > Decimal('999999999.99') or amount.as_tuple().exponent < -2:
            raise ValueError
        return amount.quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(400, 'Enter a positive amount with at most two decimal places.')


def fail(message, status=400):
    raise HTTPException(status, message)


class PartnerWalletService:
    @staticmethod
    async def linked(db, user_id, *, active=True):
        partner = (await db.execute(select(Partner).where(Partner.user_id == user_id))).scalar_one_or_none()
        if partner and active and not partner.is_active:
            fail('Partner account is inactive.', 403)
        return partner

    @staticmethod
    async def user_lock(db, user_id):
        user = (await db.execute(select(User).where(User.user_id == user_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if user is None or not user.is_active or user.is_blocked or user.deleted_at is not None:
            fail('An active customer account is required.', 403)
        return user

    @staticmethod
    async def wallet_lock(db, partner_id):
        await db.execute(insert(PartnerWallet).values(partner_id=partner_id, balance=0).on_conflict_do_nothing(index_elements=['partner_id']))
        return (await db.execute(select(PartnerWallet).where(PartnerWallet.partner_id == partner_id).with_for_update().execution_options(populate_existing=True))).scalar_one()

    @staticmethod
    async def entry(db, wallet, reference, kind, amount):
        previous = (await db.execute(select(PartnerWalletEntry).where(
            PartnerWalletEntry.partner_id == wallet.partner_id, PartnerWalletEntry.reference == reference))).scalar_one_or_none()
        if previous:
            if previous.kind != kind or Decimal(str(previous.amount)) != amount:
                fail('This request was already used for a different operation.', 409)
            return previous
        wallet.balance = Decimal(str(wallet.balance)) + amount
        row = PartnerWalletEntry(partner_id=wallet.partner_id, reference=reference, kind=kind, amount=amount, balance_after=wallet.balance)
        db.add(row)
        await db.flush()
        from app.services.finance_outbox import partner_wallet_movement
        await partner_wallet_movement(db, row)
        return row

    @staticmethod
    async def apply(db, user_id, password):
        from app.services.referral_service import ReferralService
        candidate = await db.get(User, user_id)
        if candidate is not None:
            await ReferralService.get_or_create_code(db, candidate)
        user = await PartnerWalletService.user_lock(db, user_id)
        if not user.email or not user.full_name:
            fail('Add your email and full name to My Account before applying.')
        if await PartnerWalletService.linked(db, user_id, active=False):
            fail('Your customer account is already linked to a Partner account.', 409)
        existing = (await db.execute(select(PartnerApplication).where(PartnerApplication.user_id == user_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if existing and existing.status == 'pending':
            return existing
        collision = (await db.execute(select(Partner.partner_id).where(func.lower(Partner.email) == user.email.lower()))).scalar_one_or_none()
        if collision:
            fail('A Partner account already uses this email. Contact support to verify ownership; it cannot be linked automatically.', 409)
        row = existing or PartnerApplication(user_id=user_id)
        row.password_hash = hash_password(password)
        row.status = 'pending'
        row.decision_by = row.decision_note = row.decided_at = None
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def decide_application(db, application_id, admin_id, approve, master_id=None, note=None):
        # Read user id first, then use the same user lock as referral claims.
        initial = await db.get(PartnerApplication, application_id)
        if initial is None:
            fail('Application not found.', 404)
        user = await PartnerWalletService.user_lock(db, initial.user_id)
        app = (await db.execute(select(PartnerApplication).where(PartnerApplication.application_id == application_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
        if app.status != 'pending':
            fail('This application has already been decided.', 409)
        partner = None
        if approve:
            if await PartnerWalletService.linked(db, user.user_id, active=False):
                fail('Customer is already linked.', 409)
            collision = (await db.execute(select(Partner.partner_id).where(func.lower(Partner.email) == user.email.lower()))).scalar_one_or_none()
            if collision:
                fail('Partner email is already in use; resolve ownership first.', 409)
            if master_id:
                master = await db.get(Partner, master_id)
                if master is None or not master.is_active or master.role != 'master_partner' or master.parent_partner_id is not None or master.user_id == user.user_id:
                    fail('Choose an active Master Partner.')
            code = user.referral_code
            if not code:
                fail('Open Invite Friends before submitting your application.')
            # A legacy code can be shared by this linked user/partner pair only.
            occupied = (await db.execute(select(Partner.partner_id).where(Partner.referral_code == code))).scalar_one_or_none()
            if occupied:
                fail('Invite code is already assigned to a Partner. Contact support.', 409)
            partner = Partner(user_id=user.user_id, role='partner', email=user.email.lower(),
                full_name=user.full_name, phone=user.phone, password_hash=app.password_hash,
                parent_partner_id=master_id, referral_code=code, is_active=True)
            db.add(partner)
            await db.flush()
            # Preserve referral dates and the identity of every introduced customer.
            await db.execute(update(User).where(User.referred_by_user_id == user.user_id).values(
                referred_by_user_id=None, referred_by_partner_id=partner.partner_id))
            # Old shopping credits stay untouched. Only unpaid entitlements move.
            await db.execute(update(ReferralCommission).where(
                ReferralCommission.beneficiary_user_id == user.user_id,
                ReferralCommission.status.in_(['pending', 'claimable'])
            ).values(beneficiary_user_id=None, beneficiary_partner_id=partner.partner_id))
            await PartnerWalletService.credit_available(db, partner.partner_id)
        app.status = 'approved' if approve else 'rejected'
        app.password_hash = None
        app.decision_by, app.decision_note, app.decided_at = admin_id, note, datetime.now(timezone.utc)
        await db.flush()
        return partner

    @staticmethod
    async def credit_available(db, partner_id):
        partner = await db.get(Partner, partner_id)
        if not partner or not partner.user_id:
            return 0
        rows = (await db.execute(select(ReferralCommission).where(
            ReferralCommission.beneficiary_partner_id == partner_id,
            ReferralCommission.status == ReferralCommissionStatus.CLAIMABLE,
            ReferralCommission.claimable_at.isnot(None),
            ReferralCommission.claimable_at <= datetime.now(timezone.utc)
        ).order_by(ReferralCommission.commission_id).with_for_update().execution_options(populate_existing=True))).scalars().all()
        if not rows:
            return 0
        wallet = await PartnerWalletService.wallet_lock(db, partner_id)
        for row in rows:
            await PartnerWalletService.entry(db, wallet, f'commission:{row.commission_id}', 'commission', Decimal(str(row.amount)))
            row.status, row.claimed_at = ReferralCommissionStatus.PAID, datetime.now(timezone.utc)
        return len(rows)

    @staticmethod
    async def adjust_commission(db, row, difference):
        """Claw back only commissions that were credited to this wallet."""
        if not row.beneficiary_partner_id or difference <= 0:
            return
        credit = (await db.execute(select(PartnerWalletEntry.entry_id).where(
            PartnerWalletEntry.partner_id == row.beneficiary_partner_id,
            PartnerWalletEntry.reference == f'commission:{row.commission_id}'))).scalar_one_or_none()
        if credit:
            wallet = await PartnerWalletService.wallet_lock(db, row.beneficiary_partner_id)
            await PartnerWalletService.entry(db, wallet, f'adjust:{row.commission_id}:{row.amount}', 'commission_reversal', -Decimal(str(difference)))

    @staticmethod
    async def transfer_to_shopping(db, partner, amount, request_id):
        amount = money(amount)
        await PartnerWalletService.user_lock(db, partner.user_id)
        wallet = await PartnerWalletService.wallet_lock(db, partner.partner_id)
        reference = f'shopping:{request_id}'
        previous = (await db.execute(select(PartnerWalletEntry).where(
            PartnerWalletEntry.partner_id == partner.partner_id, PartnerWalletEntry.reference == reference))).scalar_one_or_none()
        if previous:
            if previous.amount != -amount:
                fail('Request already used with a different amount.', 409)
            return
        if wallet.balance < amount:
            fail('Insufficient available Partner Wallet balance.')
        # Both wallets and both ledger entries commit together. There is no reverse API.
        shopping = await WalletService.get_or_create_wallet(db, partner.user_id)
        await db.refresh(shopping, with_for_update=True)
        await PartnerWalletService.entry(db, wallet, reference, 'shopping_transfer', -amount)
        await WalletService.apply_transaction(db, partner.user_id, WalletTransactionType.EARNED, amount,
            'Transfer from Partner Wallet', notes=str(request_id), reason='partner_wallet_transfer')

    @staticmethod
    async def withdraw(db, partner, body):
        amount = money(body.amount)
        wallet = await PartnerWalletService.wallet_lock(db, partner.partner_id)
        previous = (await db.execute(select(PartnerWithdrawal).where(
            PartnerWithdrawal.partner_id == partner.partner_id, PartnerWithdrawal.request_id == body.request_id))).scalar_one_or_none()
        if previous:
            for field in ['amount', 'bank_name', 'branch_name', 'account_name', 'account_number']:
                if getattr(previous, field) != getattr(body, field):
                    fail('Request already used for different withdrawal details.', 409)
            return previous
        if wallet.balance < amount:
            fail('Insufficient available Partner Wallet balance.')
        row = PartnerWithdrawal(partner_id=partner.partner_id, request_id=body.request_id, amount=amount,
            bank_name=body.bank_name, branch_name=body.branch_name, account_name=body.account_name, account_number=body.account_number)
        db.add(row)
        await db.flush()
        await PartnerWalletService.entry(db, wallet, f'withdrawal:{row.withdrawal_id}', 'withdrawal_reserved', -amount)
        return row

    @staticmethod
    async def decide_withdrawal(db, withdrawal_id, admin_id, paid, reference, note):
        initial = await db.get(PartnerWithdrawal, withdrawal_id)
        if initial is None:
            fail('Withdrawal not found.', 404)
        wallet = await PartnerWalletService.wallet_lock(db, initial.partner_id)
        row = (await db.execute(select(PartnerWithdrawal).where(PartnerWithdrawal.withdrawal_id == withdrawal_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
        if row.status != 'pending':
            fail('Withdrawal already decided.', 409)
        if paid and not (reference or '').strip():
            fail('Enter the bank payment reference after completing the transfer.')
        if not paid:
            await PartnerWalletService.entry(db, wallet, f'rejected:{withdrawal_id}', 'withdrawal_released', Decimal(str(row.amount)))
        row.status = 'paid' if paid else 'rejected'
        row.decision_by, row.payment_reference, row.decision_note = admin_id, reference, note
        row.decided_at = datetime.now(timezone.utc)
        if paid:
            from app.services.finance_outbox import partner_withdrawal_paid
            await partner_withdrawal_paid(db, row)
        return row

    @staticmethod
    async def snapshot(db, partner):
        await PartnerWalletService.credit_available(db, partner.partner_id)
        wallet = await PartnerWalletService.wallet_lock(db, partner.partner_id)
        entries = (await db.execute(select(PartnerWalletEntry).where(PartnerWalletEntry.partner_id == partner.partner_id)
            .order_by(PartnerWalletEntry.created_at.desc()).limit(100))).scalars().all()
        withdrawals = (await db.execute(select(PartnerWithdrawal).where(PartnerWithdrawal.partner_id == partner.partner_id)
            .order_by(PartnerWithdrawal.created_at.desc()).limit(100))).scalars().all()
        pending = (await db.execute(select(func.coalesce(func.sum(ReferralCommission.amount), 0)).where(
            ReferralCommission.beneficiary_partner_id == partner.partner_id, ReferralCommission.status == 'pending'))).scalar_one()
        return {'balance': str(wallet.balance), 'pendingCommission': str(pending), 'currency': 'LKR',
            'entries': [{'id': str(r.entry_id), 'kind': r.kind, 'amount': str(r.amount), 'balanceAfter': str(r.balance_after), 'date': r.created_at.isoformat()} for r in entries],
            'withdrawals': [withdrawal_json(r) for r in withdrawals]}


def withdrawal_json(row, *, admin=False):
    result = {'id': str(row.withdrawal_id), 'partnerId': str(row.partner_id), 'amount': str(row.amount),
        'status': row.status, 'bankName': row.bank_name, 'accountName': row.account_name,
        'accountNumber': row.account_number if admin else '••••' + row.account_number[-4:],
        'branchName': row.branch_name, 'date': row.created_at.isoformat(),
        'paymentReference': row.payment_reference, 'note': row.decision_note}
    return result
