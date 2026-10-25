"""
L2: hand-written rules for the frequent, safe message shapes.

Every word is given the kinds it can have (a product word, "want", "order",
filler...). A rule fires only when *every* word is accounted for and the
kinds point one way; a single word the rules do not understand, or one that
needs judgement (comparisons, negations, "best", recipes), sends the whole
message to the model. Precision first: a missed easy message costs a model
call, a wrong instant answer costs trust.
"""

import re
from typing import Dict, List, Optional, Set

from app.services import chat_language
from app.services.hybrid import lexicon as lx
from app.services.hybrid.normalize import Message, Word
from app.services.hybrid.plan import Plan, ToolCall
from app.services.search_text import SOFT_TOKENS

MAX_PRODUCT_GROUPS = 3
MIN_STEM = 4  # "orderekata", Tamil "முகவரியை": a known word plus a case ending


def _kinds(word: Word, vocabulary) -> Set[str]:
    toks = set(word.tokens)
    kinds: Set[str] = set()

    def all_in(group) -> bool:
        return toks <= group

    def stem_in(group) -> bool:
        """Every token is a word of ``group`` or one of them plus an ending."""
        return all(
            t in group or any(len(g) >= MIN_STEM and t.startswith(g) for g in group) for t in toks
        )

    if toks & lx.NEEDS_MODEL:
        return {"needs_model"}
    if all_in(lx.CONJUNCTION):
        kinds.add("and")
    if all_in(lx.FILLER):
        kinds.add("filler")
    if stem_in(lx.ORDER):
        kinds.add("order")
    for name, group in (
        ("want", lx.WANT),
        ("price", lx.PRICE),
        ("track", lx.TRACK),
        ("history", lx.HISTORY),
        ("not_arrived", lx.NOT_ARRIVED),
        ("how", lx.HOW),
        ("open", lx.OPEN),
        ("greeting", lx.GREETING),
        ("thanks", lx.THANKS),
        ("cart_add", lx.CART_ADD),
        ("verb", lx.HELP_VERBS),
    ):
        if all_in(group):
            kinds.add(name)
    for topic, group in lx.TOPICS.items():
        if stem_in(group):
            kinds.add(f"topic:{topic}")
    if not kinds and _is_product_word(word, vocabulary):
        kinds.add("product")
    return kinds or {"unknown"}


def _is_product_word(word: Word, vocabulary) -> bool:
    """Every token is a catalogue word, a pack size, or completes/corrects to one."""
    for tok in word.tokens:
        if tok.isdigit() or tok in SOFT_TOKENS or tok in vocabulary.terms:
            continue
        if len(tok) < 3:
            return False
        if not any(term in vocabulary.terms for term in vocabulary.expand(tok) if term != tok):
            return False
    return True


def _language(msg: Message, fallback: str) -> str:
    fixed = chat_language.fixed_language(msg.raw, fallback)
    if fixed:
        return fixed
    toks = set(msg.tokens)
    singlish, english = len(toks & lx.SINGLISH), len(toks & lx.ENGLISH)
    if singlish > english:
        return "si"
    if english > singlish:
        return "en"
    return fallback


_SIZE = re.compile(r"^\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?\s?(?:g|kg|ml|l|ltr|mg)$", re.I)


def _product_groups(words: List[Word], kinds: List[Set[str]]) -> List[Dict[str, str]]:
    """
    Product words split into separate lookups at "and" / commas. A pack size
    ("400g", "9-14kg") becomes the lookup's size filter, so only that size
    counts as a match -- the same way the agent passes it.
    """
    groups: List[Dict[str, list]] = [{"words": [], "size": []}]
    for word, k in zip(words, kinds, strict=True):
        if "product" in k:
            groups[-1]["size" if _SIZE.match(word.raw) else "words"].append(word.raw)
        elif "and" in k and (groups[-1]["words"] or groups[-1]["size"]):
            groups.append({"words": [], "size": []})
    out = []
    for g in groups:
        if not g["words"]:
            continue  # a size on its own is not a product
        args = {"query": " ".join(g["words"])}
        if g["size"]:
            args["size"] = g["size"][0]
        out.append(args)
    return out


def route(msg: Optional[Message], vocabulary, fallback_lang: str) -> Optional[Plan]:
    """A plan for ``msg`` if a rule is sure, else None (the model answers)."""
    if msg is None:
        return None
    kinds = [_kinds(w, vocabulary) for w in msg.words]
    present: Dict[str, int] = {}
    for k in kinds:
        for name in k:
            present[name] = present.get(name, 0) + 1
    if "needs_model" in present or "unknown" in present:
        return None
    lang = _language(msg, fallback_lang)
    topics = sorted(name[6:] for name in present if name.startswith("topic:"))
    has = present.__contains__
    products = _product_groups(msg.words, kinds)

    def plan(steps, reply_kind, rule, signature) -> Plan:
        return Plan(steps=steps, reply_kind=reply_kind, layer="L2", rule=rule, signature=signature, language=lang)

    # Greetings and thanks: nothing but social words and filler.
    social = {"greeting", "thanks", "filler", "and", "how"}
    if all(k & social for k in kinds) and (has("greeting") or has("thanks")):
        kind = "thanks" if has("thanks") and not has("greeting") else "greeting"
        return plan([], kind, f"social.{kind}", kind)

    # App help and pages: a topic plus "how" or "open", no products.
    if topics and (has("how") or has("open")) and not products and len(topics) == 1:
        opening = has("open") and not has("how")
        topic = topics[0]
        return plan(
            [ToolCall("app_help", {"topic": topic, "open_page": opening})],
            "open" if opening else "help",
            "help.open" if opening else "help.how",
            f"{'open' if opening else 'how'}|{topic}",
        )

    # The customer's own wallet / points or offers: "mage wallet eke salli
    # kiyada", "points kiyada", "offers monada thiyenne".
    account_words = {"filler", "want", "price", "topic:wallet", "topic:offers"}
    if len(topics) == 1 and topics[0] in ("wallet", "offers") and not products and all(k & account_words for k in kinds):
        tool = "wallet_and_points" if topics[0] == "wallet" else "my_offers"
        return plan([ToolCall(tool, {})], "account", f"account.{topics[0]}", f"account|{topics[0]}")

    # "How do I place an order": order + how + a placing verb ("danne").
    # Without the verb, "mage order eka kohomada" asks how the order is doing.
    if has("order") and has("how") and has("verb") and not topics and not products:
        return plan(
            [ToolCall("app_help", {"topic": "checkout", "open_page": False})], "help", "help.order", "how|order"
        )

    if has("verb"):
        return None  # action words outside a how-to question need judgement

    # Orders: an order word (or reference), nothing about products or pages.
    if (has("order") or msg.order_reference) and not products and not topics and not has("how"):
        if has("history"):
            return plan([ToolCall("my_orders", {"recent": True})], "order_history", "orders.history", "orders|history")
        if has("track") or has("not_arrived") or msg.order_reference or msg.has_question_mark:
            args = {"order_reference": msg.order_reference} if msg.order_reference else {}
            return plan([ToolCall("my_orders", args)], "order_track", "orders.track", "orders|track")
        return None

    if not products or len(products) > MAX_PRODUCT_GROUPS:
        return None
    if has("order") or has("history") or has("track") or has("greeting") or has("thanks"):
        return None  # products mixed with another request: the model handles both
    if has("how") and not has("price"):
        return None  # "how ... <product>" is a how-to, not a lookup; "how much" is a price

    # Into the basket: products plus an "add" word ("kiri 2k cart ekata danna").
    if has("cart_add"):
        if len(products) != 1:
            return None
        query = " ".join(x for x in (products[0]["query"], products[0].get("size")) if x)
        op = {"operation": "add", "query": query, "quantity": msg.quantity or 1}
        return plan([ToolCall("stage_basket_change", {"operations": [op]})], "cart_preview", "cart.add", "cart_add|<P>")
    if topics or msg.quantity is not None:
        # A count without "add" ("kiri gana 3 karanna") changes something
        # already in the basket -- the model works out what.
        return None

    steps = [ToolCall("search_products", dict(args)) for args in products]
    kind = "price" if has("price") else "want" if has("want") else "list"
    shape = "|".join(["<P>"] * len(products))
    return plan(steps, kind, f"products.{kind}", f"{kind}|{shape}")
