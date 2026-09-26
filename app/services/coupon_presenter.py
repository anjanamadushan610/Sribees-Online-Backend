"""
What the customer app shows about a coupon -- decided here, in the customer's
language.

The app used to carry its own copy of the coupon rules: the cart re-computed
the discount, the offers screen decided which coupons were usable and wrote
the "add Rs X more" hint, and the model knew only two discount types. Every
change to a coupon rule was therefore also an app release, and the phone's
copy drifted from the server's (the cart ignored category restrictions, so it
promised a discount checkout then refused).

Now the app renders what this module returns and decides nothing:

- ``label`` / ``terms`` -- the words on the card ("20% OFF", "Min. order
  Rs. 2,000", ...), so a new kind of coupon needs no app change;
- ``section`` / ``action`` / ``status`` -- where the card goes and what its
  button does;
- ``reason`` -- why it cannot be used right now, from the same
  ``CouponService.validate`` checkout runs;
- ``estimated_discount`` -- from the same ``PricingService._discount``
  checkout charges.

Pure: no database, no request. The endpoint gathers the facts and passes
them in, which is what keeps this testable line by line.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from app.services.coupon_service import CouponService, CouponValidationError
from app.services.pricing_service import PricingService
from app.utils.i18n import DEFAULT_LANGUAGE, localized

COLOMBO = ZoneInfo("Asia/Colombo")

# One table per language. A key missing from si/ta falls back to English, so
# adding a rule can never render a blank.
_COPY: dict[str, dict[str, str]] = {
    "en": {
        "label.percentage": "{value}% OFF",
        "label.fixed": "Rs. {value} OFF",
        "term.min_order": "Min. order Rs. {amount}",
        "term.max_discount": "Max discount Rs. {amount}",
        "term.valid_until": "Valid till {date}",
        "term.first_order": "First order only",
        "term.categories": "Only on {names}",
        "term.products": "Selected products only",
        "term.per_user_once": "Once per customer",
        "term.per_user_many": "{count} times per customer",
        "term.tier": "{level} members and above",
        "reason.budget_ended": "This promotion has ended.",
        "reason.inactive": "This coupon is no longer active.",
        "reason.wrong_branch": "Not valid for your delivery area.",
        "reason.not_started": "This offer starts on {date}.",
        "reason.expired": "This offer has expired.",
        "reason.not_yours": "This coupon was issued to another customer.",
        "reason.usage_limit": "This offer has been fully claimed.",
        "reason.per_user_limit": "You have already used this coupon.",
        "reason.first_order": "Only on your first order.",
        "reason.not_whitelisted": "This offer is for selected customers.",
        "reason.tier": "For {level} members and above.",
        "reason.min_order": "Add Rs. {amount} more to unlock this offer.",
        "reason.min_order_restricted": "Add Rs. {amount} more of eligible items to unlock this offer.",
        "reason.no_eligible_items": "None of these items qualify for this offer.",
        "reason.not_found": "Invalid coupon code.",
        "reason.cart_empty": "Your cart is empty.",
        "status.ready": "READY",
        "status.applied": "APPLIED",
        "status.used": "USED",
        "status.ended": "EXPIRED",
        "saving": "You save Rs. {amount}",
        "section.eligible": "Eligible Offers",
        "section.collect": "Available Offers",
        "section.locked": "Other Coupons",
        "section.wallet": "My Coupons",
        "notice.select_items": "Select items in your cart to see which coupons apply.",
        "message.collected": "Offer added to My Coupons",
        "message.applied": "Coupon applied",
    },
    "si": {
        "label.percentage": "{value}% වට්ටම්",
        "label.fixed": "රු. {value} වට්ටම්",
        "term.min_order": "අවම ඇණවුම රු. {amount}",
        "term.max_discount": "උපරිම වට්ටම රු. {amount}",
        "term.valid_until": "{date} දක්වා වලංගුයි",
        "term.first_order": "පළමු ඇණවුමට පමණයි",
        "term.categories": "{names} සඳහා පමණයි",
        "term.products": "තෝරාගත් භාණ්ඩ සඳහා පමණයි",
        "term.per_user_once": "එක් පාරිභෝගිකයෙකුට එක් වරක්",
        "term.per_user_many": "එක් පාරිභෝගිකයෙකුට {count} වරක්",
        "term.tier": "{level} සහ ඉහළ සාමාජිකයින්ට",
        "reason.budget_ended": "මෙම දීමනාව අවසන් වී ඇත.",
        "reason.inactive": "මෙම කූපනය තවදුරටත් සක්‍රිය නැත.",
        "reason.wrong_branch": "ඔබේ බෙදාහැරීමේ ප්‍රදේශයට වලංගු නැත.",
        "reason.not_started": "මෙම දීමනාව {date} ආරම්භ වේ.",
        "reason.expired": "මෙම දීමනාව කල් ඉකුත් වී ඇත.",
        "reason.not_yours": "මෙම කූපනය වෙනත් පාරිභෝගිකයෙකුට නිකුත් කළ එකකි.",
        "reason.usage_limit": "මෙම දීමනාව සම්පූර්ණයෙන් ලබාගෙන අවසන්.",
        "reason.per_user_limit": "ඔබ මෙම කූපනය දැනටමත් භාවිත කර ඇත.",
        "reason.first_order": "ඔබේ පළමු ඇණවුමට පමණයි.",
        "reason.not_whitelisted": "මෙම දීමනාව තෝරාගත් පාරිභෝගිකයින් සඳහායි.",
        "reason.tier": "{level} සහ ඉහළ සාමාජිකයින් සඳහායි.",
        "reason.min_order": "මෙම දීමනාව ලබා ගැනීමට තව රු. {amount}ක් එකතු කරන්න.",
        "reason.min_order_restricted": "මෙම දීමනාව ලබා ගැනීමට සුදුසු භාණ්ඩ තව රු. {amount}ක් එකතු කරන්න.",
        "reason.no_eligible_items": "මෙම භාණ්ඩ කිසිවක් මෙම දීමනාවට සුදුසු නැත.",
        "reason.not_found": "වලංගු නොවන කූපන් කේතයකි.",
        "reason.cart_empty": "ඔබේ කරත්තය හිස්ය.",
        "status.ready": "සූදානම්",
        "status.applied": "යොදා ඇත",
        "status.used": "භාවිත කළා",
        "status.ended": "කල් ඉකුත්",
        "saving": "ඔබට රු. {amount}ක් ඉතිරියි",
        "section.eligible": "ලබා ගත හැකි දීමනා",
        "section.collect": "තවත් දීමනා",
        "section.locked": "වෙනත් කූපන්",
        "section.wallet": "මගේ කූපන්",
        "notice.select_items": "යෙදිය හැකි කූපන් බැලීමට ඔබේ කරත්තයේ භාණ්ඩ තෝරන්න.",
        "message.collected": "දීමනාව මගේ කූපන් වෙත එකතු කළා",
        "message.applied": "කූපනය යෙදුවා",
    },
    "ta": {
        "label.percentage": "{value}% தள்ளுபடி",
        "label.fixed": "ரூ. {value} தள்ளுபடி",
        "term.min_order": "குறைந்தபட்ச ஆர்டர் ரூ. {amount}",
        "term.max_discount": "அதிகபட்ச தள்ளுபடி ரூ. {amount}",
        "term.valid_until": "{date} வரை செல்லுபடியாகும்",
        "term.first_order": "முதல் ஆர்டருக்கு மட்டும்",
        "term.categories": "{names} க்கு மட்டும்",
        "term.products": "தேர்ந்தெடுக்கப்பட்ட பொருட்களுக்கு மட்டும்",
        "term.per_user_once": "ஒரு வாடிக்கையாளருக்கு ஒரு முறை",
        "term.per_user_many": "ஒரு வாடிக்கையாளருக்கு {count} முறை",
        "term.tier": "{level} மற்றும் அதற்கு மேற்பட்ட உறுப்பினர்களுக்கு",
        "reason.budget_ended": "இந்த சலுகை முடிவடைந்தது.",
        "reason.inactive": "இந்த கூப்பன் இப்போது செயலில் இல்லை.",
        "reason.wrong_branch": "உங்கள் விநியோகப் பகுதிக்கு செல்லுபடியாகாது.",
        "reason.not_started": "இந்த சலுகை {date} அன்று தொடங்கும்.",
        "reason.expired": "இந்த சலுகை காலாவதியானது.",
        "reason.not_yours": "இந்த கூப்பன் வேறு வாடிக்கையாளருக்கு வழங்கப்பட்டது.",
        "reason.usage_limit": "இந்த சலுகை முழுமையாகப் பெறப்பட்டுவிட்டது.",
        "reason.per_user_limit": "இந்த கூப்பனை நீங்கள் ஏற்கனவே பயன்படுத்திவிட்டீர்கள்.",
        "reason.first_order": "உங்கள் முதல் ஆர்டருக்கு மட்டும்.",
        "reason.not_whitelisted": "இந்த சலுகை தேர்ந்தெடுக்கப்பட்ட வாடிக்கையாளர்களுக்கு மட்டும்.",
        "reason.tier": "{level} மற்றும் அதற்கு மேற்பட்ட உறுப்பினர்களுக்கு மட்டும்.",
        "reason.min_order": "இந்த சலுகையைப் பெற மேலும் ரூ. {amount} சேர்க்கவும்.",
        "reason.min_order_restricted": "இந்த சலுகையைப் பெற தகுதியான பொருட்களை மேலும் ரூ. {amount} சேர்க்கவும்.",
        "reason.no_eligible_items": "இந்தப் பொருட்கள் எதுவும் இந்த சலுகைக்குத் தகுதியானவை அல்ல.",
        "reason.not_found": "தவறான கூப்பன் குறியீடு.",
        "reason.cart_empty": "உங்கள் கூடை காலியாக உள்ளது.",
        "status.ready": "தயார்",
        "status.applied": "சேர்க்கப்பட்டது",
        "status.used": "பயன்படுத்தப்பட்டது",
        "status.ended": "காலாவதி",
        "saving": "நீங்கள் ரூ. {amount} சேமிக்கிறீர்கள்",
        "section.eligible": "தகுதியான சலுகைகள்",
        "section.collect": "கிடைக்கும் சலுகைகள்",
        "section.locked": "மற்ற கூப்பன்கள்",
        "section.wallet": "எனது கூப்பன்கள்",
        "notice.select_items": "எந்த கூப்பன்கள் பொருந்தும் என்பதைப் பார்க்க உங்கள் கூடையில் பொருட்களைத் தேர்ந்தெடுக்கவும்.",
        "message.collected": "சலுகை எனது கூப்பன்களில் சேர்க்கப்பட்டது",
        "message.applied": "கூப்பன் சேர்க்கப்பட்டது",
    },
}

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Refusals that mean the coupon is finished for everyone, not "not yet".
_ENDED = frozenset({"budget_ended", "inactive", "expired", "usage_limit"})


def text(lang: str, key: str, **values: Any) -> str:
    """The copy for *key* in *lang*, falling back to English."""
    table = _COPY.get(lang) or _COPY[DEFAULT_LANGUAGE]
    template = table.get(key) or _COPY[DEFAULT_LANGUAGE][key]
    return template.format(**values)


def money(amount: Decimal | float | int) -> str:
    """``2,000`` or ``1,250.50`` -- the currency word comes from the template."""
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    if value == value.to_integral_value():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def day(dt: datetime, lang: str) -> str:
    """A calendar day in Sri Lanka time: ``5 Oct 2026``, or ``2026-10-05``
    where a month name would be English inside Sinhala or Tamil."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(COLOMBO)
    if lang == DEFAULT_LANGUAGE:
        return f"{local.day} {_MONTHS[local.month - 1]} {local.year}"
    return local.strftime("%Y-%m-%d")


def label(coupon, lang: str) -> str:
    value = Decimal(str(coupon.discount_value or 0))
    if coupon.discount_type == "percentage":
        shown = f"{value.normalize():f}" if value != value.to_integral_value() else str(int(value))
        return text(lang, "label.percentage", value=shown)
    return text(lang, "label.fixed", value=money(value))


def _tier_name(tier, lang: str) -> str:
    if tier is None:
        return ""
    return localized(tier, "name", lang) or ""


def terms(coupon, lang: str, *, category_names: Optional[dict[str, str]] = None) -> list[str]:
    """The fine print, one short line each, in the order a customer checks it."""
    lines: list[str] = []
    min_order = Decimal(str(coupon.min_order_value or 0))
    if min_order > 0:
        lines.append(text(lang, "term.min_order", amount=money(min_order)))
    if coupon.discount_type == "percentage" and coupon.max_discount_amount is not None:
        lines.append(text(lang, "term.max_discount", amount=money(coupon.max_discount_amount)))

    categories = [str(c.category_id) for c in (coupon.eligible_categories or [])]
    if categories:
        names = [n for n in ((category_names or {}).get(cid) for cid in categories) if n]
        lines.append(
            text(lang, "term.categories", names=", ".join(names))
            if names
            else text(lang, "term.products")
        )
    elif coupon.eligible_products:
        lines.append(text(lang, "term.products"))

    if coupon.first_order_only:
        lines.append(text(lang, "term.first_order"))
    if coupon.per_user_limit:
        lines.append(
            text(lang, "term.per_user_once")
            if coupon.per_user_limit == 1
            else text(lang, "term.per_user_many", count=coupon.per_user_limit)
        )
    if getattr(coupon, "min_tier", None) is not None:
        lines.append(text(lang, "term.tier", level=_tier_name(coupon.min_tier, lang)))
    if coupon.valid_until is not None:
        lines.append(text(lang, "term.valid_until", date=day(coupon.valid_until, lang)))
    return lines


def reason(error: CouponValidationError, lang: str, *, restricted: bool = False) -> str:
    """Why this coupon cannot be used now, in the customer's language."""
    code = getattr(error, "code", "invalid")
    params = getattr(error, "params", {}) or {}
    if code == "min_order":
        short_by = params.get("short_by") or params.get("min_order") or 0
        key = "reason.min_order_restricted" if restricted else "reason.min_order"
        return text(lang, key, amount=money(max(Decimal(str(short_by)), Decimal("0"))))
    if code == "not_started" and params.get("starts_at") is not None:
        return text(lang, "reason.not_started", date=day(params["starts_at"], lang))
    if code == "tier":
        return text(lang, "reason.tier", level=_tier_name(params.get("tier"), lang))
    key = f"reason.{code}"
    if key in _COPY[DEFAULT_LANGUAGE]:
        return text(lang, key)
    # A rule added to CouponService before its copy was written: the English
    # message it raised is still better than nothing.
    return str(error)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class Evaluation:
    """Whether a coupon can be used on these items, and for how much."""

    eligible: bool
    discount: Decimal
    error: Optional[CouponValidationError]
    restricted: bool


def is_restricted(coupon) -> bool:
    return bool(coupon.eligible_categories or coupon.eligible_products)


def evaluate(
    coupon,
    items: Optional[list[dict]],
    *,
    now: Optional[datetime] = None,
    branch_id: Optional[UUID] = None,
    user_id: Optional[UUID] = None,
    user_phone: Optional[str] = None,
    user_points: Optional[int] = None,
    user_redemptions: Optional[int] = None,
    user_has_completed_order: Optional[bool] = None,
) -> Evaluation:
    """
    Run checkout's own rules against *items*.

    ``items`` are server-priced, enriched lines (``price``, ``quantity``,
    ``product_id``, ``category_id``). ``None`` means "no basket yet": every
    rule that does not depend on the basket is still checked, and the minimum
    order is not -- there is nothing to measure it against.
    """
    restricted = is_restricted(coupon)
    if items is None:
        measured = Decimal(str(coupon.min_order_value or 0))
    else:
        measured = CouponService.eligible_subtotal(coupon, items)

    error: Optional[CouponValidationError] = None
    try:
        CouponService.validate(
            coupon,
            measured,
            now,
            branch_id=branch_id,
            user_id=user_id,
            user_redemptions=user_redemptions,
            user_has_completed_order=user_has_completed_order,
            user_points=user_points,
            user_phone=user_phone,
        )
    except CouponValidationError as exc:
        error = exc

    if items is None:
        return Evaluation(eligible=error is None, discount=Decimal("0"), error=error, restricted=restricted)

    # A restricted coupon with nothing it may discount says so, rather than
    # "add Rs X more" -- adding more of the wrong items would never unlock it.
    if restricted and measured <= 0 and (error is None or error.code == "min_order"):
        error = CouponValidationError(
            "None of these items qualify for this coupon.", "no_eligible_items"
        )

    discount = Decimal("0")
    if error is None:
        total = sum(
            (Decimal(str(i.get("price", 0))) * Decimal(str(i.get("quantity", 0))) for i in items),
            Decimal("0"),
        )
        discount = PricingService._discount(
            total,
            {
                "discount_type": coupon.discount_type,
                "discount_value": coupon.discount_value,
                "max_discount_amount": coupon.max_discount_amount,
                "eligible_subtotal": measured,
            },
        )
        if discount <= 0:
            error = CouponValidationError(
                "None of these items qualify for this coupon.", "no_eligible_items"
            )

    return Evaluation(eligible=error is None, discount=discount, error=error, restricted=restricted)


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

# The order sections are listed in, top to bottom. The endpoint sends only the
# ones that have cards.
SECTION_ORDER = ("eligible", "collect", "locked", "wallet")


def present(
    coupon,
    lang: str,
    *,
    evaluation: Evaluation,
    collected: bool,
    applied: bool = False,
    basket: bool = True,
    category_names: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """
    One card, ready to draw.

    ``basket`` is True when the customer is choosing a coupon for an order
    (cart, checkout, Buy Now) and False when they are browsing the Offers
    page with nothing selected.
    """
    error = evaluation.error
    code = getattr(error, "code", None) if error else None
    ended = code in _ENDED
    used = code == "per_user_limit"

    if applied:
        section = "eligible" if evaluation.eligible else "locked"
        action: Optional[str] = "remove"
        status = "applied"
    elif not basket:
        section = "wallet" if collected else "collect"
        action = None if collected or ended or used else "collect"
        status = "ended" if ended else "used" if used else "ready"
    elif evaluation.eligible:
        section = "eligible"
        action = "use"
        status = "ready"
    else:
        # Not usable on this basket. An offer not yet in My Coupons can still
        # be saved for later; one already there has nothing to do but wait.
        section = "locked"
        action = "collect" if not collected and not ended and not used else None
        status = "ended" if ended else "used" if used else "locked"

    tier = getattr(coupon, "min_tier", None)
    # Browsing never measures a minimum order (there is no basket), so every
    # refusal that can reach here is one worth saying.
    shown_reason = (
        reason(error, lang, restricted=evaluation.restricted) if error is not None else None
    )

    return {
        "coupon_id": str(coupon.coupon_id),
        "code": coupon.code,
        "label": label(coupon, lang),
        "description": coupon.description,
        "terms": terms(coupon, lang, category_names=category_names),
        "valid_until": coupon.valid_until.isoformat() if coupon.valid_until else None,
        "section": section,
        "action": action,
        "applied": applied,
        "collected": collected,
        "eligible": evaluation.eligible,
        "status": {
            "key": status,
            "text": text(lang, f"status.{status}") if status != "locked" else None,
        },
        # Faded on screen: it cannot be used on this order, or ever again.
        "dimmed": status in ("used", "ended", "locked") or (applied and not evaluation.eligible),
        "reason": shown_reason,
        "estimated_discount": float(evaluation.discount),
        "saving": (
            text(lang, "saving", amount=money(evaluation.discount))
            if evaluation.eligible and evaluation.discount > 0
            else None
        ),
        "tier": (
            {"name": _tier_name(tier, lang), "color": tier.color}
            if tier is not None
            else None
        ),
    }


def sections(cards: list[dict], lang: str) -> list[dict[str, str]]:
    """The section headings the cards fall under, in display order."""
    present_keys = {c["section"] for c in cards}
    return [
        {"key": key, "title": text(lang, f"section.{key}")}
        for key in SECTION_ORDER
        if key in present_keys
    ]
