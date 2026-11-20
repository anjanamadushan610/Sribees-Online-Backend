"""Read existing free-delivery rules without creating or redeeming benefits."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from app.config.redis import get_redis_client
from app.models.free_delivery import FreeDeliveryGrant
from app.services import branch_service, free_delivery_service
from app.services.referral_promotion_service import ReferralPromotionService
from app.services.whatsapp_content import copy, translated


async def free_delivery_summary(db, user, language):
    now = datetime.now(timezone.utc)
    lines = []
    grants = (
        (
            await db.execute(
                select(FreeDeliveryGrant)
                .where(
                    FreeDeliveryGrant.user_id == user.user_id,
                    FreeDeliveryGrant.revoked_at.is_(None),
                    FreeDeliveryGrant.starts_at <= now,
                    FreeDeliveryGrant.ends_at > now,
                )
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    for grant in grants:
        lines.append(
            copy(
                "grant",
                language,
                date=grant.ends_at.strftime("%Y-%m-%d"),
                minimum=f"{grant.min_order:.2f}",
            )
        )
    redis = get_redis_client()
    context = (
        await branch_service.get_branch_context(redis, str(user.user_id))
        if redis
        else None
    )
    if context and context.get("branch_id"):
        offer = await free_delivery_service.live_offer(db, UUID(context["branch_id"]))
        if offer:
            label = translated(
                (
                    "Selected branch free delivery",
                    "තෝරාගත් ශාඛාවේ නොමිලේ බෙදාහැරීම",
                    "தேர்ந்தெடுத்த கிளையில் இலவச விநியோகம்",
                ),
                language,
            )
            minimum = translated(
                ("Minimum order", "අවම ඇණවුම", "குறைந்தபட்ச ஆர்டர்"), language
            )
            lines.append(
                f"{label}\n{free_delivery_service.offer_ends_at(offer).strftime('%Y-%m-%d')}\n{minimum} LKR {offer.min_order:.2f}"
            )
    benefit = await ReferralPromotionService.customer_summary(db, user.user_id)
    if benefit and not benefit["expired"] and benefit["freeDelivery"]:
        limit = benefit["deliveryUses"]
        remaining = (
            max(0, limit - benefit["usedDeliveries"]) if limit is not None else None
        )
        if remaining is None or remaining > 0:
            label = translated(
                (
                    "Referral delivery benefit",
                    "හඳුන්වාදීමේ බෙදාහැරීම් දීමනාව",
                    "பரிந்துரை விநியோகச் சலுகை",
                ),
                language,
            )
            minimum = translated(
                ("Minimum order", "අවම ඇණවුම", "குறைந்தபட்ச ஆர்டர்"), language
            )
            maximum = translated(
                (
                    "Maximum delivery fee",
                    "උපරිම බෙදාහැරීමේ ගාස්තුව",
                    "அதிகபட்ச விநியோக கட்டணம்",
                ),
                language,
            )
            lines.append(
                f"{label}\n{benefit['expiresAt'][:10]}\n{minimum} LKR {benefit['minOrder']}\n{maximum} LKR {benefit['maxDeliveryFee']}"
            )
            if remaining is not None:
                label = translated(
                    ("Remaining uses", "ඉතිරි වාර", "மீதமுள்ள பயன்பாடுகள்"), language
                )
                lines[-1] += f"\n{label} {remaining}"
    return "\n\n".join(lines) if lines else copy("no_grant", language)
