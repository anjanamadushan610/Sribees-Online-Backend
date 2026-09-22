"""Trusted, localized assistant tools. The model selects intents, never routes or SQL."""

import asyncio
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.order import Order, OrderStatusHistory
from app.services import courier_sync_service


def tr(lang: str, en: str, si: str, ta: str) -> str:
    return {"en": en, "si": si, "ta": ta}.get(lang, en)


# These IDs are shared with the mobile action allowlist. No model-authored URLs.
DESTINATIONS = {
    "orders": ("My orders", "මගේ ඇණවුම්", "எனது ஆர்டர்கள்"),
    "cart": ("My basket", "මගේ භාණ්ඩ මල්ල", "எனது கூடை"),
    "checkout": ("Basket and checkout", "භාණ්ඩ මල්ල සහ ඇණවුම", "கூடை மற்றும் செக்அவுட்"),
    "addresses": ("Delivery addresses", "බෙදාහැරීමේ ලිපින", "விநியோக முகவரிகள்"),
    "wallet": ("Cashback wallet", "මුදල් ආපසු ලැබීම්", "கேஷ்பேக் பணப்பை"),
    "offers": ("Offers", "දීමනා", "சலுகைகள்"),
    "settings": ("Settings", "සැකසුම්", "அமைப்புகள்"),
    "support": ("Help and support", "උදව් ලබාගන්න", "உதவி பெறுங்கள்"),
    "profile": ("My account", "මගේ ගිණුම", "எனது கணக்கு"),
    "login": ("Sign in", "පිවිසෙන්න", "உள்நுழையவும்"),
}


def action(target: str, lang: str, order_id: Optional[str] = None) -> dict:
    if order_id:
        return {
            "type": "navigate",
            "target": "order_details",
            "order_id": order_id,
            "label": tr(lang, "View order", "ඇණවුම බලන්න", "ஆர்டரைப் பார்க்கவும்"),
        }
    return {
        "type": "navigate",
        "target": target,
        "label": tr(lang, *DESTINATIONS[target]),
    }


_GUIDES = {
    "checkout": [
        (
            "Choose your delivery location and add products to your basket.",
            "බෙදාහැරීමේ ස්ථානය තෝරා අවශ්‍ය භාණ්ඩ භාණ්ඩ මල්ලට එකතු කරන්න.",
            "விநியோக இடத்தைத் தேர்ந்தெடுத்து பொருட்களைக் கூடையில் சேர்க்கவும்.",
        ),
        (
            "Open your basket, select the items to order, and review quantities and prices.",
            "භාණ්ඩ මල්ල විවෘත කර ඇණවුම් කරන භාණ්ඩ තෝරන්න. ප්‍රමාණ සහ මිල පරීක්ෂා කරන්න.",
            "கூடையைத் திறந்து ஆர்டர் செய்யும் பொருட்களைத் தேர்ந்தெடுக்கவும். அளவுகளையும் விலைகளையும் சரிபார்க்கவும்.",
        ),
        (
            "Continue to checkout and choose your delivery address and payment method.",
            "ඇණවුම සම්පූර්ණ කිරීමට ගොස් බෙදාහැරීමේ ලිපිනය සහ ගෙවීම් ක්‍රමය තෝරන්න.",
            "செக்அவுட்டில் விநியோக முகவரியையும் பணம் செலுத்தும் முறையையும் தேர்ந்தெடுக்கவும்.",
        ),
        (
            "Review the final total and place the order when you are ready.",
            "අවසන් මුදල පරීක්ෂා කර ඔබ සූදානම් වූ විට ඇණවුම තහවුරු කරන්න.",
            "இறுதித் தொகையைச் சரிபார்த்து தயாரானதும் ஆர்டர் செய்யவும்.",
        ),
    ],
    "orders": [
        (
            "Open My orders and select an order.",
            "මගේ ඇණවුම් විවෘත කර ඇණවුමක් තෝරන්න.",
            "எனது ஆர்டர்களில் ஓர் ஆர்டரைத் தேர்ந்தெடுக்கவும்.",
        ),
        (
            "View its status, items and delivery information.",
            "එහි තත්ත්වය, භාණ්ඩ සහ බෙදාහැරීමේ තොරතුරු බලන්න.",
            "நிலை, பொருட்கள், விநியோக விவரங்களைப் பார்க்கவும்.",
        ),
        (
            "To buy again, use Reorder in My orders. For returns, open a delivered order and choose Return Items. Contact support for cancellation help.",
            "ආපසු මිලදී ගන්න මගේ ඇණවුම් පිටුවේ නැවත ඇණවුම් කරන්න තෝරන්න. භාණ්ඩ ආපසු දෙන්න බෙදාහැර අවසන් ඇණවුමක් විවෘත කර භාණ්ඩ ආපසු දීම තෝරන්න. අවලංගු කිරීමට සහාය සේවාව අමතන්න.",
            "மீண்டும் வாங்க எனது ஆர்டர்களில் மீண்டும் ஆர்டர் செய் என்பதைத் தேர்ந்தெடுக்கவும். திருப்பி அனுப்ப, விநியோகித்த ஆர்டரைத் திறந்து பொருட்களைத் திருப்பி அனுப்பு என்பதைத் தேர்ந்தெடுக்கவும். ரத்து செய்ய உதவியை அணுகவும்.",
        ),
    ],
    "addresses": [
        (
            "Open your delivery addresses.",
            "බෙදාහැරීමේ ලිපින විවෘත කරන්න.",
            "விநியோக முகவரிகளைத் திறக்கவும்.",
        ),
        (
            "Add or edit an address and check the location and contact details.",
            "ලිපිනයක් එකතු කර හෝ සංස්කරණය කර ස්ථානය සහ සම්බන්ධතා විස්තර පරීක්ෂා කරන්න.",
            "முகவரியைச் சேர்க்கவும் அல்லது திருத்தவும்; இடம் மற்றும் தொடர்பு விவரங்களைச் சரிபார்க்கவும்.",
        ),
        (
            "Save it, then select it at checkout for your next order.",
            "එය සුරකින්න. ඊළඟ ඇණවුම තහවුරු කරන විට එම ලිපිනය තෝරන්න.",
            "சேமித்து அடுத்த ஆர்டரின் செக்அவுட்டில் அதைத் தேர்ந்தெடுக்கவும்.",
        ),
    ],
    "wallet": [
        (
            "Open your cashback history to see earned credit and transactions.",
            "ලැබුණු මුදල් සහ ගනුදෙනු බලන්න මුදල් ආපසු ලැබීම් විවෘත කරන්න.",
            "பெற்ற தொகை மற்றும் பரிவர்த்தனைகளைப் பார்க்க கேஷ்பேக் வரலாற்றைத் திறக்கவும்.",
        ),
        (
            "At checkout, review the available wallet credit and final payable total.",
            "ඇණවුම තහවුරු කරන විට භාවිත කළ හැකි ශේෂය සහ ගෙවිය යුතු අවසන් මුදල පරීක්ෂා කරන්න.",
            "செக்அவுட்டில் கிடைக்கும் பணப்பைத் தொகையையும் இறுதிக் கட்டணத்தையும் சரிபார்க்கவும்.",
        ),
    ],
    "cart": [
        (
            "Open your basket and adjust product quantities or remove unwanted items.",
            "භාණ්ඩ මල්ල විවෘත කර භාණ්ඩ ප්‍රමාණ වෙනස් කරන්න හෝ අනවශ්‍ය භාණ්ඩ ඉවත් කරන්න.",
            "கூடையைத் திறந்து பொருட்களின் அளவை மாற்றவும் அல்லது தேவையற்றவற்றை நீக்கவும்.",
        ),
        (
            "Select the items you want to buy, review the total, and continue to checkout.",
            "මිලදී ගන්නා භාණ්ඩ තෝරා එකතුව පරීක්ෂා කර ඇණවුම සම්පූර්ණ කිරීමට යන්න.",
            "வாங்க வேண்டிய பொருட்களைத் தேர்ந்தெடுத்து மொத்தத்தைச் சரிபார்த்து செக்அவுட்டிற்குச் செல்லவும்.",
        ),
    ],
    "settings": [
        (
            "Open Settings. In Language, choose Sinhala, Tamil or English.",
            "සැකසුම් විවෘත කරන්න. භාෂාව යටතේ සිංහල, දෙමළ හෝ ඉංග්‍රීසි තෝරන්න.",
            "அமைப்புகளைத் திறந்து மொழியில் சிங்களம், தமிழ் அல்லது ஆங்கிலத்தைத் தேர்ந்தெடுக்கவும்.",
        ),
        (
            "For account security, use the available security options on that page. Never share your password or OTP in chat.",
            "ගිණුමේ ආරක්ෂාව සඳහා එම පිටුවේ ඇති ආරක්ෂක විකල්ප භාවිත කරන්න. මුරපදය හෝ OTP අංකය කතාබහට එවන්න එපා.",
            "கணக்குப் பாதுகாப்பிற்கு அங்குள்ள பாதுகாப்பு விருப்பங்களைப் பயன்படுத்தவும். கடவுச்சொல் அல்லது OTP எண்ணை அரட்டையில் பகிர வேண்டாம்.",
        ),
    ],
    "offers": [
        (
            "Open Offers and check the available coupons and their conditions.",
            "දීමනා විවෘත කර තිබෙන කූපන් සහ ඒවායේ කොන්දේසි බලන්න.",
            "சலுகைகளைத் திறந்து கிடைக்கும் கூப்பன்களையும் அவற்றின் நிபந்தனைகளையும் பார்க்கவும்.",
        ),
        (
            "Collect a coupon, then apply an eligible coupon from the basket or checkout and check the updated total.",
            "කූපනයක් ලබාගෙන භාණ්ඩ මල්ලෙන් හෝ ඇණවුම තහවුරු කරන තැනින් සුදුසු කූපනය යොදන්න. වෙනස් වූ එකතුව පරීක්ෂා කරන්න.",
            "கூப்பனைப் பெற்றுப் பின் கூடை அல்லது செக்அவுட்டில் தகுதியான கூப்பனைப் பயன்படுத்தி புதிய மொத்தத்தைச் சரிபார்க்கவும்.",
        ),
    ],
    "profile": [
        (
            "Open My account to see your profile and saved addresses.",
            "ඔබේ තොරතුරු සහ සුරැකි ලිපින බලන්න මගේ ගිණුම විවෘත කරන්න.",
            "சுயவிவரம் மற்றும் சேமித்த முகவரிகளைப் பார்க்க எனது கணக்கைத் திறக்கவும்.",
        ),
        (
            "Tap the name or email edit option, update the details and save. Use the address controls to add or edit an address.",
            "නම හෝ ඊමේල් සංස්කරණ විකල්පය තෝරා විස්තර වෙනස් කර සුරකින්න. ලිපිනයක් එකතු කිරීමට හෝ වෙනස් කිරීමට ලිපින විකල්ප භාවිත කරන්න.",
            "பெயர் அல்லது மின்னஞ்சலைத் திருத்தி சேமிக்கவும். முகவரியைச் சேர்க்க அல்லது திருத்த முகவரி விருப்பங்களைப் பயன்படுத்தவும்.",
        ),
    ],
    "support": [
        (
            "Open Help and support and select an available contact option.",
            "උදව් සහ සහාය විවෘත කර ඇති සම්බන්ධතා ක්‍රමයක් තෝරන්න.",
            "உதவி மற்றும் ஆதரவைத் திறந்து கிடைக்கும் தொடர்பு முறையைத் தேர்ந்தெடுக்கவும்.",
        ),
        (
            "Describe the issue and provide the order number if relevant. Support can check cancellation or refund requests; this chat has not changed your order.",
            "ගැටලුව සහ අදාළ නම් ඇණවුම් අංකය සඳහන් කරන්න. අවලංගු කිරීමේ හෝ මුදල් ආපසු ඉල්ලීම් සහාය සේවාවට පරීක්ෂා කළ හැකියි. මේ කතාබහෙන් ඔබේ ඇණවුම වෙනස් කර නැහැ.",
            "சிக்கலையும் தொடர்புடைய ஆர்டர் எண்ணையும் தெரிவிக்கவும். ரத்து அல்லது பணம் திரும்பப் பெறும் கோரிக்கையை ஆதரவு சரிபார்க்கலாம்; இந்த அரட்டை உங்கள் ஆர்டரை மாற்றவில்லை.",
        ),
    ],
}


def help_result(target: Optional[str], lang: str, *, navigate: bool = False) -> dict:
    target = target if target in DESTINATIONS and target != "login" else "support"
    steps = _GUIDES.get(
        target,
        [
            (
                "Open the page below and choose the option you need. Support can help with questions specific to your account.",
                "පහළ බොත්තමෙන් පිටුව විවෘත කර අවශ්‍ය විකල්පය තෝරන්න. ගිණුමට අදාළ විශේෂ ගැටලුවලට සහාය සේවාවෙන් උදව් ලබාගන්න පුළුවන්.",
                "கீழே உள்ள பக்கத்தைத் திறந்து தேவையான விருப்பத்தைத் தேர்ந்தெடுக்கவும். கணக்கு தொடர்பான கேள்விகளுக்கு உதவிச் சேவையை அணுகலாம்.",
            ),
        ],
    )
    return {
        "reply": tr(
            lang,
            "Use the button below to open it.",
            "පහළ බොත්තමෙන් ඒ පිටුවට යන්න පුළුවන්.",
            "கீழே உள்ள பொத்தானால் பக்கத்தைத் திறக்கலாம்.",
        )
        if navigate
        else tr(
            lang,
            "Let's do it step by step.",
            "අපි පියවරෙන් පියවර කරමු 😊",
            "படிப்படியாகச் செய்வோம் 😊",
        ),
        "actions": [action(target, lang)],
        "guide": None
        if navigate
        else {
            "title": tr(lang, *DESTINATIONS[target]),
            "steps": [tr(lang, *step) for step in steps],
        },
    }


_STATUSES = {
    "pending": ("Order received", "ඇණවුම ලැබුණා", "ஆர்டர் பெறப்பட்டது"),
    "confirmed": ("Confirmed", "තහවුරු කළා", "உறுதிசெய்யப்பட்டது"),
    "processing": ("Preparing", "සූදානම් කරනවා", "தயாராகிறது"),
    "packing": ("Packing", "ඇසුරුම් කරනවා", "பொதியாக்கப்படுகிறது"),
    "packed": ("Packed", "ඇසුරුම් කර අවසන්", "பொதியாக்கப்பட்டது"),
    "ready_for_pickup": (
        "Awaiting courier pickup",
        "කුරියර් භාරගන්නා තෙක් රැඳී සිටිනවා",
        "கூரியருக்காகக் காத்திருக்கிறது",
    ),
    "handed_to_courier": (
        "With the courier",
        "කුරියර් වෙත භාර දුන්නා",
        "கூரியரிடம் ஒப்படைக்கப்பட்டது",
    ),
    "shipped": ("In transit", "ප්‍රවාහනය වෙමින්", "பயணத்தில் உள்ளது"),
    "out_for_delivery": (
        "Out for delivery",
        "ඔබ වෙත බෙදාහැරීමට පිටත් වී ඇත",
        "விநியோகத்திற்குப் புறப்பட்டது",
    ),
    "delivered": ("Delivered", "බෙදාහැර අවසන්", "விநியோகிக்கப்பட்டது"),
    "delivery_failed": (
        "Delivery attempt failed",
        "බෙදාහැරීමේ උත්සාහය අසාර්ථකයි",
        "விநியோக முயற்சி தோல்வியடைந்தது",
    ),
    "rto_initiated": (
        "Returning to us",
        "අප වෙත ආපසු එවමින්",
        "எங்களிடம் திரும்புகிறது",
    ),
    "cancelled": ("Cancelled", "අවලංගු කළා", "ரத்து செய்யப்பட்டது"),
    "return_requested": ("Return requested", "ආපසු දීම ඉල්ලා ඇත", "திரும்பப் பெறக் கோரப்பட்டது"),
    "return_approved": (
        "Return approved",
        "ආපසු දීම අනුමතයි",
        "திரும்பப் பெற அனுமதிக்கப்பட்டது",
    ),
    "refunded": ("Refunded", "මුදල් ආපසු ලබා දුන්නා", "பணம் திரும்ப வழங்கப்பட்டது"),
    "unknown": ("Status unavailable", "තත්ත්වය ලබාගත නොහැක", "நிலை கிடைக்கவில்லை"),
}
_COURIER_STATUSES = {
    "booked": "ready_for_pickup",
    "picked_up": "handed_to_courier",
    "in_transit": "shipped",
    "failed": "delivery_failed",
    "returned": "rto_initiated",
}


def status_label(status: str, lang: str) -> str:
    return tr(lang, *_STATUSES.get(status, _STATUSES["unknown"]))


def iso(value) -> Optional[str]:
    if value is None:
        return None
    return (
        value.replace(tzinfo=timezone.utc).isoformat()
        if value.tzinfo is None
        else value.isoformat()
    )


def order_card(order: Order, events: list, lang: str, freshness: str) -> dict:
    """Only observed events are completed. Never infer delivery from a failed attempt."""
    milestones = [
        {
            "label": status_label("pending", lang),
            "at": iso(order.created_at),
            "state": "complete",
        }
    ]
    for event in events:
        if event.new_status != "pending":
            milestones.append(
                {
                    "label": status_label(event.new_status, lang),
                    "at": iso(event.created_at),
                    "state": "complete",
                }
            )
    if order.status == "pending" and not events:
        milestones[-1]["state"] = "current"
    elif not events or events[-1].new_status != order.status:
        milestones.append(
            {
                "label": status_label(order.status, lang),
                "at": iso(order.updated_at),
                "state": "current",
            }
        )
    else:
        milestones[-1]["state"] = "current"
    if order.status not in {
        "delivered",
        "cancelled",
        "refunded",
        "rto_initiated",
        "return_requested",
        "return_approved",
    }:
        milestones.append(
            {"label": status_label("delivered", lang), "at": None, "state": "upcoming"}
        )
    courier_status = order.courier_tracking_status
    return {
        "order_id": str(order.order_id),
        "order_number": order.order_number,
        "status": order.status,
        "status_label": status_label(order.status, lang),
        "total": float(order.total_amount),
        "currency": "LKR",
        "created_at": iso(order.created_at),
        "items": [
            {"name": item.product_name, "quantity": item.quantity}
            for item in order.items[:8]
        ],
        "item_count": sum(item.quantity for item in order.items),
        "timeline": milestones[-8:],
        "freshness": freshness,
        "courier_status_label": status_label(
            _COURIER_STATUSES.get(courier_status, courier_status), lang
        )
        if courier_status
        else None,
        "waybill": order.courier_waybill_number,
        "updated_at": iso(order.courier_tracking_updated_at or order.updated_at),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "will_retry": order.courier_will_retry if courier_status == "failed" else None,
        "action": action("orders", lang, str(order.order_id)),
    }


async def orders_result(
    db,
    user_id: Optional[UUID],
    reference: Optional[str],
    lang: str,
    *,
    recent: bool = False,
) -> dict:
    if user_id is None:
        return {
            "reply": tr(
                lang,
                "Sign in to see your own orders and delivery updates.",
                "ඔබේ ඇණවුම් සහ බෙදාහැරීමේ තොරතුරු බලන්න මුලින් ගිණුමට පිවිසෙන්න.",
                "உங்கள் ஆர்டர்களையும் விநியோகத் தகவல்களையும் பார்க்க உள்நுழையவும்.",
            ),
            "actions": [action("login", lang)],
        }
    # user_id comes only from authentication, never from the model/session text.
    query = (
        select(Order).where(Order.user_id == user_id).options(selectinload(Order.items))
    )
    if reference:
        try:
            query = query.where(Order.order_id == UUID(reference))
        except ValueError:
            query = query.where(Order.order_number == reference)
    query = query.order_by(Order.created_at.desc()).limit(3 if recent else 1)
    orders = list((await db.execute(query)).scalars().all())
    if not orders:
        return {
            "reply": tr(
                lang,
                "I couldn't find a matching order in your account. You can check My orders below.",
                "ඔබේ ගිණුමේ ගැළපෙන ඇණවුමක් හමු වුණේ නැහැ. පහළින් මගේ ඇණවුම් බලන්න පුළුවන්.",
                "உங்கள் கணக்கில் பொருந்தும் ஆர்டர் கிடைக்கவில்லை. கீழே எனது ஆர்டர்களைப் பார்க்கலாம்.",
            ),
            "actions": [action("orders", lang)],
        }
    cards = []
    for order in orders:
        freshness = "saved" if recent else "store"
        if not recent and order.courier_shipment_id:
            updated = order.courier_tracking_updated_at
            if (
                updated
                and (
                    datetime.now(timezone.utc)
                    - updated.replace(tzinfo=updated.tzinfo or timezone.utc)
                ).total_seconds()
                < 60
            ):
                freshness = "recent"
            else:
                try:
                    payload = await asyncio.wait_for(
                        courier_sync_service.refresh_order_tracking(db, order),
                        timeout=8,
                    )
                    freshness = "live" if payload else "unavailable"
                except asyncio.TimeoutError:
                    # A cancelled async DB operation must be rolled back before reuse.
                    await db.rollback()
                    return {
                        "reply": tr(
                            lang,
                            "Tracking is taking longer than expected. Please open your order and try again.",
                            "බෙදාහැරීමේ තොරතුරු ලබාගැනීමට වැඩි වේලාවක් යනවා. ඇණවුම විවෘත කර නැවත උත්සාහ කරන්න.",
                            "கண்காணிப்புத் தகவல் தாமதமாகிறது. ஆர்டரைத் திறந்து மீண்டும் முயற்சிக்கவும்.",
                        ),
                        "actions": [action("orders", lang)],
                    }
        events = list(
            (
                await db.execute(
                    select(OrderStatusHistory)
                    .where(OrderStatusHistory.order_id == order.order_id)
                    .order_by(OrderStatusHistory.created_at.desc())
                    .limit(8)
                )
            )
            .scalars()
            .all()
        )
        cards.append(order_card(order, list(reversed(events)), lang, freshness))
    reply = (
        tr(
            lang,
            "Here are your latest orders. Select one to see its details.",
            "මෙන්න ඔබේ මෑත ඇණවුම්. විස්තර බලන්න ඇණවුමක් තෝරන්න 😊",
            "உங்கள் சமீபத்திய ஆர்டர்கள் இதோ. விவரங்களைப் பார்க்க ஒன்றைத் தேர்ந்தெடுக்கவும்.",
        )
        if recent
        else tr(
            lang,
            "Here is the latest available update for your order.",
            "ඔබේ ඇණවුම ගැන දැනට ලැබී ඇති අලුත්ම තොරතුරු මෙන්න.",
            "உங்கள் ஆர்டருக்குக் கிடைக்கும் சமீபத்திய தகவல் இதோ.",
        )
    )
    return {"reply": reply, "orders": cards, "actions": [action("orders", lang)]}
