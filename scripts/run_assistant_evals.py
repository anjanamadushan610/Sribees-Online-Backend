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
    cases = cases[: args.limit] if args.limit else cases
    planned = len(cases) * args.k
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
            try:
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
                trials.append(
                    {
                        "ok": not failures,
                        "failures": failures,
                        "intent": plan.get("intent"),
                        "terms": plan.get("search_terms"),
                        "reply": plan.get("reply"),
                        "seconds": round(time.monotonic() - t0, 2),
                    }
                )
            except (GeminiAPIError, asyncio.TimeoutError) as exc:
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
        "model": settings.gemini_generation_model,
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
    parser.add_argument("--price-in", type=float, default=DEFAULT_PRICE["input"])
    parser.add_argument("--price-cached", type=float, default=DEFAULT_PRICE["cached"])
    parser.add_argument("--price-out", type=float, default=DEFAULT_PRICE["output"])
    parser.add_argument("--usd-lkr", type=float, default=300.0)
    parser.add_argument("--out", help="write the full JSON report here")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this command consumes provider quota")
    raise SystemExit(asyncio.run(run(args)))
