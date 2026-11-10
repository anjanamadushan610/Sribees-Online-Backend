"""Announce committed customer grants using the existing localized FCM pipeline."""

from zoneinfo import ZoneInfo

from loguru import logger

from app.schemas.notification import NotificationTypeEnum
from app.services import notification_i18n as i18n
from app.services.notification_service import NotificationService


def grant_params(lang, *, min_order, branch_name):
    scopes = {
        "en": f"Valid at {branch_name} branch." if branch_name else "Valid at all branches.",
        "si": f"{branch_name} ශාඛාවට අදාළයි." if branch_name else "සියලු ශාඛාවලට අදාළයි.",
        "ta": f"{branch_name} கிளையில் செல்லுபடியாகும்." if branch_name else "அனைத்து கிளைகளிலும் செல்லுபடியாகும்.",
    }
    amount = i18n.money(min_order)
    minimums = {
        "en": f" Minimum order value: {amount}.",
        "si": f" අවම ඇණවුම් වටිනාකම: {amount}.",
        "ta": f" குறைந்தபட்ச ஆர்டர் மதிப்பு: {amount}.",
    }
    return {"scope": scopes[lang], "minimum": minimums[lang] if min_order > 0 else ""}


async def notify_grants(db, grants, *, ends_at, min_order, branch_name):
    """One offer notification per new grant, after all grants have committed.

    Grants are immutable (user_id, grant_id) pairs: a notification rollback
    must not expire ORM grant objects needed for the rest of a batch.
    A stored notification is not a guarantee that FCM delivered to a phone.
    """
    notified = 0
    expires = ends_at.astimezone(ZoneInfo("Asia/Colombo")).strftime("%Y-%m-%d %H:%M")
    for user_id, grant_id in grants:
        try:
            notification = await NotificationService.notify(
                db,
                user_id,
                template="free_delivery.granted",
                type=NotificationTypeEnum.PROMOTION,
                category="offers",
                params={"expires": expires},
                per_lang_params=lambda lang: grant_params(
                    lang, min_order=min_order, branch_name=branch_name
                ),
                reference_type="free_delivery_grant",
                reference_id=grant_id,
                data={"type": "promotion", "ends_at": ends_at.isoformat()},
            )
            notified += notification is not None
        except Exception:
            await db.rollback()
            logger.exception(f"Could not notify customer {user_id} of free delivery grant {grant_id}")
    return notified
