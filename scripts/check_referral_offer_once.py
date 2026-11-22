import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, text

import app.models  # noqa: F401
from app.api.v1.referral_promotions import CampaignBody, Rules, save
from app.config.database import get_db_context, engine
from app.models.referral_promotion import ReferralPromotion


async def main():
    try:
        async with get_db_context() as db:
            await db.execute(text('SELECT pg_advisory_xact_lock(1811120026)'))
            name = 'Partner Invite – 10 Free Deliveries'
            existing = await db.scalar(select(ReferralPromotion).where(ReferralPromotion.name == name))
            if existing:
                raise SystemExit('Campaign already exists. No change made.')
            campaign = CampaignBody(
                name=name, scope='all', active=False,
                starts_at=datetime.now(timezone.utc),
                ends_at=datetime.fromisoformat('2027-01-01T00:00:00+05:30'),
                max_customers=5000, delivery_budget=100000,
                rules=Rules(free_delivery=True, delivery_uses=10, valid_days=60,
                            min_order=1000, max_delivery_fee=2500),
            )
            print((await save(db, campaign))['data'])
    finally:
        await engine.dispose()


asyncio.run(main())
