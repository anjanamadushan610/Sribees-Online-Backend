"""Consume a synthetic Online development probe inside an outer rollback."""
import asyncio
import json
import sys
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_engine
from app.models import HEAD_OFFICE_ID, Account, Journal, JournalLine, ProcessedEvent
from app.services.feed_consumer import book
from app.services.online_feed import entry_date
from app.services.reconciliation import finance_totals


async def main():
    if get_settings().environment != 'development':
        raise RuntimeError('This probe is permitted only in development')
    bundle = json.load(sys.stdin)
    assert bundle['source_rolled_back'] is True
    events = bundle['events']
    assert len(events) == 11
    ids = [UUID(e['event_id']) for e in events]
    deltas = {'6110': Decimal(85), '2130': Decimal(0), '2136': Decimal(-45),
              '2137': Decimal(0), '2140': Decimal(0), '2100': Decimal(-30), '2135': Decimal(-10)}
    engine = get_engine()
    async with engine.connect() as conn:
        outer = await conn.begin()
        try:
            def sessions():
                return AsyncSession(bind=conn, expire_on_commit=False,
                    join_transaction_mode='create_savepoint')
            async def balances():
                q = select(Account.code, func.sum(JournalLine.debit - JournalLine.credit)).join(
                    JournalLine, JournalLine.account_id == Account.account_id).join(
                    Journal, Journal.journal_id == JournalLine.journal_id).where(
                    Journal.status == 'posted', JournalLine.branch_id == HEAD_OFFICE_ID,
                    Account.code.in_(deltas)).group_by(Account.code)
                return dict((await conn.execute(q)).all())
            before = await balances()
            day = entry_date(events[0]['occurred_at'])
            async with sessions() as db:
                prior = dict((await finance_totals(db, day)).get(HEAD_OFFICE_ID, {}))
            outcomes = [await book(sessions, e) for e in events]
            assert 'exception' not in outcomes, outcomes
            after = await balances()
            for code, amount in deltas.items():
                assert after.get(code, 0) - before.get(code, 0) == amount, code
            async with sessions() as db:
                totals = (await finance_totals(db, day))[HEAD_OFFICE_ID]
            for key, expected in {'partner_wallet_net': 45, 'partner_wallet_count': 6,
                    'wallet_net': 30, 'wallet_count': 1, 'partner_withdrawal_paid': 10,
                    'partner_withdrawal_count': 1}.items():
                assert totals[key] - prior.get(key, 0) == expected, key
            assert [await book(sessions, e) for e in events] == ['duplicate'] * 11
            assert await balances() == after
        finally:
            await outer.rollback()
        assert await conn.scalar(select(func.count()).select_from(ProcessedEvent).where(
            ProcessedEvent.event_id.in_(ids))) == 0
    await engine.dispose()
    print(json.dumps({'result': 'PASS', 'events': 11, 'source_records_rolled_back': True,
        'finance_records_rolled_back': True, 'replay': 'PASS', 'daily_totals': 'PASS',
        'source_checks': bundle['source_checks']}))


if __name__ == '__main__':
    asyncio.run(main())
