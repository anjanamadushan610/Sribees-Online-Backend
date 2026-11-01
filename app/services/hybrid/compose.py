"""
Replies for fast-path plans, written from what the tools returned.

Every fact in these sentences -- a product's name, its price, that it exists
-- comes from this turn's tool results, never from a template's guess. Where
a server tool already wrote verified copy (orders, help, basket previews),
that copy is the reply.
"""

import zlib
from typing import Any, Dict, List, Tuple

from app.services.assistant_agent import MAX_TOOL_PRODUCTS
from app.services.assistant_tools import tr
from app.services.hybrid.plan import Plan, ToolCall


def _money(value: Any) -> str:
    amount = float(value or 0)
    return f"{amount:,.0f}" if amount.is_integer() else f"{amount:,.2f}"


def greeting(lang: str) -> str:
    return tr(
        lang,
        "Hello! I can find products, check your orders or help with the app. What do you need?",
        "ආයුබෝවන් 😊 භාණ්ඩ සොයන්න, ඇණවුම් බලන්න, නැත්නම් ඇප් එක ගැන උදව් කරන්න මම ලෑස්තියි. මොකක්ද ඕන?",
        "வணக்கம் 😊 பொருட்களைத் தேட, ஆர்டர்களைப் பார்க்க அல்லது செயலி பற்றி உதவ நான் தயார். என்ன வேண்டும்?",
    )


def thanks(lang: str) -> str:
    return tr(
        lang,
        "You're welcome! Tell me if you need anything else.",
        "සතුටුයි 😊 තව මොනවා හරි ඕන නම් කියන්න.",
        "மகிழ்ச்சி 😊 வேறு ஏதாவது வேண்டுமென்றால் சொல்லுங்கள்.",
    )


def choose_location(lang: str) -> str:
    return tr(
        lang,
        "Choose your delivery location first, so I can show you what we deliver there.",
        "ඔබට බෙදාහැරිය හැකි භාණ්ඩ පෙන්වන්න මුලින් බෙදාහැරීමේ ස්ථානය තෝරන්න.",
        "உங்களுக்கு விநியோகிக்கக்கூடிய பொருட்களைக் காட்ட முதலில் விநியோக இடத்தைத் தேர்ந்தெடுக்கவும்.",
    )


def _not_found(query: str, lang: str) -> str:
    return tr(
        lang,
        f"I couldn't find {query}. Try another name or size.",
        f"{query} හමු වුණේ නැහැ. වෙනත් නමක් හෝ ප්‍රමාණයක් කියන්න පුළුවන්ද?",
        f"{query} கிடைக்கவில்லை. வேறு பெயர் அல்லது அளவை முயற்சிக்கவும்.",
    )


def rescued(lang: str) -> str:
    return tr(
        lang,
        "I can't answer in full right now, but these match what you wrote.",
        "දැනට සම්පූර්ණ පිළිතුරක් දෙන්න බැහැ, නමුත් ඔබ ලියපු දේට ගැළපෙන භාණ්ඩ මේවා.",
        "இப்போது முழு பதில் தர முடியவில்லை, ஆனால் நீங்கள் எழுதியதற்குப் பொருந்தும் பொருட்கள் இவை.",
    )


def _price_of(product: dict, lang: str) -> str:
    return f"{tr(lang, 'Rs', 'රු.', 'ரூ.')} {_money(product.get('price'))}"


def _pick(seed: str, *lines: str) -> str:
    """The same answer every time for the same products, but not one fixed sentence for all."""
    return lines[zlib.crc32(seed.encode("utf-8")) % len(lines)]


def _found(kind: str, products: List[dict], lang: str) -> str:
    first, count = products[0], len(products)
    name = first.get("name", "")
    if kind == "price":
        priced = " · ".join(f"{p['name']} {_price_of(p, lang)}" for p in products[:3])
        return tr(lang, f"Prices: {priced}.", f"මිල: {priced}.", f"விலை: {priced}.")
    seed = name + kind
    if count == 1:
        price = _price_of(first, lang)
        if kind == "want":
            return tr(
                lang,
                f"Yes, we have {name} at {price}.",
                f"ඔව්, {name} තියෙනවා, {price} 😊",
                f"ஆம், {name} உள்ளது, {price} 😊",
            )
        return tr(lang, f"Here's {name}, {price}.", f"මෙන්න {name}, {price} 😊", f"இதோ {name}, {price} 😊")
    # A full list was cut off at the cap, so its length is not how many we stock.
    capped = count >= MAX_TOOL_PRODUCTS
    low = _price_of(min(products, key=lambda p: float(p.get("price") or 0)), lang)
    options = tr(
        lang,
        "several options" if capped else f"{count} options",
        "වර්ග කිහිපයක්" if capped else f"වර්ග {count}ක්",
        "பல வகைகள்" if capped else f"{count} வகைகள்",
    )
    if kind == "want":
        return tr(
            lang,
            _pick(seed, f"Yes, {options} including {name}, from {low}.", f"We have {options}, from {low}, including {name}."),
            _pick(seed, f"ඔව්, {name} ඇතුළුව {options} තියෙනවා, {low} ඉඳන් 😊", f"තියෙනවා 😊 {name} ඇතුළුව {options}, {low} ඉඳන්."),
            _pick(seed, f"ஆம், {name} உட்பட {options} உள்ளன, {low} முதல் 😊", f"உள்ளது 😊 {name} உட்பட {options}, {low} முதல்."),
        )
    return tr(
        lang,
        _pick(seed, f"{options}, from {low}, including {name}.", f"Here you go: {options} including {name}, from {low}."),
        _pick(seed, f"{name} ඇතුළුව {options} මෙන්න, {low} ඉඳන් 😊", f"මේ බලන්න 😊 {name} ඇතුළුව {options}, {low} ඉඳන්."),
        _pick(seed, f"{name} உட்பட {options} இதோ, {low} முதல் 😊", f"இவற்றைப் பாருங்கள் 😊 {name} உட்பட {options}, {low} முதல்."),
    )


def products_reply(plan: Plan, outputs: List[Tuple[ToolCall, Dict[str, Any]]], lang: str) -> str:
    lines = []
    for step, out in outputs:
        found = out.get("products") or []
        if found:
            lines.append(_found(plan.reply_kind, found, lang))
        else:
            lines.append(_not_found(step.args.get("query", ""), lang))
    tail = tr(
        lang,
        "Pick the one you want below.",
        "ඕන එක පහළින් තෝරන්න.",
        "வேண்டியதைக் கீழே தேர்ந்தெடுக்கவும்.",
    )
    if plan.reply_kind != "price" and any(out.get("products") for _, out in outputs):
        lines.append(tail)
    return " ".join(lines)
