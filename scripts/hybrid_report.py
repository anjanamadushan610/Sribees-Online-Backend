"""What the assistant did over the last N days, from assistant_turns. No model calls.

Run inside the backend (it reads the database):

    python -m scripts.hybrid_report --days 14

The same numbers the admin page shows (app/services/hybrid/insights.py).
"""

import argparse
import asyncio
import json


async def report(days: int) -> dict:
    from app.config.database import async_session_maker
    from app.services.hybrid.insights import summary

    async with async_session_maker() as db:
        return await summary(db, days)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(report(args.days)), indent=2, ensure_ascii=False, default=str))
