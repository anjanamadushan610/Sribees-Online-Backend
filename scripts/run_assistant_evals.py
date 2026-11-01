"""Snapshot evals for the shopping assistant's planning call.

Each case in ``evals/assistant/cases.jsonl`` is a fixed starting state (prior
conversation + new message) and a graded outcome: the intent chosen, the
reply language, the search terms/brand/size/price extracted, and sentences the
reply must never contain. Graders are plain code -- no LLM judge yet.

Every case runs ``--k`` times. ``pass^k`` (all k trials pass) is the headline
number because a customer-facing bot has to be right every time, not once in
three. Token use is read from Gemini's own usageMetadata on every call, so the
report also says what a message really costs.

Run from the backend root (consumes provider quota -- mind --max-calls):

    python -m scripts.run_assistant_evals --live --k 3 --max-calls 60
    python -m scripts.run_assistant_evals --live --flow orders --k 1
    python -m scripts.run_assistant_evals --live --model gemini-3.5-flash-lite
"""

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

CASES = Path(__file__).resolve().parent.parent / "evals" / "assistant" / "cases.jsonl"

# USD per 1M tokens, from https://ai.google.dev/gemini-api/docs/pricing
# (read 2026-10-25, Flash list price through 2026-12-31). Override per run.
DEFAULT_PRICE = {"input": 0.75, "cached": 0.075, "output": 3.75}


def load_cases(path: Path = CASES) -> List[Dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _hit(values: List[str], needles: List[str]) -> bool:
    values = [v.casefold() for v in values if v]
    return any(n.casefold() in v for v in values for n in needles)


def grade(case: Dict[str, Any], plan: Dict[str, Any], language: str) -> List[str]:
    """Failed checks for one trial; an empty list is a pass."""
    expect = case["expect"]
    failures: List[str] = []
    terms = plan.get("search_terms") or []
    reply = (plan.get("reply") or "") + "\n" + (plan.get("briefing_note") or "")

    if plan.get("intent") not in expect["intent"]:
        failures.append(f"intent={plan.get('intent')} want {expect['intent']}")
    if "lang" in expect and language != expect["lang"]:
        failures.append(f"lang={language} want {expect['lang']}")
    if expect.get("no_terms") and terms:
        failures.append(f"searched {terms} but should not")
    if "terms_any" in expect and not _hit(terms, expect["terms_any"]):
        failures.append(f"terms {terms} miss {expect['terms_any']}")
    if "terms_none" in expect and _hit(terms, expect["terms_none"]):
        failures.append(f"terms {terms} contain forbidden {expect['terms_none']}")
    for group in expect.get("terms_all", []):
        if not _hit(terms, group):
            failures.append(f"terms {terms} miss one of {group}")
    if "brand_any" in expect and not _hit([plan.get("brand") or ""], expect["brand_any"]):
        failures.append(f"brand={plan.get('brand')} want {expect['brand_any']}")
    if "variant_any" in expect and not _hit(
        [plan.get("variant") or ""] + terms, expect["variant_any"]
    ):
        failures.append(f"variant={plan.get('variant')} want {expect['variant_any']}")
    if "max_price" in expect and plan.get("max_price") != expect["max_price"]:
        failures.append(f"max_price={plan.get('max_price')} want {expect['max_price']}")
    if "order_reference" in expect and plan.get("order_reference") != expect["order_reference"]:
        failures.append(
            f"order_reference={plan.get('order_reference')} want {expect['order_reference']}"
        )
    if "target_any" in expect and plan.get("target") not in expect["target_any"]:
        failures.append(f"target={plan.get('target')} want {expect['target_any']}")
    for phrase in expect.get("forbid_reply", []):
        if phrase.casefold() in reply.casefold():
            failures.append(f"reply contains forbidden {phrase!r}")
    return failures


class UsageMeter:
    """Collects Gemini usageMetadata per call by wrapping ChatService._log_usage."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, int]] = []

    def record(self, usage: Optional[Dict[str, Any]]) -> None:
        usage = usage or {}
        self.calls.append(
            {
                "prompt": usage.get("promptTokenCount", 0),
                "cached": usage.get("cachedContentTokenCount", 0),
                "output": usage.get("candidatesTokenCount", 0)
                + usage.get("thoughtsTokenCount", 0),
            }
        )

    def usd(self, price: Dict[str, float]) -> float:
        total = 0.0
        for c in self.calls:
            fresh = max(c["prompt"] - c["cached"], 0)
            total += (
                fresh * price["input"] + c["cached"] * price["cached"] + c["output"] * price["output"]
            ) / 1_000_000
        return total


# ---------------------------------------------------------------------------
# Agent mode: the whole tool loop against a fixed catalogue snapshot
# ---------------------------------------------------------------------------

SNAPSHOT = CASES.parent / "catalog_snapshot.json"

# Which tool shows that the agent understood a planner intent.
INTENT_TOOLS = {
    "buy_product": {"search_products"},
    "recipe_ingredients": {"search_products", "pantry_recipes"},
    "pantry_recipe": {"pantry_recipes", "search_products"},
    "track_order": {"my_orders"},
    "recent_orders": {"my_orders"},
    "app_help": {"app_help"},
    "navigate": {"app_help"},
    "cart_edit": {"stage_basket_change"},
    "compare_products": {"compare_products"},
    "alternatives": {"compare_products", "search_products"},
    "memory": {"saved_preferences"},
    "reminders": {"reminders"},
}


def harness_targets():
    """Every (object, attribute) AgentHarness replaces -- for tests to restore."""
    from app.services import (
        assistant_account,
        assistant_agent,
        assistant_shopping,
        catalog_search_service,
        semantic_search,
    )
    from app.services.chat_service import ChatService

    targets = [(catalog_search_service, "any_covers_all"), (ChatService, "_match_products"), (assistant_shopping, "catalog_product"),
               (semantic_search, "related_product_ids")]
    targets += [(assistant_agent, n) for n in ("orders_result", "_feature", "_run_tool")]
    targets += [(assistant_account, n) for n in ("wallet_summary", "my_offers", "my_wishlist", "my_notifications",
                                                  "my_referrals", "reorder", "stage_wishlist_add")]
    return targets


class AgentHarness:
    """
    Stands in for the database: catalogue search runs over the snapshot with
    the real tokenizer (exact and prefix matches, no typo expansion), orders
    and basket/preference/reminder previews return fixed server copy. Every
    tool call the model makes is recorded for grading.
    """

    def __init__(self) -> None:
        from app.services import assistant_agent, catalog_search_service
        from app.services.catalog_search_service import Vocabulary
        from app.services.chat_service import ChatService
        from app.services.search_text import index_tokens

        data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["products"]
        for p in data:
            p["_tokens"] = index_tokens(p["name"], p.get("name_si"), p.get("name_ta"), p.get("search_keywords"))[0]
        self.products = data
        self.calls: List[Dict[str, Any]] = []
        self.agent = assistant_agent

        ChatService._match_products = staticmethod(self.match)
        assistant_agent.orders_result = self.orders
        assistant_agent._feature = self.feature
        original = assistant_agent._run_tool

        async def recorded(name, raw_args, ctx, turn):
            self.calls.append({"name": name, "args": raw_args or {}})
            return await original(name, raw_args, ctx, turn)

        assistant_agent._run_tool = recorded
        self.vectors: Dict[str, List[float]] = {}
        self._fake_account_tools()
        self.by_id = {p["product_id"]: p for p in data}
        self.rows = [
            (p["product_id"], *index_tokens(p["name"], p.get("name_si"), p.get("name_ta"), p.get("search_keywords")),
             False, 0, p["name"])
            for p in data
        ]
        self.vocabulary = Vocabulary(Counter(t for p in data for t in set(p["_tokens"])))
        catalog_search_service.any_covers_all = self.covers
        from app.services import assistant_shopping

        assistant_shopping.catalog_product = self.catalog_product

    @staticmethod
    def _fake_account_tools() -> None:
        """Fixed account data, so the account tools run without a database."""
        from app.services import assistant_account as acc

        def fixed(reply, data):
            async def tool(*args, **kwargs):
                return {"ui": {"reply": reply}, "data": data}

            return tool

        acc.wallet_summary = fixed("Your wallet has Rs 450. Points: 120.", {"wallet_balance": 450, "points": 120})
        acc.my_offers = fixed("2 offers are available for you now.", {"count": 2, "offers": [{"code": "SAVE10"}, {"code": "NEW50"}]})
        acc.my_wishlist = fixed("You have 1 item in your wishlist.", {"count": 1})
        acc.my_notifications = fixed("Your latest notifications are below.", {"total": 1})
        acc.my_referrals = fixed("Your invite code is SB1234.", {"code": "SB1234"})
        acc.reorder = fixed("Check the preview below and confirm it to apply.", {"needs_confirmation": True})
        acc.stage_wishlist_add = fixed("Add it to your wishlist? Tap confirm below.", {"needs_confirmation": True})

    async def catalog_product(self, db, branch_id, product_id, lang):
        p = self.by_id.get(str(product_id))
        return self.card(p, lang) if p else None

    @staticmethod
    def card(p, lang):
        name = {"si": p.get("name_si"), "ta": p.get("name_ta")}.get(lang) or p["name"]
        return {
            "product_id": p["product_id"], "name": name, "price": p["price"],
            "discount_price": p["discount_price"], "is_on_sale": p["is_on_sale"], "variants": [],
        }

    async def enable_semantic(self) -> None:
        """Embed the snapshot with the real model and serve meaning lookups from it."""
        from types import SimpleNamespace

        from app.services import semantic_search

        docs = [
            semantic_search.document_text(
                SimpleNamespace(
                    name=p["name"], name_si=p.get("name_si"), name_ta=p.get("name_ta"),
                    category=p.get("category"), subcategory=p.get("subcategory"),
                    search_keywords=p.get("search_keywords"),
                )
            )
            for p in self.products
        ]
        vectors = await semantic_search.embed(docs, "RETRIEVAL_DOCUMENT")
        self.vectors = {p["product_id"]: v for p, v in zip(self.products, vectors, strict=True)}

        async def related(db, query, limit=6):
            (qv,) = await semantic_search.embed([query], "RETRIEVAL_QUERY")
            return semantic_search.rank(qv, self.vectors, limit)

        semantic_search.related_product_ids = related
        self.agent.settings.semantic_search_enabled = True

    async def match(self, db, branch_id, terms, brand=None, variant=None, max_price=None, min_price=None, lang="si"):
        from app.services.catalog_search_service import make_plan, rank

        def price_of(p):
            return p["discount_price"] if p["is_on_sale"] and p["discount_price"] else p["price"]

        query = f"{brand} {terms[0]}".strip() if brand else terms[0]
        plan = make_plan(self.vocabulary, query)
        if plan is None:
            return [], []
        wanted = set(plan.terms)
        rows = [r for r in self.rows if wanted & set(r[1])]
        ranked = [self.by_id[pid] for pid in rank(rows, plan)]
        broad = [
            p for p in ranked
            if (not brand or brand.casefold() in p["name"].casefold())
            and (max_price is None or price_of(p) <= max_price)
            and (min_price is None or price_of(p) >= min_price)
        ]
        broad = broad[:9]
        size = (variant or "").casefold().replace(" ", "")
        strict = [p for p in broad if not size or size in p["name"].casefold().replace(" ", "")]

        return [self.card(p, lang) for p in strict], [self.card(p, lang) for p in broad]

    async def covers(self, db, product_ids, query):
        from app.services.catalog_search_service import covers_all_words, make_plan

        plan = make_plan(self.vocabulary, query)
        return bool(plan) and any(covers_all_words(self.by_id[pid]["_tokens"], plan) for pid in product_ids)

    async def orders(self, db, user_id, reference, lang, *, recent=False):
        return {
            "reply": "Your latest orders are below.",
            "orders": [{"order_number": "SB-10423", "status_label": "Out for delivery", "freshness": "live"}],
        }

    async def feature(self, name, args, ctx, lang):
        return {
            "reply": "Check the preview below and confirm it to apply.",
            "panels": [{"title": f"{name} preview", "commands": [{"type": "confirm"}]}],
        }


def grade_agent(case: Dict[str, Any], out: Dict[str, Any], calls: List[Dict[str, Any]]) -> List[str]:
    """Outcome checks for one agent turn: tools used, cards shown, reply."""
    expect = case["expect"]
    failures: List[str] = []
    used = [c["name"] for c in calls]
    searches = [c["args"] for c in calls if c["name"] == "search_products"]
    queries = [str(s.get("query", "")) for s in searches]
    shown = [p.get("name", "") for p in out.get("products") or []]
    reply = out.get("reply") or ""

    wanted = set().union(*(INTENT_TOOLS.get(i, set()) for i in expect["intent"]))
    chitchat_ok = "chitchat" in expect["intent"] and not set(used) - {"app_help"}
    if not chitchat_ok and not wanted & set(used):
        failures.append(f"tools {used or 'none'} want one of {sorted(wanted) or 'none'}")
    for tool in expect.get("agent_tools_all", []):
        if tool not in used:
            failures.append(f"did not call {tool}")
    if "lang" in expect and out.get("language") != expect["lang"]:
        failures.append(f"lang={out.get('language')} want {expect['lang']}")
    if expect.get("no_terms") and searches:
        failures.append(f"searched {queries} but should not")
    if "terms_any" in expect and not _hit(queries + shown, expect["terms_any"]):
        failures.append(f"searches {queries} / cards {shown[:3]} miss {expect['terms_any']}")
    if "terms_none" in expect and _hit(queries, expect["terms_none"]):
        failures.append(f"searches {queries} contain forbidden {expect['terms_none']}")
    for group in expect.get("terms_all", []):
        if not _hit(queries + shown, group):
            failures.append(f"searches {queries} miss one of {group}")
    if "brand_any" in expect and not _hit(
        [str(s.get("brand") or "") for s in searches] + queries, expect["brand_any"]
    ):
        failures.append(f"brand not searched, want {expect['brand_any']}")
    if "max_price" in expect and not any(s.get("max_price") == expect["max_price"] for s in searches):
        failures.append(f"max_price not used, want {expect['max_price']}")
    refs = [c["args"].get("order_reference") for c in calls if c["name"] == "my_orders"]
    if "order_reference" in expect and refs and expect["order_reference"] not in refs:
        failures.append(f"order_reference={refs} want {expect['order_reference']}")
    topics = [c["args"].get("topic") for c in calls if c["name"] == "app_help"]
    if "target_any" in expect and "app_help" in wanted and topics and not set(topics) & set(expect["target_any"]):
        failures.append(f"help topic={topics} want {expect['target_any']}")
    if "shows_any" in expect and not _hit(shown, expect["shows_any"]):
        failures.append(f"cards {shown[:4]} miss {expect['shows_any']}")
    for phrase in expect.get("forbid_reply", []):
        if phrase.casefold() in reply.casefold():
            failures.append(f"reply contains forbidden {phrase!r}")
    return failures


async def agent_trial(case, history, fixed, fallback, harness: "AgentHarness") -> Dict[str, Any]:
    import uuid

    async def emit(stage, **detail):
        return None

    harness.calls.clear()
    out = await asyncio.wait_for(
        harness.agent.run(
            db=None,
            redis=None,
            user_id=uuid.uuid4(),
            branch_id=uuid.uuid4(),
            message=case["message"],
            image_base64=None,
            image_mime_type=None,
            history=history,
            fixed_lang=fixed,
            fallback_lang=fallback,
            application_context=None,
            briefing_prompt="",
            emit=emit,
        ),
        timeout=45,
    )
    failures = grade_agent(case, out, harness.calls)
    return {
        "ok": not failures,
        "failures": failures,
        "tools": [f"{c['name']}({json.dumps(c['args'], ensure_ascii=False)[:60]})" for c in harness.calls],
        "reply": out.get("reply"),
        "shown": [p.get("name") for p in out.get("products") or []][:6],
    }


async def run(args: argparse.Namespace) -> int:
    from app.config.settings import settings
    from app.services import chat_language
    from app.services.chat_service import ChatService
    from app.services.embedding_service import GeminiAPIError

    if args.model:
        settings.gemini_generation_model = args.model
    meter = UsageMeter()
    original_log = ChatService._log_usage

    def _log(usage):
        meter.record(usage)
        original_log(usage)

    ChatService._log_usage = staticmethod(_log)

    cases = [c for c in load_cases() if not args.flow or c["flow"] == args.flow]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    cases = cases[: args.limit] if args.limit else cases
    # The agent makes about two model calls per message; budget for three.
    planned = len(cases) * args.k * (3 if args.agent else 1)
    harness = AgentHarness() if args.agent else None
    if harness and args.semantic:
        await harness.enable_semantic()
    if planned > args.max_calls:
        print(f"{planned} calls planned, over --max-calls {args.max_calls}; lower --k or --limit")
        return 2

    results = []
    started = time.monotonic()
    for case in cases:
        history = case.get("history", [])
        fallback = chat_language.last_language(history) or case.get("app_lang", "si")
        fixed = chat_language.fixed_language(case["message"], fallback)
        trials = []
        for _ in range(args.k):
            t0 = time.monotonic()
            n_calls_before = len(meter.calls)
            try:
                if args.agent:
                    trial = await agent_trial(case, history, fixed, fallback, harness)
                else:
                    plan = await asyncio.wait_for(
                        ChatService._ask_gemini(
                            case["message"], history, None, None, lang=fixed, fallback_lang=fallback
                        ),
                        timeout=30,
                    )
                    language = chat_language.final_language(
                        fixed, plan.get("reply_language"), fallback, reply=plan.get("reply")
                    )
                    failures = grade(case, plan, language)
                    trial = {
                        "ok": not failures,
                        "failures": failures,
                        "intent": plan.get("intent"),
                        "terms": plan.get("search_terms"),
                        "reply": plan.get("reply"),
                    }
                trial["seconds"] = round(time.monotonic() - t0, 2)
                trial["model_calls"] = len(meter.calls) - n_calls_before
                trials.append(trial)
            except Exception as exc:  # noqa: BLE001 - a crash is a failed trial, not the end of the run
                if not isinstance(exc, (GeminiAPIError, asyncio.TimeoutError)):
                    exc = RuntimeError(f"CRASH {type(exc).__name__}: {exc}")
                trials.append(
                    {
                        "ok": False,
                        "failures": [f"error {type(exc).__name__}: {str(exc)[:80]}"],
                        "seconds": round(time.monotonic() - t0, 2),
                    }
                )
            if args.delay:
                await asyncio.sleep(args.delay)
        passes = sum(t["ok"] for t in trials)
        results.append(
            {"id": case["id"], "flow": case["flow"], "passes": passes, "k": args.k, "trials": trials}
        )
        mark = "PASS" if passes == args.k else ("FLAKY" if passes else "FAIL")
        print(f"{mark:5} {passes}/{args.k} {case['id']}")
        for t in trials:
            for failure in t["failures"]:
                print(f"        - {failure}")

    price = {"input": args.price_in, "cached": args.price_cached, "output": args.price_out}
    n_calls = max(len(meter.calls), 1)
    summary = {
        "mode": ("agent+semantic" if args.semantic else "agent") if args.agent else "planner",
        "model": settings.gemini_generation_model,
        "avg_model_calls_per_message": round(
            len(meter.calls) / max(sum(len(r["trials"]) for r in results), 1), 2
        ),
        "cases": len(results),
        "k": args.k,
        "pass_hat_k": round(sum(r["passes"] == r["k"] for r in results) / max(len(results), 1), 3),
        "pass_at_1": round(
            sum(r["passes"] for r in results) / max(len(results) * args.k, 1), 3
        ),
        "calls": len(meter.calls),
        "avg_prompt_tokens": round(sum(c["prompt"] for c in meter.calls) / n_calls),
        "avg_cached_tokens": round(sum(c["cached"] for c in meter.calls) / n_calls),
        "avg_output_tokens": round(sum(c["output"] for c in meter.calls) / n_calls),
        "usd_per_call": round(meter.usd(price) / n_calls, 6),
        "lkr_per_call": round(meter.usd(price) / n_calls * args.usd_lkr, 3),
        "avg_seconds": round(
            sum(t["seconds"] for r in results for t in r["trials"])
            / max(sum(len(r["trials"]) for r in results), 1),
            2,
        ),
        "wall_seconds": round(time.monotonic() - started, 1),
    }
    by_flow: Dict[str, List[int]] = {}
    for r in results:
        by_flow.setdefault(r["flow"], []).append(int(r["passes"] == r["k"]))
    summary["pass_hat_k_by_flow"] = {f: round(sum(v) / len(v), 2) for f, v in by_flow.items()}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(
            json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return 0 if summary["pass_hat_k"] == 1 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--live", action="store_true", help="required: uses the configured key and quota")
    parser.add_argument("--k", type=int, default=3, help="trials per case (pass^k)")
    parser.add_argument("--flow", help="only cases of this flow")
    parser.add_argument("--limit", type=int, help="first N cases only")
    parser.add_argument("--max-calls", type=int, default=60, help="refuse to run more calls than this")
    parser.add_argument("--delay", type=float, default=0.5, help="seconds between calls")
    parser.add_argument("--model", help="override GEMINI_GENERATION_MODEL for this run")
    parser.add_argument(
        "--agent",
        action="store_true",
        help="run the tool-using agent end to end against evals/assistant/catalog_snapshot.json",
    )
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument(
        "--semantic",
        action="store_true",
        help="with --agent: embed the snapshot and turn on meaning-based lookup",
    )
    parser.add_argument("--price-in", type=float, default=DEFAULT_PRICE["input"])
    parser.add_argument("--price-cached", type=float, default=DEFAULT_PRICE["cached"])
    parser.add_argument("--price-out", type=float, default=DEFAULT_PRICE["output"])
    parser.add_argument("--usd-lkr", type=float, default=300.0)
    parser.add_argument("--out", help="write the full JSON report here")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this command consumes provider quota")
    raise SystemExit(asyncio.run(run(args)))
