"""
SRIBEESonline - who is using the customer app or the web store, and how much.

The app sends a heartbeat when it opens, every ``BEAT_INTERVAL`` while it
stays in the foreground, and once more when it goes to the background. Each
one carries the foreground seconds since the previous one, so the daily
total is time the customer actually had the app on screen, not time between
launches.

"Online now" = the last heartbeat said foreground and arrived within
``ONLINE_WINDOW``. The background heartbeat takes a customer offline at once;
the window only catches an app that died without saying so.

The web store (www.sribeesonline.com) sends the same heartbeat with platform
``web`` while its tab is on screen; its "install" is one browser, and the
page tells the two apart with ``channel`` ("app" or "web").
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_usage import AppInstall, AppUsageDay
from app.models.user import User

LK_TZ = ZoneInfo("Asia/Colombo")

# The app's heartbeat period while open (lib/core/services/app_presence.dart).
BEAT_INTERVAL = timedelta(minutes=2)
# Two missed beats and a bit: long enough for a slow network, short enough
# that a phone which died without a goodbye drops off the list quickly.
ONLINE_WINDOW = timedelta(minutes=5)
# A single report can carry time saved up while the phone was offline, but
# never more than this -- a clock jump or a forged body cannot add a day.
MAX_SECONDS_PER_BEAT = 3600
# Oldest believable first-install time (the app did not exist before this).
EARLIEST_INSTALL = datetime(2024, 1, 1, tzinfo=timezone.utc)
# Longest daily history returned in one go.
MAX_HISTORY_DAYS = 1000

EVENTS = ("open", "beat", "background")

# The page's App / Web filter.
CHANNELS = ("app", "web")
APP_PLATFORMS = ("android", "ios")

# Most specific first: Edge and Opera also say "Chrome", Chrome also says "Safari".
_BROWSERS = (
    ("Edge", re.compile(r"Edg(e|A|iOS)?/")),
    ("Opera", re.compile(r"OPR/|Opera")),
    ("Samsung Internet", re.compile(r"SamsungBrowser/")),
    ("Firefox", re.compile(r"Firefox/|FxiOS/")),
    ("Chrome", re.compile(r"Chrome/|CriOS/")),
    ("Safari", re.compile(r"Safari/")),
)
_SYSTEMS = (
    ("Android", re.compile(r"Android")),
    ("iPhone", re.compile(r"iPhone|iPod")),
    ("iPad", re.compile(r"iPad")),
    ("Windows", re.compile(r"Windows")),
    ("Mac", re.compile(r"Macintosh|Mac OS X")),
    ("Linux", re.compile(r"Linux|X11")),
)


def browser_label(user_agent: Optional[str]) -> Optional[str]:
    """'Chrome on Windows' from a User-Agent; None when nothing is recognised."""
    if not user_agent:
        return None
    ua = user_agent[:500]
    name = next((n for n, rx in _BROWSERS if rx.search(ua)), None)
    system = next((n for n, rx in _SYSTEMS if rx.search(ua)), None)
    if name and system:
        return f"{name} on {system}"
    return name or system


def _channel_clause(channel: Optional[str]):
    """The App / Web filter as a WHERE clause; None = both."""
    if channel == "web":
        return AppInstall.platform == "web"
    if channel == "app":
        return AppInstall.platform.in_(APP_PLATFORMS)
    return None


def lk_today(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(LK_TZ).date()


def _believable_install(installed_at: Optional[datetime], now: datetime) -> Optional[datetime]:
    if installed_at is None:
        return None
    if installed_at.tzinfo is None:
        installed_at = installed_at.replace(tzinfo=timezone.utc)
    if installed_at < EARLIEST_INSTALL or installed_at > now + timedelta(days=1):
        return None
    return installed_at


async def record_heartbeat(
    db: AsyncSession,
    *,
    device_id: str,
    user_id: Optional[UUID],
    event: str,
    foreground_seconds: int,
    platform: str,
    app_version: Optional[str],
    build_number: Optional[str],
    installed_at: Optional[datetime],
    browser: Optional[str] = None,
    now: Optional[datetime] = None,
) -> None:
    """Upsert the install and add this report to today's usage. Commits."""
    now = now or datetime.now(timezone.utc)
    seconds = max(0, min(int(foreground_seconds), MAX_SECONDS_PER_BEAT))
    if platform == "web":
        # A browser has no install date: its first visit (first_seen_at) stands in.
        installed = None
        browser = (browser or "")[:60] or None
    else:
        installed = _believable_install(installed_at, now)
        browser = None

    stmt = pg_insert(AppInstall).values(
        device_id=device_id,
        user_id=user_id,
        platform=platform,
        app_version=app_version,
        build_number=build_number,
        browser=browser,
        installed_at=installed,
        first_seen_at=now,
        last_seen_at=now,
        is_foreground=event != "background",
    )
    excluded = stmt.excluded
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[AppInstall.device_id],
            set_={
                # A sign-out keeps the last customer: the install is still theirs.
                "user_id": func.coalesce(excluded.user_id, AppInstall.user_id),
                "platform": excluded.platform,
                "app_version": func.coalesce(excluded.app_version, AppInstall.app_version),
                "build_number": func.coalesce(excluded.build_number, AppInstall.build_number),
                "browser": func.coalesce(excluded.browser, AppInstall.browser),
                "installed_at": func.coalesce(excluded.installed_at, AppInstall.installed_at),
                "last_seen_at": excluded.last_seen_at,
                "is_foreground": excluded.is_foreground,
            },
        )
    )

    day = pg_insert(AppUsageDay).values(
        device_id=device_id,
        usage_date=lk_today(now),
        user_id=user_id,
        foreground_seconds=seconds,
        opens=1 if event == "open" else 0,
    )
    await db.execute(
        day.on_conflict_do_update(
            index_elements=[AppUsageDay.device_id, AppUsageDay.usage_date],
            set_={
                "foreground_seconds": AppUsageDay.foreground_seconds + day.excluded.foreground_seconds,
                "opens": AppUsageDay.opens + day.excluded.opens,
                "user_id": func.coalesce(day.excluded.user_id, AppUsageDay.user_id),
            },
        )
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Super Admin reads
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    install: AppInstall
    user: Optional[User]
    today_seconds: int
    total_seconds: int
    days_active: int


def _online_clause(now: datetime):
    return and_(AppInstall.is_foreground.is_(True), AppInstall.last_seen_at >= now - ONLINE_WINDOW)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _serialize(row: _Row, now: datetime, latest_version: Optional[str]) -> dict:
    i, u = row.install, row.user
    online = bool(i.is_foreground and i.last_seen_at and i.last_seen_at >= now - ONLINE_WINDOW)
    return {
        "device_id": i.device_id,
        "user_id": str(i.user_id) if i.user_id else None,
        "customer_name": (u.full_name if u else None) or None,
        "customer_phone": u.phone if u else None,
        "is_guest": u is None,
        "platform": i.platform,
        "browser": i.browser,
        "app_version": i.app_version,
        "build_number": i.build_number,
        # The store's latest version means nothing for a browser.
        "is_latest_version": (
            None
            if i.platform == "web" or not latest_version or not i.app_version
            else i.app_version == latest_version
        ),
        # The phone's own install time; first heartbeat when the phone gave none.
        "installed_at": _iso(i.installed_at or i.first_seen_at),
        "installed_at_is_estimate": i.installed_at is None,
        "first_seen_at": _iso(i.first_seen_at),
        "last_seen_at": _iso(i.last_seen_at),
        "is_online": online,
        "today_seconds": row.today_seconds,
        "total_seconds": row.total_seconds,
        "days_active": row.days_active,
    }


def _base_query(today: date):
    today_seconds = (
        select(func.coalesce(func.sum(AppUsageDay.foreground_seconds), 0))
        .where(AppUsageDay.device_id == AppInstall.device_id, AppUsageDay.usage_date == today)
        .scalar_subquery()
    )
    total_seconds = (
        select(func.coalesce(func.sum(AppUsageDay.foreground_seconds), 0))
        .where(AppUsageDay.device_id == AppInstall.device_id)
        .scalar_subquery()
    )
    days_active = (
        select(func.count())
        .where(AppUsageDay.device_id == AppInstall.device_id, AppUsageDay.foreground_seconds > 0)
        .scalar_subquery()
    )
    return (
        select(AppInstall, User, today_seconds, total_seconds, days_active)
        .outerjoin(User, User.user_id == AppInstall.user_id)
    )


def _rows(result) -> list[_Row]:
    return [_Row(i, u, int(t or 0), int(tot or 0), int(d or 0)) for i, u, t, tot, d in result.all()]


async def overview(
    db: AsyncSession,
    latest_version: Optional[str],
    channel: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Online now, with the headline numbers above it -- for the app, the web, or both."""
    now = now or datetime.now(timezone.utc)
    today = lk_today(now)
    only = _channel_clause(channel)

    def scoped(query):
        return query if only is None else query.where(only)

    online = _rows(
        await db.execute(
            scoped(_base_query(today).where(_online_clause(now)))
            .order_by(AppInstall.last_seen_at.desc())
            .limit(500)
        )
    )
    online_count = await db.scalar(scoped(select(func.count()).select_from(AppInstall).where(_online_clause(now))))
    # Online split by channel whatever the filter, so both counts stay in view.
    online_all = await db.scalar(select(func.count()).select_from(AppInstall).where(_online_clause(now)))
    online_web = await db.scalar(
        select(func.count()).select_from(AppInstall).where(_online_clause(now), AppInstall.platform == "web")
    )
    total_installs = await db.scalar(scoped(select(func.count()).select_from(AppInstall)))
    active_today = await db.scalar(
        scoped(
            select(func.count())
            .select_from(AppUsageDay)
            .join(AppInstall, AppInstall.device_id == AppUsageDay.device_id)
            .where(AppUsageDay.usage_date == today, or_(AppUsageDay.foreground_seconds > 0, AppUsageDay.opens > 0))
        )
    )
    seconds_today = await db.scalar(
        scoped(
            select(func.coalesce(func.sum(AppUsageDay.foreground_seconds), 0))
            .select_from(AppUsageDay)
            .join(AppInstall, AppInstall.device_id == AppUsageDay.device_id)
            .where(AppUsageDay.usage_date == today)
        )
    )
    # Versions among phones seen in the last 30 days: a phone nobody has
    # opened for months says nothing about who still needs to update. A
    # browser has no app version, so the web never appears here.
    versions = []
    if channel != "web":
        versions = (
            await db.execute(
                select(AppInstall.app_version, func.count())
                .where(
                    AppInstall.last_seen_at >= now - timedelta(days=30),
                    AppInstall.platform.in_(APP_PLATFORMS),
                )
                .group_by(AppInstall.app_version)
                .order_by(func.count().desc())
            )
        ).all()

    return {
        "channel": channel,
        "online_count": int(online_count or 0),
        "online_app_count": int((online_all or 0) - (online_web or 0)),
        "online_web_count": int(online_web or 0),
        "online": [_serialize(r, now, latest_version) for r in online],
        "total_installs": int(total_installs or 0),
        "active_today": int(active_today or 0),
        "seconds_today": int(seconds_today or 0),
        "latest_version": latest_version,
        "versions": [{"version": v or "unknown", "installs": int(c)} for v, c in versions],
        "online_window_minutes": int(ONLINE_WINDOW.total_seconds() // 60),
        "generated_at": now.isoformat(),
    }


async def list_installs(
    db: AsyncSession,
    *,
    latest_version: Optional[str],
    search: Optional[str] = None,
    channel: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    now: Optional[datetime] = None,
) -> dict:
    """Every install (and web browser), most recently seen first."""
    now = now or datetime.now(timezone.utc)
    query = _base_query(lk_today(now))
    count_query = select(func.count()).select_from(AppInstall).outerjoin(User, User.user_id == AppInstall.user_id)
    only = _channel_clause(channel)
    if only is not None:
        query = query.where(only)
        count_query = count_query.where(only)
    if search and search.strip():
        like = f"%{search.strip()}%"
        cond = or_(
            User.full_name.ilike(like),
            User.phone.ilike(like),
            AppInstall.app_version.ilike(like),
            AppInstall.browser.ilike(like),
            AppInstall.device_id.ilike(like),
        )
        query = query.where(cond)
        count_query = count_query.where(cond)

    total = int(await db.scalar(count_query) or 0)
    rows = _rows(
        await db.execute(
            query.order_by(AppInstall.last_seen_at.desc()).offset((page - 1) * limit).limit(limit)
        )
    )
    return {
        "installs": [_serialize(r, now, latest_version) for r in rows],
        "total": total,
        "page": page,
        "limit": limit,
    }


async def install_history(
    db: AsyncSession, device_id: str, *, latest_version: Optional[str], now: Optional[datetime] = None
) -> Optional[dict]:
    """One install, with a row for every day from its download to today."""
    now = now or datetime.now(timezone.utc)
    today = lk_today(now)
    found = _rows(await db.execute(_base_query(today).where(AppInstall.device_id == device_id)))
    if not found:
        return None
    row = found[0]

    start = (row.install.installed_at or row.install.first_seen_at).astimezone(LK_TZ).date()
    start = max(start, today - timedelta(days=MAX_HISTORY_DAYS - 1))
    recorded = {
        d.usage_date: d
        for d in (
            await db.execute(
                select(AppUsageDay).where(AppUsageDay.device_id == device_id, AppUsageDay.usage_date >= start)
            )
        ).scalars()
    }
    # Days before the first heartbeat were never measured: say so rather than
    # showing them as zero use.
    measured_from = row.install.first_seen_at.astimezone(LK_TZ).date()
    days = []
    d = today
    while d >= start:
        rec = recorded.get(d)
        days.append({
            "date": d.isoformat(),
            "seconds": rec.foreground_seconds if rec else 0,
            "opens": rec.opens if rec else 0,
            "measured": d >= measured_from,
        })
        d -= timedelta(days=1)

    return {"install": _serialize(row, now, latest_version), "days": days}
