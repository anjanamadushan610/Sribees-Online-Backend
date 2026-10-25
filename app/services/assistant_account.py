"""
Account tools for the assistant (roadmap P4): what a shopper can see or start
in the app -- wallet and points, wishlist, offers, reorder, referrals,
notifications, product details.

Every tool reads the signed-in customer's own data through the same services
the app's screens use; ``user_id`` comes from authentication only, never from
model text. The two that change something -- adding to the wishlist and
reordering into the basket -- only build a preview the customer confirms
(the same token flow as basket and preference previews). Cancellations and
returns are never started here; the assistant points to the right page.

Each tool returns ``{"ui": <chat response pieces>, "data": <lean facts for
the model>}``.
"""

from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select

from app.services.assistant_shopping import (
    cart_preview,
    catalog_product,
    panel,
    price,
    proposal,
    section,
)
from app.services.assistant_tools import action, tr

MAX_ITEMS = 8


def _money(value: Any) -> str:
    amount = float(value or 0)
    return f"{amount:,.0f}" if amount.is_integer() else f"{amount:,.2f}"


def _rs(lang: str) -> str:
    return tr(lang, "Rs", "රු.", "ரூ.")


def login(lang: str) -> Dict[str, Any]:
    return {
        "ui": {
            "reply": tr(
                lang,
                "Sign in to see this for your account.",
                "මේ තොරතුරු බලන්න ඔබේ ගිණුමට පිවිසෙන්න.",
                "இதைப் பார்க்க உங்கள் கணக்கில் உள்நுழையவும்.",
            ),
            "actions": [action("login", lang)],
        },
        "data": {"signed_in": False},
    }


async def wallet_summary(db, user_id: UUID, lang: str) -> Dict[str, Any]:
    from app.services.loyalty_service import LoyaltyService
    from app.services.order_reward_service import OrderRewardService
    from app.services.wallet_service import WalletService

    balance = float(await WalletService.get_balance(db, user_id))
    pending = float(OrderRewardService.pending_total(await OrderRewardService.pending_for_user(db, user_id)))
    loyalty = await LoyaltyService.summary(db, user_id, lang)
    tier = (loyalty.get("tier") or {}).get("name")
    nxt = loyalty.get("next_tier") or {}
    data = {
        "wallet_balance": balance,
        "cashback_on_its_way": pending,
        "points": loyalty.get("points"),
        "level": tier,
        "next_level": nxt.get("name"),
        "points_to_next_level": (nxt.get("min_points") - loyalty.get("points", 0)) if nxt.get("min_points") else None,
    }
    rs = _rs(lang)
    reply = tr(
        lang,
        f"Your wallet has {rs} {_money(balance)}" + (f", with {rs} {_money(pending)} cashback on its way" if pending else "") + f". Points: {data['points']}" + (f" ({tier})" if tier else "") + ".",
        f"ඔබේ wallet එකේ {rs} {_money(balance)}ක් තියෙනවා" + (f", තව {rs} {_money(pending)} cashback එනවා" if pending else "") + f". Points: {data['points']}" + (f" ({tier})" if tier else "") + ".",
        f"உங்கள் wallet-இல் {rs} {_money(balance)} உள்ளது" + (f", மேலும் {rs} {_money(pending)} cashback வருகிறது" if pending else "") + f". Points: {data['points']}" + (f" ({tier})" if tier else "") + ".",
    )
    rows = [(tr(lang, "Balance", "ශේෂය", "இருப்பு"), f"{rs} {_money(balance)}"), (tr(lang, "Points", "Points", "Points"), data["points"])]
    if pending:
        rows.append((tr(lang, "Cashback on its way", "එන cashback", "வரும் cashback"), f"{rs} {_money(pending)}"))
    if data["next_level"] and data["points_to_next_level"] is not None:
        rows.append((tr(lang, "To next level", "ඊළඟ මට්ටමට", "அடுத்த நிலைக்கு"), f"{data['points_to_next_level']} ({data['next_level']})"))
    return {
        "ui": {
            "reply": reply,
            "panels": [panel(tr(lang, "Wallet & points", "Wallet සහ points", "Wallet & points"), [section("", rows)])],
            "actions": [action("wallet", lang)],
        },
        "data": data,
    }


async def _user(db, user_id: UUID):
    from app.models.user import User

    return await db.get(User, user_id)


async def my_wishlist(db, redis, user_id: UUID, lang: str) -> Dict[str, Any]:
    from app.api.v1.wishlist import get_wishlist

    result = await get_wishlist(db=db, redis=redis, current_user=await _user(db, user_id))
    items = (result.get("data") or {}).get("items") or []
    lean = []
    for item in items[:MAX_ITEMS]:
        product = item.get("product") or item
        lean.append({
            "product_id": str(product.get("product_id") or item.get("product_id")),
            "name": product.get("name"),
            "price": product.get("sale_price") or product.get("discount_price") or product.get("price"),
            "watched_at_price": item.get("price_at_watch"),
        })
    reply = (
        tr(lang, f"You have {len(items)} items in your wishlist.", f"ඔබේ wishlist එකේ භාණ්ඩ {len(items)}ක් තියෙනවා.", f"உங்கள் wishlist-இல் {len(items)} பொருட்கள் உள்ளன.")
        if items
        else tr(lang, "Your wishlist is empty.", "ඔබේ wishlist එක හිස්.", "உங்கள் wishlist காலியாக உள்ளது.")
    )
    rows = [(i["name"], f"{_rs(lang)} {_money(i['price'])}") for i in lean if i["name"]]
    return {
        "ui": {"reply": reply, "panels": [panel(tr(lang, "Wishlist", "Wishlist", "Wishlist"), [section("", rows)])] if rows else []},
        "data": {"count": len(items), "items": lean},
    }


async def stage_wishlist_add(db, redis, user_id: UUID, branch_id, product_id: str, lang: str) -> Dict[str, Any]:
    product = await catalog_product(db, branch_id, product_id, lang)
    if not product:
        return {
            "ui": {},
            "data": {"error": "Unknown product_id for this branch. Use an id from search_products, then try again."},
        }
    command = await proposal(
        redis,
        user_id,
        "wishlist_add",
        {"product_id": str(product["product_id"]), "price": price(product)},
        tr(lang, "Add to wishlist", "Wishlist එකට දාන්න", "Wishlist-இல் சேர்க்கவும்"),
    )
    name = product.get("name")
    return {
        "ui": {
            "reply": tr(
                lang,
                f"Add {name} to your wishlist? Tap confirm below.",
                f"{name} wishlist එකට දාන්නද? පහළින් තහවුරු කරන්න.",
                f"{name} wishlist-இல் சேர்க்கவா? கீழே உறுதிசெய்யவும்.",
            ),
            "panels": [panel(tr(lang, "Wishlist", "Wishlist", "Wishlist"), [section("", [(name, f"{_rs(lang)} {_money(price(product))}")])], [command])],
            "products": [product],
        },
        "data": {"preview": True, "product": name, "needs_confirmation": True},
    }


async def my_offers(db, redis, user_id: UUID, lang: str) -> Dict[str, Any]:
    from app.api.v1.coupons import get_available_coupons

    result = await get_available_coupons(db=db, redis=redis, current_user=await _user(db, user_id))
    coupons = (result.get("data") or {}).get("coupons") or []
    lean = [
        {
            k: c.get(k)
            for k in ("code", "title", "description", "discount_type", "discount_value", "min_order_amount", "valid_until", "collected")
            if c.get(k) is not None
        }
        for c in coupons[:MAX_ITEMS]
    ]
    reply = (
        tr(lang, f"{len(coupons)} offers are available for you now.", f"ඔබට දැන් ලබාගත හැකි offers {len(coupons)}ක් තියෙනවා.", f"உங்களுக்கு இப்போது {len(coupons)} சலுகைகள் உள்ளன.")
        if coupons
        else tr(lang, "There are no offers for you right now.", "දැනට ඔබට offers නැහැ.", "இப்போது உங்களுக்கு சலுகைகள் இல்லை.")
    )
    return {"ui": {"reply": reply, "actions": [action("offers", lang)]}, "data": {"count": len(coupons), "offers": lean}}


async def reorder(db, redis, user_id: UUID, branch_id, order_reference: Optional[str], lang: str) -> Dict[str, Any]:
    from app.models.order import Order
    from app.services.order_service import OrderService

    query = select(Order).where(Order.user_id == user_id)
    if order_reference:
        query = query.where(Order.order_number == order_reference.strip().upper())
    order = (await db.execute(query.order_by(Order.created_at.desc()).limit(1))).scalar_one_or_none()
    if order is None:
        return {
            "ui": {},
            "data": {"error": "No order of this customer matches. Ask which order, or use my_orders to list them."},
        }
    lines = await OrderService.reorder_preview(db, order.order_id, user_id)
    available = [line for line in lines if line.get("available")]
    unavailable = [line.get("name") for line in lines if not line.get("available")]
    if not available:
        return {
            "ui": {"reply": tr(lang, "None of that order's items are in stock right now.", "ඒ ඇණවුමේ භාණ්ඩ කිසිවක් දැනට තොගයේ නැහැ.", "அந்த ஆர்டரின் பொருட்கள் எதுவும் இப்போது கையிருப்பில் இல்லை.")},
            "data": {"order": order.order_number, "available": 0, "unavailable": unavailable},
        }
    plan = {
        "operations": [
            {"operation": "add", "product_id": line["product_id"], "variant_id": line.get("variant_id"), "query": line.get("name") or "", "quantity": max(1, int(line.get("quantity") or 1))}
            for line in available[:8]
        ]
    }
    ui = await cart_preview(db, redis, user_id, branch_id, plan, lang)
    return {
        "ui": ui,
        "data": {"order": order.order_number, "available": len(available), "unavailable": unavailable, "needs_confirmation": True},
    }


async def my_referrals(db, user_id: UUID, lang: str) -> Dict[str, Any]:
    from app.services.referral_service import ReferralService

    user = await _user(db, user_id)
    code = await ReferralService.get_or_create_code(db, user)
    totals = await ReferralService.get_summary(db, user.user_id)
    reply = tr(
        lang,
        f"Your invite code is {code}. Share it from the Invite Friends page.",
        f"ඔබේ invite code එක {code}. Invite Friends පිටුවෙන් share කරන්න පුළුවන්.",
        f"உங்கள் அழைப்புக் குறியீடு {code}. Invite Friends பக்கத்திலிருந்து பகிரலாம்.",
    )
    return {"ui": {"reply": reply, "actions": [action("profile", lang)]}, "data": {"code": code, "totals": totals}}


async def my_notifications(db, user_id: UUID, lang: str) -> Dict[str, Any]:
    from app.services.notification_service import NotificationService

    notes, total = await NotificationService.get_by_user(db, user_id, limit=5)
    lean = [
        {"title": getattr(n, "title", None), "message": (getattr(n, "message", None) or "")[:160], "read": bool(getattr(n, "is_read", False))}
        for n in notes
    ]
    unread = sum(1 for n in lean if not n["read"])
    reply = tr(
        lang,
        f"Your latest notifications ({unread} unread) are below.",
        f"ඔබේ අලුත්ම දැනුම්දීම් පහළින් (නොකියවූ {unread}).",
        f"உங்கள் சமீபத்திய அறிவிப்புகள் கீழே ({unread} படிக்காதவை).",
    ) if lean else tr(lang, "You have no notifications.", "ඔබට දැනුම්දීම් නැහැ.", "உங்களுக்கு அறிவிப்புகள் இல்லை.")
    rows = [(n["title"] or "", n["message"]) for n in lean]
    return {
        "ui": {"reply": reply, "panels": [panel(tr(lang, "Notifications", "දැනුම්දීම්", "அறிவிப்புகள்"), [section("", rows)])] if rows else []},
        "data": {"total": total, "latest": lean},
    }


async def product_details(db, branch_id, product_id: str, lang: str) -> Dict[str, Any]:
    product = await catalog_product(db, branch_id, product_id, lang)
    if not product:
        return {"ui": {}, "data": {"error": "Unknown product_id for this branch. Use an id from search_products."}}
    variants: List[dict] = [
        {k: v.get(k) for k in ("variant_id", "name", "price") if v.get(k) is not None}
        for v in (product.get("variants") or [])[:6]
        if isinstance(v, dict)
    ]
    data = {
        "product_id": str(product.get("product_id")),
        "name": product.get("name"),
        "price": price(product),
        "on_sale": bool(product.get("is_on_sale")),
        "cashback_percentage": product.get("cashback_percentage"),
        "returnable": product.get("is_returnable"),
        "description": (product.get("short_description") or product.get("description") or "")[:400],
        "variants": variants,
    }
    return {"ui": {"products": [product]}, "data": data}
