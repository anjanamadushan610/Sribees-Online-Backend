"""Read-only production offer configuration, excluding customer information."""
import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import func, select, text

import app.models  # noqa: F401
from app.config.database import async_session_maker, engine
from app.models.referral_promotion import ReferralBenefit, ReferralPromotion
from app.services.referral_promotion_service import enabled


async def main():
    try:
        async with async_session_maker() as db:
            await db.execute(text('SET TRANSACTION READ ONLY'))
            now = datetime.now(timezone.utc)
            rows = (await db.execute(select(ReferralPromotion))).scalars().all()
            print(json.dumps({'referral_execution_enabled': enabled(), 'campaign_count': len(rows)}))
            for row in rows:
                print(json.dumps({'campaign': row.name, 'scope': row.scope, 'active': row.active,
                    'within_dates': row.starts_at <= now < row.ends_at,
                    'starts_at': row.starts_at.isoformat(), 'ends_at': row.ends_at.isoformat(),
                    'delivery_uses': row.rules.get('delivery_uses'), 'free_delivery': row.rules.get('free_delivery'),
                    'valid_days': row.rules.get('valid_days'), 'min_order': row.rules.get('min_order'),
                    'max_delivery_fee': row.rules.get('max_delivery_fee'),
                    'max_customers': row.max_customers, 'enrolled_count': row.enrolled_count,
                    'delivery_budget': str(row.delivery_budget), 'delivery_spent': str(row.delivery_spent)}))
            benefits = (await db.execute(select(func.count(ReferralBenefit.benefit_id)))).scalar_one()
            print(json.dumps({'total_granted_customer_benefits': benefits}))
            await db.rollback()
    finally:
        await engine.dispose()


asyncio.run(main())
