"""Real database regression for invitation consumption on a linked account."""
import pytest
from fastapi import HTTPException

from app.core.security import verify_password
from app.services.partner_invitation import accept_invitation
from tests.conftest import requires_db
from tests.test_linked_partner_wallet import linked_partner
from tests.test_linked_partner_wallet import money_db as money_db
from tests.test_partner_invitation import token_for


@requires_db
@pytest.mark.asyncio
async def test_password_setup_consumes_invitation_without_changing_referrals(money_db):  # noqa: F811
    db, user = money_db
    partner = await linked_partner(db, user)
    code = partner.referral_code
    token = token_for(partner)
    await accept_invitation(db, token, 'Customer chosen password 19!')
    await db.flush()
    await db.refresh(partner)
    assert verify_password('Customer chosen password 19!', partner.password_hash)
    assert partner.user_id == user.user_id
    assert partner.referral_code == code
    with pytest.raises(HTTPException):
        await accept_invitation(db, token, 'Different password 19!')
