"""Offline evaluation of the free fast path -- no model, no database, no cost.

Runs every case in evals/assistant/cases.jsonl (and evals/assistant/fast_cases.jsonl)
through the L2 rules with the production catalogue snapshot as the
vocabulary, then executes each claimed plan through the real engine against
the snapshot (the same AgentHarness the agent evals use). Reports:

* coverage  -- share of cases the fast path answers itself;
* precision -- of those, the share whose decision (tool, language, search
  words, cards) matches the case's expectation. Must stay >= 99 %;
* fall-backs -- plans dropped at execution because a search found nothing.

    python -m scripts.hybrid_eval            # summary + every wrong claim
    python -m scripts.hybrid_eval --all      # also list what each case did
"""

import argparse
import asyncio
import json
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASE_FILES = [ROOT / "evals" / "assistant" / "cases.jsonl", ROOT / "evals" / "assistant" / "fast_cases.jsonl"]

# The planner intents a fast-path plan stands for.
def plan_intent(plan) -> str:
    if not plan.steps:
        return "chitchat"
    step = plan.steps[0]
    if step.name == "search_products":
        return "buy_product"
    if step.name == "my_orders":
        return "recent_orders" if step.args.get("recent") else "track_order"
    if step.name == "app_help":
        return "navigate" if step.args.get("open_page") else "app_help"
    if step.name == "stage_basket_change":
        return "cart_edit"
    return step.name


def _hit(values, needles) -> bool:
    values = [v.casefold() for v in values if v]
    return any(n.casefold() in v for v in values for n in needles)


def judge(case, plan, out) -> list:
    """Why a claimed case is wrong; empty when the fast answer is right."""
    expect = case["expect"]
    wrong = []
    intent = plan_intent(plan)
    if intent not in expect["intent"]:
        wrong.append(f"intent {intent} not in {expect['intent']}")
    if expect.get("lang") and plan.language != expect["lang"]:
        wrong.append(f"lang {plan.language} want {expect['lang']}")
    queries = [s.args.get("query", "") for s in plan.steps] + [
        op.get("query", "") for s in plan.steps for op in s.args.get("operations", [])
    ]
    shown = [p.get("name", "") for p in (out or {}).get("products") or []]
    if "terms_any" in expect and not _hit(queries + shown, expect["terms_any"]):
        wrong.append(f"queries {queries} miss {expect['terms_any']}")
    for group in expect.get("terms_all", []):
        if not _hit(queries + shown, group):
            wrong.append(f"queries {queries} miss one of {group}")
    if "shows_any" in expect and out is not None and not _hit(shown, expect["shows_any"]):
        wrong.append(f"cards {shown[:3]} miss {expect['shows_any']}")
    if expect.get("no_terms") and any(s.name == "search_products" for s in plan.steps):
        wrong.append("searched but should not")
    if "target_any" in expect:
        topics = [s.args.get("topic") for s in plan.steps if s.name == "app_help"]
        if topics and not set(topics) & set(expect["target_any"]):
            wrong.append(f"topic {topics} want {expect['target_any']}")
    if "order_reference" in expect:
        refs = [s.args.get("order_reference") for s in plan.steps if s.name == "my_orders"]
        if refs and expect["order_reference"] not in refs:
            wrong.append(f"order_reference {refs} want {expect['order_reference']}")
    for tool in expect.get("agent_tools_all", []):
        if tool not in [s.name for s in plan.steps]:
            wrong.append(f"multi-request case answered with only {[s.name for s in plan.steps]}")
            break
    for phrase in expect.get("forbid_reply", []):
        if out and phrase.casefold() in (out.get("reply") or "").casefold():
            wrong.append(f"reply contains forbidden {phrase!r}")
    return wrong


async def run(show_all: bool = False) -> dict:
    from app.services.catalog_search_service import Vocabulary
    from app.services.hybrid import engine
    from app.services.hybrid.normalize import parse
    from app.services.hybrid.rules import route
    from scripts.run_assistant_evals import AgentHarness, load_cases

    harness = AgentHarness()
    vocabulary = Vocabulary(Counter(t for p in harness.products for t in set(p["_tokens"])))
    cases = []
    for path in CASE_FILES:
        if path.exists():
            cases += load_cases(path)

    async def emit(stage, **detail):
        return None

    claimed = correct = dropped = 0
    wrong_cases = []
    by_rule = Counter()
    for case in cases:
        history = case.get("history", [])
        fallback = next((t.get("language") for t in reversed(history) if t.get("role") == "assistant" and t.get("language")), "si")
        plan = route(parse(case["message"]), vocabulary, fallback)
        if plan is None:
            if show_all:
                print(f"MODEL  {case['id']}")
            continue
        out = await engine.run_plan(
            plan, db=None, redis=None, user_id=uuid.uuid4(), branch_id=uuid.uuid4(), history=history, emit=emit
        )
        if out is None:
            dropped += 1
            if show_all:
                print(f"DROP   {case['id']} ({plan.rule}: a search found nothing -> model)")
            continue
        claimed += 1
        by_rule[plan.rule] += 1
        problems = judge(case, plan, out)
        if problems:
            wrong_cases.append((case["id"], plan.rule, problems, out.get("reply")))
        else:
            correct += 1
        if show_all:
            print(f"{'OK    ' if not problems else 'WRONG '}{case['id']:30} {plan.rule:16} {out.get('reply', '')[:90]!r}")
    total = len(cases)
    summary = {
        "cases": total,
        "answered_free": claimed,
        "coverage": round(claimed / total, 3) if total else 0,
        "precision": round(correct / claimed, 3) if claimed else 1.0,
        "dropped_to_model_after_search": dropped,
        "by_rule": dict(by_rule.most_common()),
    }
    return {"summary": summary, "wrong": wrong_cases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args.all))
    for cid, rule, problems, reply in result["wrong"]:
        print(f"WRONG {cid} [{rule}] {problems} reply={reply!r}")
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
