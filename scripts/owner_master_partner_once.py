"""One-off owner-authorized provisioning. Sensitive inputs arrive on stdin only."""
import asyncio
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import func, select

import app.models  # noqa: F401
from app.config.database import async_session_maker, engine
from app.models.partner import Partner
from app.models.user import User
from app.services.audit_service import AuditLog
from app.services.partner_wallet_service import PartnerWalletService
from app.services.referral_service import ReferralService


async def run(payload):
    async with async_session_maker() as db:
        async with db.begin():
            user = (await db.execute(select(User).where(
                User.nic == payload['nic'], User.deleted_at.is_(None)
            ).with_for_update())).scalar_one()
            if (user.email or '').strip().lower() != payload['email'].lower():
                raise RuntimeError('NIC/email mismatch; no changes applied')
            if not user.is_active or user.is_blocked:
                raise RuntimeError('Customer is inactive or blocked; no changes applied')
            linked = await PartnerWalletService.linked(db, user.user_id, active=False)
            collision = (await db.execute(select(Partner).where(
                func.lower(Partner.email) == payload['email'].lower()
            ).with_for_update())).scalar_one_or_none()
            if collision is not None and (linked is None or collision.partner_id != linked.partner_id):
                raise RuntimeError('Email belongs to a different Partner; no changes applied')
            if linked and not linked.is_active:
                raise RuntimeError('Linked Partner is inactive; no changes applied')
            print(json.dumps({'identity_match': True, 'name': user.full_name,
                              'existing_role': linked.role if linked else 'customer',
                              'mode': 'apply' if payload['apply'] else 'read-only'}), flush=True)
            if not payload['apply']:
                return
            if not payload['password_hash'].startswith('$argon2id$'):
                raise RuntimeError('Expected Argon2id password hash')
            old_role = linked.role if linked else 'customer'
            if linked is None:
                await ReferralService.get_or_create_code(db, user, commit=False)
                linked = await PartnerWalletService._create_linked_partner(
                    db, user, user.referred_by_partner_id, payload['password_hash'])
                await PartnerWalletService.wallet_lock(db, linked.partner_id)
            elif linked.role not in ('partner', 'master_partner'):
                raise RuntimeError('Unexpected existing role')
            linked.role = 'master_partner'
            linked.password_hash = payload['password_hash']
            linked.password_changed_at = datetime.now(timezone.utc)
            db.add(AuditLog(
                action='partner.owner_requested_master_provision', actor_type='system',
                target_type='partner', target_id=linked.partner_id,
                description='Account owner explicitly requested backend promotion and supplied portal credentials in Codex chat.',
                event_metadata={'previous_role': old_role, 'new_role': 'master_partner',
                                'customer_link_preserved': True, 'password_set': True}))
            await db.flush()
            partner_id = linked.partner_id
            user_id = user.user_id
        # Verify committed data using a new query after the transaction completes.
        db.expire_all()
        partner = (await db.execute(select(Partner).where(Partner.partner_id == partner_id))).scalar_one()
        assert partner.user_id == user_id and partner.role == 'master_partner'
        assert partner.is_active and partner.password_hash == payload['password_hash']
        print(json.dumps({'committed': True, 'name': partner.full_name, 'role': partner.role,
                          'customer_link_preserved': True, 'password_hash_verified': True}), flush=True)


if __name__ == '__main__':
    async def main():
        try:
            await run(json.load(sys.stdin))
        except Exception as exc:
            print(json.dumps({'failed': True, 'error_type': type(exc).__name__,
                              'message': 'Operation refused or failed; credentials and identity inputs omitted.'}), flush=True)
            raise SystemExit(1) from None
        finally:
            await engine.dispose()
    asyncio.run(main())
