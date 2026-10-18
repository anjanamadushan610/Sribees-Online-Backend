"""
SRIBEESonline — keep the postal city directory in step with SribeesExpress.

Every delivery quote is keyed on the ``courier_postal_city_id`` the directory
sync fills in. That sync used to run only when someone pressed "Sync postal
cities" in the admin, so a server whose courier key was set but whose button
was never pressed priced no address at all: every cart said "not serviceable"
while the courier itself was answering fine. Nothing in that failure points at
the button.

So the sync now runs by itself:

* shortly after startup, and then every ``courier_postal_city_sync_hours``,
  which also picks up districts SribeesExpress opens later;
* right after an admin saves a courier key, base URL or environment, so the
  setup works on the next cart instead of the next loop.

Several server processes run this loop. A Postgres advisory lock lets one of
them sync and the others skip that round: two concurrent syncs would race to
insert the same new postal city, and one would fail on the unique name.
"""
import asyncio

from loguru import logger
from sqlalchemy import text

from app.config.database import async_session_maker, engine
from app.config.settings import settings
from app.core.crypto import DecryptionError
from app.services import courier_coverage_service
from app.services.courier_credentials import CourierNotConfiguredError

# Arbitrary, fixed: "only one postal city sync at a time" across processes.
_ADVISORY_LOCK_KEY = 7_311_205_901
STARTUP_DELAY_SECONDS = 60


def _enabled() -> bool:
    # Test runs must not call SribeesExpress from a background task.
    return settings.app_env.strip().lower() != "testing"


async def sync_once(reason: str) -> bool:
    """
    One sync, if this process wins the lock. Never raises.

    Returns whether a sync ran. An unconfigured courier is not an error here --
    a fresh deployment has no key yet -- and is logged quietly.
    """
    async with engine.connect() as lock_conn:
        postgres = lock_conn.dialect.name == "postgresql"
        if postgres:
            got = await lock_conn.scalar(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
            )
            await lock_conn.commit()
            if not got:
                logger.info(f"Postal city sync ({reason}) skipped: another process is syncing")
                return False
        try:
            async with async_session_maker() as db:
                await courier_coverage_service.sync(db)
            return True
        except (CourierNotConfiguredError, DecryptionError) as exc:
            logger.info(f"Postal city sync ({reason}) skipped: {exc}")
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                f"Postal city sync ({reason}) failed; addresses keep the ids they "
                "already have, and new towns stay unquotable until the next run."
            )
            return False
        finally:
            if postgres:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY}
                )
                await lock_conn.commit()


# The loop holds only weak references to tasks; an unreferenced one can be
# collected mid-flight.
_pending: set[asyncio.Task] = set()


def sync_soon(reason: str) -> None:
    """Start a sync without making the caller wait for SribeesExpress."""
    if not _enabled():
        return
    task = asyncio.get_running_loop().create_task(sync_once(reason))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def run_postal_city_worker():
    hours = settings.courier_postal_city_sync_hours
    if not _enabled():
        return
    if hours <= 0:
        logger.info("Postal city sync loop is off (COURIER_POSTAL_CITY_SYNC_HOURS=0)")
        return
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        await sync_once("scheduled")
        await asyncio.sleep(hours * 3600)
