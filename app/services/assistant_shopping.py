"""Grounded shopping comparisons and previewed, atomic basket edits."""

import copy
import json
import re
import time
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.config.redis import RedisKeys
from app.services.assistant_tools import action, tr
from app.services.cart_service import CART_TTL, CartService
from app.services.product_service import ProductService


def money(value):
    return float(
        Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def price(product):
    return money(
        product.get("discount_price")
        if product.get("is_on_sale") and product.get("discount_price") is not None
        else product.get("price")
    )


def pack_size(name):
    """Explicit single pack/multipack sizes only; shipping weight is NOT net content."""
    text = name.lower()
    matches = list(
        re.finditer(
            r"(?<![\d.])(?:(\d+)\s*[x×]\s*)?(\d+(?:\.\d+)?)\s*(kg|g|ml|litres?|liters?|l)\b",
            text,
        )
    )
    if len(matches) != 1:
        return None
    match = matches[0]
    # A trailing multipack indicator is ambiguous in this parser, so do not guess.
    if re.search(r"\s*[x×]\s*\d", text[match.end() :]):
        return None
    amount = Decimal(match[2]) * Decimal(match[1] or 1)
    unit = match[3]
    if unit in ("kg", "l", "litre", "litres", "liter", "liters"):
        amount *= 1000
    if amount <= 0:
        return None
    return amount, "g" if unit in ("g", "kg") else "ml"


def panel(title, sections=None, commands=None, note=None):
    return {
        "title": title,
        "sections": sections or [],
        "commands": commands or [],
        "note": note,
    }


def section(title, rows):
    return {
        "title": title,
        "rows": [{"label": label, "value": str(value)} for label, value in rows],
    }


def proposal_key(user_id, token):
    return f"assistant:proposal:{user_id}:{token}"


async def proposal(redis, user_id, kind, payload, label):
    token = uuid4().hex
    await redis.set(
        proposal_key(user_id, token),
        json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False),
        ex=600,
    )
    return {"token": token, "label": label}


async def catalog_product(db, branch_id, product_id, lang):
    from app.api.v1.products import format_product

    try:
        product_id = UUID(str(product_id))
    except (TypeError, ValueError):
        return None
    if not branch_id:
        return None
    found = await ProductService.get_by_id_for_branch(db, product_id, branch_id)
    if not found:
        return None
    result = format_product(
        found["product"], branch_overrides=found["effective"], lang=lang
    )
    result["pack_name"] = found["product"].name
    return result


async def search(db, branch_id, terms, lang, **kwargs):
    from app.services.chat_service import ChatService

    exact, broad = await ChatService._match_products(
        db, branch_id, terms[:4], lang=lang, **kwargs
    )
    return (exact or broad)[:6]


async def comparison(db, branch_id, plan, history, lang):
    if not branch_id:
        return {
            "reply": tr(
                lang,
                "Select a delivery location first.",
                "මුලින් බෙදාහැරීමේ ස්ථානය තෝරන්න.",
                "முதலில் விநியோக இடத்தைத் தேர்ந்தெடுக்கவும்.",
            ),
            "actions": [action("addresses", lang)],
        }
    ids = plan.get("product_ids") or []
    if not ids and plan.get("is_followup"):
        ids = next(
            (
                [p["product_id"] for p in t.get("products", [])[:4]]
                for t in reversed(history)
                if t.get("products")
            ),
            [],
        )
    products = [
        p for pid in ids[:4] if (p := await catalog_product(db, branch_id, pid, lang))
    ]
    if not products:
        products = await search(db, branch_id, plan.get("search_terms") or [], lang)
    if plan["intent"] == "alternatives" and ids:
        # The source may now be unavailable. Never substitute across categories.
        try:
            source = await ProductService.get_by_id(
                db, UUID(ids[0]), include_inactive=True
            )
        except ValueError:
            source = None
        if source and source.category_id:
            candidates, _ = await ProductService.get_all_for_branch(
                db,
                branch_id,
                category_id=source.category_id,
                subcategory_id=source.subcategory_id,
                limit=8,
            )
            products = [
                p
                for item in candidates
                if str(item["product"].product_id) not in ids
                and (
                    p := await catalog_product(
                        db, branch_id, item["product"].product_id, lang
                    )
                )
            ][:4]
        else:
            products = []
    products = list({p["product_id"]: p for p in products}.values())[:4]
    products = [
        fresh
        for p in products
        if (fresh := await catalog_product(db, branch_id, p["product_id"], lang))
    ]
    if not products:
        return {
            "reply": tr(
                lang,
                "Tell me the product name and pack size to find comparable options.",
                "ගැළපෙන විකල්ප සොයන්න භාණ්ඩයේ නම සහ පැකට් ප්‍රමාණය කියන්න.",
                "ஒப்பிடப் பொருளின் பெயரையும் பொதி அளவையும் சொல்லுங்கள்.",
            )
        }
    sections = []
    for p in products:
        net = pack_size(p.get("pack_name") or p["name"])
        unit_price = (
            f"LKR {money(Decimal(str(price(p))) * 100 / net[0]):.2f} / 100 {net[1]}"
            if net
            else tr(
                lang,
                "Pack size not verified",
                "පැකට් ප්‍රමාණය තහවුරු නැත",
                "பொதி அளவு உறுதியாகவில்லை",
            )
        )
        sections.append(
            section(
                p["name"],
                [
                    (tr(lang, "Price", "මිල", "விலை"), f"LKR {price(p):.2f}"),
                    (tr(lang, "Per unit", "ඒකක මිල", "அலகு விலை"), unit_price),
                    (
                        tr(
                            lang,
                            "Estimated cashback",
                            "අපේක්ෂිත ආපසු ලැබීම",
                            "மதிப்பிட்ட கேஷ்பேக்",
                        ),
                        f"LKR {money(price(p) * float(p.get('cashback_percentage') or 0) / 100):.2f}",
                    ),
                    (
                        tr(lang, "Available packs", "ඇති පැකට්", "கிடைக்கும் பொதிகள்"),
                        p.get("stock_quantity", 0),
                    ),
                ],
            )
        )
    return {
        "reply": tr(
            lang,
            "Here are real store prices and pack comparisons. Choose what suits you.",
            "වෙළඳසැලේ සැබෑ මිල සහ පැකට් ප්‍රමාණ මෙන්න. ඔබට ගැළපෙන එක තෝරන්න.",
            "கடையின் உண்மையான விலைகள் மற்றும் பொதி ஒப்பீடுகள் இதோ. பொருத்தமானதைத் தேர்ந்தெடுக்கவும்.",
        ),
        "products": products,
        "panels": [
            panel(
                tr(lang, "Compare your options", "විකල්ප සසඳමු", "விருப்பங்களை ஒப்பிடுவோம்"),
                sections,
                note=tr(
                    lang,
                    "Unit prices use the stated pack size. Compare similar products and matching units; cashback is separate from the payable price. No substitutions are automatic.",
                    "ඒකක මිල පැකට් නමේ ප්‍රමාණය මතයි. සමාන භාණ්ඩ සහ එකම ඒකක සසඳන්න. ආපසු ලැබීම් ගෙවිය යුතු මිලෙන් වෙනමයි. භාණ්ඩ ස්වයංක්‍රීයව මාරු කරන්නේ නැහැ.",
                    "அலகு விலை பொதிப் பெயரின் அளவை அடிப்படையாகக் கொண்டது. ஒத்த பொருட்களையும் ஒரே அலகுகளையும் ஒப்பிடுங்கள். கேஷ்பேக் செலுத்தும் விலையிலிருந்து தனியானது. தானியங்கி மாற்றங்கள் இல்லை.",
                ),
            )
        ],
    }


async def build_cart(db, branch_id, original, operations, lang):
    items = copy.deepcopy(original.get("items", []))
    changes = []
    for op in operations:
        pid = op.get("product_id")
        if not pid:
            query = op.get("query", "").strip().casefold()
            if not query:
                return None, []
            if op["operation"] in ("set", "remove"):
                candidates = [
                    item for item in items if query in item["name"].casefold()
                ]
            else:
                candidates = await search(db, branch_id, [query], lang)
            if len(candidates) != 1:
                return None, candidates
            pid = candidates[0]["product_id"]
            op["product_id"] = pid
            if candidates[0].get("variant_id"):
                op["variant_id"] = candidates[0]["variant_id"]
        variant = op.get("variant_id")
        matches = [
            p
            for p in items
            if p["product_id"] == pid and p.get("variant_id") == variant
        ]
        existing = matches[0] if matches else None
        if op["operation"] in ("set", "remove") and existing is None:
            raise HTTPException(409, "CART_ITEM_NOT_FOUND")
        if op["operation"] == "remove":
            items.remove(existing)
            changes.append((existing["name"], f"{existing['quantity']} → 0"))
            continue
        product = await catalog_product(db, branch_id, pid, lang)
        if not product:
            raise HTTPException(409, "PRODUCT_UNAVAILABLE")
        if product.get("has_variants"):
            # Variants need explicit selection; never silently add the base SKU.
            selected = next(
                (
                    v
                    for v in product["variants"]
                    if v["variant_id"] == variant and v["is_active"]
                ),
                None,
            )
            if selected is None:
                return None, [product]
            unit_price = money(selected["price"])
            stock = min(product["stock_quantity"], selected["stock_quantity"])
            name = product["name"] + " · " + selected["name"]
        else:
            if variant:
                raise HTTPException(409, "INVALID_VARIANT")
            unit_price, stock, name = (
                price(product),
                product["stock_quantity"],
                product["name"],
            )
        before = existing["quantity"] if existing else 0
        quantity = (
            before + op["quantity"] if op["operation"] == "add" else op["quantity"]
        )
        other_quantity = sum(
            p["quantity"]
            for p in items
            if p["product_id"] == pid and p.get("variant_id") != variant
        )
        if quantity > stock or quantity + other_quantity > product["stock_quantity"]:
            raise HTTPException(409, "STOCK_CHANGED")
        updated = {
            "product_id": pid,
            "variant_id": variant,
            "quantity": quantity,
            "price": unit_price,
            "name": name,
            "image": (product.get("images") or [{}])[0].get("image_url"),
            "sku": product.get("sku"),
            "category_id": product.get("category_id"),
            "original_price": product["price"],
            "cashback_percentage": product.get("cashback_percentage"),
        }
        if existing:
            items[items.index(existing)] = updated
        else:
            items.append(updated)
        changes.append((name, f"{before} → {quantity} · LKR {unit_price:.2f}"))
    return {"items": items, "changes": changes}, []


async def cart_preview(db, redis, user_id, branch_id, plan, lang):
    if not user_id:
        return {
            "reply": tr(
                lang,
                "Sign in to edit your basket with the assistant.",
                "සහායකයා හරහා බඩු මල්ල වෙනස් කරන්න ගිණුමට පිවිසෙන්න.",
                "உதவியாளர் மூலம் கூடையை மாற்ற உள்நுழையவும்.",
            ),
            "actions": [action("login", lang)],
        }
    if not branch_id:
        return {
            "reply": tr(
                lang,
                "Select your delivery location first.",
                "මුලින් බෙදාහැරීමේ ස්ථානය තෝරන්න.",
                "முதலில் விநியோக இடத்தைத் தேர்ந்தெடுக்கவும்.",
            ),
            "actions": [action("addresses", lang)],
        }
    operations = plan.get("operations") or []
    if not operations:
        return {
            "reply": tr(
                lang,
                "Tell me which item and how many packs to add, change or remove.",
                "එකතු කරන්න, ප්‍රමාණය වෙනස් කරන්න හෝ ඉවත් කරන්න ඕන භාණ්ඩය සහ පැකට් ගණන කියන්න.",
                "சேர்க்க, மாற்ற அல்லது நீக்க வேண்டிய பொருளையும் பொதி எண்ணிக்கையையும் சொல்லுங்கள்.",
            )
        }
    key = RedisKeys.cart(str(user_id))
    raw = await redis.get(key)
    raw = raw.decode() if isinstance(raw, bytes) else raw
    original = json.loads(raw) if raw else {"items": [], "coupon": None}
    try:
        built, choices = await build_cart(db, branch_id, original, operations, lang)
    except HTTPException:
        return {
            "reply": tr(
                lang,
                "I couldn't prepare that change. Check the item, variant and available quantity in your basket.",
                "ඒ වෙනස සූදානම් කරන්න බැරි වුණා. භාණ්ඩය, වර්ගය සහ ඇති ප්‍රමාණය බඩු මල්ලෙන් පරීක්ෂා කරන්න.",
                "அந்த மாற்றத்தைத் தயாரிக்க முடியவில்லை. பொருள், வகை மற்றும் கிடைக்கும் அளவைச் சரிபார்க்கவும்.",
            ),
            "actions": [action("cart", lang)],
        }
    if built is None:
        return {
            "reply": tr(
                lang,
                "Please specify the exact brand, pack size or variant. Your basket has not changed.",
                "නිශ්චිත වෙළඳ නාමය, පැකට් ප්‍රමාණය හෝ වර්ගය කියන්න. බඩු මල්ල තවම වෙනස් කර නැහැ.",
                "சரியான பிராண்ட், பொதி அளவு அல்லது வகையைக் குறிப்பிடவும். கூடை இன்னும் மாற்றப்படவில்லை.",
            ),
            "products": choices,
        }
    subtotal = money(
        sum(Decimal(str(p["price"])) * p["quantity"] for p in built["items"])
    )
    command = await proposal(
        redis,
        user_id,
        "cart",
        {
            "branch_id": str(branch_id),
            "base": raw,
            "operations": operations,
            "items": built["items"],
        },
        tr(
            lang,
            "Confirm basket changes",
            "බඩු මල්ලේ වෙනස්කම් තහවුරු කරන්න",
            "கூடை மாற்றங்களை உறுதிசெய்யவும்",
        ),
    )
    return {
        "reply": tr(
            lang,
            "Review these changes, then confirm to update your basket.",
            "මේ වෙනස්කම් පරීක්ෂා කර බඩු මල්ල යාවත්කාලීන කිරීමට තහවුරු කරන්න.",
            "இந்த மாற்றங்களைச் சரிபார்த்து கூடையைப் புதுப்பிக்க உறுதிசெய்யவும்.",
        ),
        "panels": [
            panel(
                tr(lang, "Basket preview", "බඩු මල්ලේ පෙරදසුන", "கூடை முன்னோட்டம்"),
                [
                    section(
                        "",
                        built["changes"]
                        + [
                            (
                                tr(
                                    lang,
                                    "Goods subtotal",
                                    "භාණ්ඩ එකතුව",
                                    "பொருட்களின் கூட்டுத்தொகை",
                                ),
                                f"LKR {subtotal:.2f}",
                            )
                        ],
                    )
                ],
                [command],
                tr(
                    lang,
                    "Delivery, coupons and final totals are calculated in your basket. This does not place an order. Preview expires in 10 minutes.",
                    "බෙදාහැරීම, කූපන් සහ අවසන් මුදල බඩු මල්ලේදී ගණනය කරනවා. මෙයින් ඇණවුමක් දාන්නේ නැහැ. පෙරදසුන විනාඩි 10කින් කල් ඉකුත් වෙනවා.",
                    "விநியோகம், கூப்பன்கள் மற்றும் இறுதித் தொகை கூடையில் கணக்கிடப்படும். இது ஆர்டர் செய்யாது. முன்னோட்டம் 10 நிமிடங்களில் காலாவதியாகும்.",
                ),
            )
        ],
    }


async def confirm_cart(db, redis, user_id, branch_id, key, raw_proposal, payload, lang):
    if str(branch_id) != payload["branch_id"]:
        raise HTTPException(409, "BRANCH_CHANGED")
    original = (
        json.loads(payload["base"])
        if payload["base"]
        else {"items": [], "coupon": None}
    )
    built, _ = await build_cart(db, branch_id, original, payload["operations"], lang)
    if not built or built["items"] != payload["items"]:
        raise HTTPException(409, "PRICE_OR_STOCK_CHANGED")
    original["items"] = built["items"]
    original["totals"] = await CartService._calculate_totals(
        original["items"], original.get("coupon")
    )
    original["updated_at"] = int(time.time())
    reply = {
        "reply": tr(
            lang,
            "Your basket is updated. Review delivery and the final total in your basket.",
            "බඩු මල්ල යාවත්කාලීන කළා. බෙදාහැරීම සහ අවසන් මුදල බඩු මල්ලෙන් බලන්න.",
            "கூடை புதுப்பிக்கப்பட்டது. விநியோகத்தையும் இறுதித் தொகையையும் கூடையில் பார்க்கவும்.",
        ),
        "products": [],
        "actions": [action("cart", lang)],
        "cart_changed": True,
    }
    applied = await redis.eval(
        """
      if redis.call('get', KEYS[2]) ~= ARGV[2] then return 0 end
      local current = redis.call('get', KEYS[1]) or ''
      if current ~= ARGV[1] then return 0 end
      redis.call('set', KEYS[1], ARGV[3], 'EX', ARGV[5])
      redis.call('set', KEYS[2], ARGV[4], 'EX', 86400)
      return 1
    """,
        2,
        RedisKeys.cart(str(user_id)),
        key,
        payload["base"] or "",
        raw_proposal,
        json.dumps(original),
        json.dumps({"done": reply}),
        CART_TTL,
    )
    if not applied:
        current = await redis.get(key)
        done = json.loads(current).get("done") if current else None
        if done:
            return done
        raise HTTPException(409, "CART_CHANGED_REVIEW_AGAIN")
    return reply
