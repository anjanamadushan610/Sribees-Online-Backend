"""
SRIBEESonline - AppSettings Service

CRUD operations for the ``app_settings`` table.
Provides specialised helpers for the splash-video use-case.
"""
from decimal import Decimal, InvalidOperation
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.app_settings import AppSetting

# Well-known setting keys
SPLASH_VIDEO_KEY = "splash_video_url"
# Stored as a human-friendly percentage (e.g. "15" = 15%). Converted to a
# fraction for PricingService in get_pricing_config().
ORDER_TAX_RATE_PERCENT_KEY = "order_tax_rate_percent"
# --- Mobile remote config ---------------------------------------------------
# The Play Store freezes the app: once a build is submitted, changing anything
# in it means another review, which is days. These keys are how the running
# app is steered without a new build -- a kill switch for a broken feature, a
# maintenance notice instead of a screen of errors, and the ability to require
# an update once a fixed build is actually published.
MOBILE_MIN_VERSION_KEY = "mobile_minimum_version"
MOBILE_LATEST_VERSION_KEY = "mobile_latest_version"
MOBILE_UPDATE_URL_KEY = "mobile_update_url"
MOBILE_MAINTENANCE_ACTIVE_KEY = "mobile_maintenance_active"
MOBILE_MAINTENANCE_MESSAGE_KEY = "mobile_maintenance_message"
MOBILE_FEATURE_FLAGS_KEY = "mobile_feature_flags"

# There is deliberately no delivery fee setting. The delivery charge is
# SribeesExpress's quote for each parcel (see PricingService); migration 052
# deleted the `flat_delivery_fee` / `delivery_fee` rows an administrator used
# to set, so nothing can quietly reintroduce a guessed price.


def _to_float(value: Optional[str], fallback: float) -> float:
    """Parse a stored string setting to float, falling back on any error."""
    if value is None or value == "":
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


class AppSettingsService:
    """Service for runtime application settings."""

    # ------------------------------------------------------------------
    # Generic key/value CRUD
    # ------------------------------------------------------------------

    @staticmethod
    async def get_by_key(db: AsyncSession, key: str) -> Optional[AppSetting]:
        """Fetch a single setting by key."""
        result = await db.execute(
            select(AppSetting).where(AppSetting.key == key)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def upsert(
        db: AsyncSession,
        key: str,
        value: Optional[str],
        description: Optional[str] = None,
        is_active: bool = True,
    ) -> AppSetting:
        """Create or update a setting by key."""
        setting = await AppSettingsService.get_by_key(db, key)
        if setting is None:
            setting = AppSetting(
                key=key,
                value=value,
                description=description,
                is_active=is_active,
            )
            db.add(setting)
        else:
            setting.value = value
            if description is not None:
                setting.description = description
            setting.is_active = is_active

        await db.commit()
        await db.refresh(setting)
        logger.info(f"AppSetting upserted: {key}")
        return setting

    # ------------------------------------------------------------------
    # Mobile remote config
    # ------------------------------------------------------------------

    @staticmethod
    async def get_mobile_config(db: AsyncSession) -> dict:
        """
        What the shipped app is allowed to be told at launch.

        Every value has a default that means "carry on normally", so an empty
        settings table -- or a database that cannot be read at all -- leaves
        the app working. Failing the other way would turn one bad row into a
        total outage for every installed copy, which is exactly the situation
        this exists to prevent.

        ``minimum_version`` is the only hard gate. Setting it above what a
        customer has installed stops that copy until they update, so it is
        raised only once a fixed build is actually live on the store -- raising
        it first locks people out of an app that has no newer version to go to.
        """
        import json as _json

        rows = {}
        for key in (
            MOBILE_MIN_VERSION_KEY,
            MOBILE_LATEST_VERSION_KEY,
            MOBILE_UPDATE_URL_KEY,
            MOBILE_MAINTENANCE_ACTIVE_KEY,
            MOBILE_MAINTENANCE_MESSAGE_KEY,
            MOBILE_FEATURE_FLAGS_KEY,
        ):
            row = await AppSettingsService.get_by_key(db, key)
            rows[key] = row.value if row and row.is_active else None

        flags = {}
        raw_flags = rows[MOBILE_FEATURE_FLAGS_KEY]
        if raw_flags:
            try:
                parsed = _json.loads(raw_flags)
                if isinstance(parsed, dict):
                    flags = parsed
            except (ValueError, TypeError):
                # A typo in the admin form must not take the app down. Log it
                # and serve no flags, which is the same as every feature being
                # in its normal state.
                logger.warning("mobile_feature_flags is not valid JSON - ignoring")

        return {
            # "0.0.0" blocks nobody. A missing row must never lock users out.
            "minimum_version": rows[MOBILE_MIN_VERSION_KEY] or "0.0.0",
            "latest_version": rows[MOBILE_LATEST_VERSION_KEY] or None,
            "update_url": rows[MOBILE_UPDATE_URL_KEY] or None,
            "maintenance": {
                # Only the exact string "true" turns it on, so a malformed
                # value leaves the app running rather than dark.
                "active": (rows[MOBILE_MAINTENANCE_ACTIVE_KEY] or "").strip().lower() == "true",
                "message": rows[MOBILE_MAINTENANCE_MESSAGE_KEY] or None,
            },
            "flags": flags,
            "splash": await AppSettingsService._mobile_splash(db),
        }

    @staticmethod
    async def _mobile_splash(db: AsyncSession) -> Optional[dict]:
        """
        The remote splash animation, or None for the built-in one. A broken
        row costs the custom splash, never the rest of the config: this runs
        inside the payload that decides whether the app may start at all.
        """
        # Imported here: splash_animation builds on this service.
        from app.services import splash_animation

        try:
            value, row = await splash_animation.get_splash_animation(db)
            return splash_animation.public_splash(value, bool(row and row.is_active))
        except Exception as exc:
            logger.warning(f"splash_animation could not be read - using the built-in splash: {exc}")
            return None

    # ------------------------------------------------------------------
    # Splash video helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def get_splash_video(db: AsyncSession) -> Optional[AppSetting]:
        """Get the splash video setting."""
        return await AppSettingsService.get_by_key(db, SPLASH_VIDEO_KEY)

    @staticmethod
    async def set_splash_video_url(
        db: AsyncSession,
        url: str,
        is_active: bool = True,
    ) -> AppSetting:
        """Set (or update) the splash video URL."""
        return await AppSettingsService.upsert(
            db,
            key=SPLASH_VIDEO_KEY,
            value=url,
            description="URL of the splash-screen animation video shown when the Flutter app opens",
            is_active=is_active,
        )

    # ------------------------------------------------------------------
    # Platform settings (checkout pricing + mobile app config)
    # ------------------------------------------------------------------

    @staticmethod
    async def get_platform_settings(db: AsyncSession) -> dict:
        """
        Return the human-facing platform settings, falling back to the static
        config defaults (``app.config.settings``) when a key is unset in the DB.

        ``order_tax_rate_percent`` is a percentage (15.0 = 15%); the config
        default ``order_tax_rate`` is a fraction, so it is scaled ×100 here.
        """
        tax_row = await AppSettingsService.get_by_key(db, ORDER_TAX_RATE_PERCENT_KEY)
        splash_row = await AppSettingsService.get_by_key(db, SPLASH_VIDEO_KEY)

        return {
            "order_tax_rate_percent": _to_float(
                tax_row.value if tax_row else None, float(settings.order_tax_rate) * 100
            ),
            "splash_video_url": splash_row.value if splash_row else None,
        }

    @staticmethod
    async def get_pricing_config(db: AsyncSession) -> dict:
        """
        Pricing values for :class:`PricingService`, derived from platform
        settings. ``tax_rate`` is returned as a fraction (0.15 = 15%).
        """
        ps = await AppSettingsService.get_platform_settings(db)
        try:
            tax_fraction = Decimal(str(ps["order_tax_rate_percent"])) / Decimal("100")
        except (InvalidOperation, TypeError):
            tax_fraction = Decimal(str(settings.order_tax_rate))
        return {
            "tax_rate": tax_fraction,
        }

    @staticmethod
    async def update_platform_settings(
        db: AsyncSession,
        *,
        order_tax_rate_percent: Optional[float] = None,
        splash_video_url: Optional[str] = None,
    ) -> dict:
        """Upsert any provided platform settings; returns the fresh values."""
        if order_tax_rate_percent is not None:
            await AppSettingsService.upsert(
                db,
                key=ORDER_TAX_RATE_PERCENT_KEY,
                value=str(order_tax_rate_percent),
                description="Tax rate applied to the discounted subtotal, as a percentage",
            )
        if splash_video_url is not None:
            await AppSettingsService.set_splash_video_url(db, splash_video_url)

        return await AppSettingsService.get_platform_settings(db)
