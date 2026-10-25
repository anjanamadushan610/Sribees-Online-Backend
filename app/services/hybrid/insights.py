"""
What the hybrid assistant did over the last N days, from assistant_turns.
Shared by the admin page (/admin/assistant-insights) and scripts/hybrid_report.py.
No model calls.

Share of turns per layer (L1 memory, L2 rules, L4 model, canary, degraded),
latency per layer, model tokens and an LKR estimate, and -- per fast-path
rule or learned pattern -- how often its plan agreed with what the model
did. When a rule and the model searched different words ("seeni" / "sugar"),
the catalogue is asked for both in that turn's branch and the top results
are compared, so a translation is not counted as a disagreement.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

USD_PER_M = {"input": 0.75, "output": 3.75}  # gemini-3.8-flash list price, 2026
USD_LKR = 300.0
PROMOTE_AT = 0.98
MIN_TURNS = 30


def _pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


async def _same_products(db, branch_id, rule_steps, model_steps) -> bool:
    from app.services.product_service import ProductService

    async def top(steps):
        ids = set()
        for s in steps:
            if s.get("name") == "search_products" and branch_id:
                items, _ = await ProductService.get_all_for_branch(
                    db, branch_id=branch_id, search=str((s.get("args") or {}).get("query", "")), limit=3
                )
                ids |= {str(i["product"].product_id) for i in items}
        return ids

    ours, theirs = await top(rule_steps), await top(model_steps)
    return bool(ours & theirs)


async def summary(db, days: int = 14) -> dict:
    from sqlalchemy import select

    from app.models.assistant import AssistantTurn
    from app.services.hybrid.agreement import agree

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(select(AssistantTurn).where(AssistantTurn.created_at >= since))).scalars().all()
    layers = defaultdict(list)
    rules = defaultdict(lambda: {"turns": 0, "agree": 0, "disagree": 0, "examples": []})
    prompt = output = 0
    for r in rows:
        layers[r.layer].append(r.latency_ms or 0)
        prompt += r.prompt_tokens or 0
        output += r.output_tokens or 0
        plan = r.shadow_plan if r.layer == "L4" else (r.plan if r.layer in ("L1", "L2", "canary") else None)
        if not plan or not plan.get("rule"):
            continue
        stats = rules[plan["rule"]]
        stats["turns"] += r.layer != "canary"
        if r.layer not in ("L4", "canary"):
            continue  # answered by the rule itself with no model to compare
        verdict = agree(plan, r.model_trace)
        if verdict is False and "search_products" in {s.get("name") for s in plan.get("steps", [])}:
            if await _same_products(db, r.branch_id, plan.get("steps", []), r.model_trace or []):
                verdict = True
        if verdict is True:
            stats["agree"] += 1
        elif verdict is False:
            stats["disagree"] += 1
            if len(stats["examples"]) < 5:
                stats["examples"].append({"text": r.text_key, "rule": plan.get("steps"), "model": r.model_trace})

    total = len(rows)
    usd = (prompt * USD_PER_M["input"] + output * USD_PER_M["output"]) / 1_000_000
    result = {
        "days": days,
        "turns": total,
        "share_by_layer": {k: round(len(v) / total, 3) for k, v in layers.items()} if total else {},
        "latency_ms_p50_p95": {k: (_pct(v, 0.5), _pct(v, 0.95)) for k, v in layers.items()},
        "model_tokens": {"prompt": prompt, "output": output},
        "model_cost_lkr": round(usd * USD_LKR, 2),
        "rules": {},
    }
    for rule, s in sorted(rules.items(), key=lambda kv: -kv[1]["turns"]):
        judged = s["agree"] + s["disagree"]
        rate = round(s["agree"] / judged, 3) if judged else None
        result["rules"][rule] = {
            "turns": s["turns"],
            "agreement": rate,
            "judged": judged,
            "ready_to_activate": bool(rate is not None and rate >= PROMOTE_AT and judged >= MIN_TURNS),
            "disagreements": s["examples"],
        }
    return result


