"""
Replies for fast-path plans, written from what the tools returned.

Every fact in these sentences -- a product's name, its price, that it exists
-- comes from this turn's tool results, never from a template's guess. Where
a server tool already wrote verified copy (orders, help, basket previews),
that copy is the reply.
"""

from typing import Any, Dict, List, Tuple

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


def _found(kind: str, products: List[dict], lang: str) -> str:
    first, count = products[0], len(products)
    name = first.get("name", "")
    if kind == "price":
        priced = " · ".join(f"{p['name']} {tr(lang, 'Rs', 'රු.', 'ரூ.')} {_money(p.get('price'))}" for p in products[:3])
        return tr(lang, f"Prices: {priced}.", f"මිල: {priced}.", f"விலை: {priced}.")
    if kind == "want":
        if count == 1:
            return tr(lang, f"Yes, we have {name}.", f"ඔව්, {name} තියෙනවා 😊", f"ஆம், {name} உள்ளது 😊")
        return tr(
            lang,
            f"Yes, {count} options including {name}.",
            f"ඔව්, {name} ඇතුළුව වර්ග {count}ක් තියෙනවා 😊",
            f"ஆம், {name} உட்பட {count} வகைகள் உள்ளன 😊",
        )
    if count == 1:
        return tr(lang, f"Here's {name}.", f"{name} මෙන්න 😊", f"{name} இதோ 😊")
    return tr(
        lang,
        f"{count} options, including {name}.",
        f"{name} ඇතුළුව වර්ග {count}ක් මෙන්න 😊",
        f"{name} உட்பட {count} வகைகள் இதோ 😊",
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
        "Check the cards for prices and quantities.",
        "මිල සහ ප්‍රමාණ පහළින් බලන්න.",
        "விலைகளையும் அளவுகளையும் கீழே பார்க்கவும்.",
    )
    if plan.reply_kind != "price" and any(out.get("products") for _, out in outputs):
        lines.append(tail)
    return " ".join(lines)
