"""Offline benchmark for the customer search bar -- no database, no API, no cost.

Indexes the production catalogue snapshot (evals/assistant/catalog_snapshot.json)
exactly as ``catalog_search_service`` does when a product is saved, then runs
every query in ``evals/search/queries.tsv`` through the real planner and
ranker. A query passes when one of the top three products' English names contains
one of the expected strings, or sits in the expected sub-category
("sub:Biscuits") -- many products are named without their kind.

    python -m scripts.search_benchmark            # summary + failures
    python -m scripts.search_benchmark --all      # every query with its top 3
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from app.services.catalog_search_service import Vocabulary, make_plan, rank
from app.services.search_text import index_tokens

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "evals" / "assistant" / "catalog_snapshot.json"
QUERIES = ROOT / "evals" / "search" / "queries.tsv"
TOP = 3


def load_catalog(path: Path = SNAPSHOT):
    rows, names = [], {}
    for p in json.loads(path.read_text(encoding="utf-8"))["products"]:
        tokens, name_tokens, phrases = index_tokens(
            p["name"], p.get("name_si"), p.get("name_ta"), p.get("search_keywords")
        )
        rows.append((p["product_id"], tokens, name_tokens, phrases, False, 0, p["name"]))
        names[p["product_id"]] = (p["name"], p.get("subcategory") or "")
    return rows, names


def load_queries(path: Path = QUERIES):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        query, expect, kind = line.split("\t")
        out.append((query, [e.strip() for e in expect.split("|")], kind))
    return out


def search(rows, vocabulary, query: str):
    """What the search bar returns for ``query``: product ids best-first."""
    plan = make_plan(vocabulary, query)
    if plan is None:
        return []
    terms = set(plan.terms)
    candidates = [r for r in rows if terms & set(r[1])]
    return rank(candidates, plan)


def matches(item, expect) -> bool:
    name, sub = item
    for e in expect:
        if e.startswith("sub:"):
            if e[4:].casefold() == sub.casefold():
                return True
        elif e.casefold() in name.casefold():
            return True
    return False


def run(show_all: bool = False) -> dict:
    rows, names = load_catalog()
    vocabulary = Vocabulary(Counter(t for r in rows for t in set(r[1])))
    by_kind = defaultdict(lambda: [0, 0])
    failures, reciprocal = [], 0.0
    queries = load_queries()
    for query, expect, kind in queries:
        ranked = [names[pid] for pid in search(rows, vocabulary, query)]
        position = next((i for i, item in enumerate(ranked) if matches(item, expect)), None)
        ok = position is not None and position < TOP
        reciprocal += 1 / (position + 1) if position is not None else 0
        by_kind[kind][0] += ok
        by_kind[kind][1] += 1
        if not ok:
            failures.append((query, expect, kind, [n for n, _ in ranked[:TOP]], len(ranked)))
        if show_all:
            print(f"{'OK  ' if ok else 'MISS'} {query!r:28} -> {[n for n, _ in ranked[:TOP]]}")
    passed = sum(v[0] for v in by_kind.values())
    summary = {
        "queries": len(queries),
        "top3": passed,
        "top3_rate": round(passed / len(queries), 3),
        "mrr": round(reciprocal / len(queries), 3),
        "by_kind": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_kind.items())},
    }
    return {"summary": summary, "failures": failures}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="print every query with its top three")
    args = parser.parse_args()
    result = run(args.all)
    for query, expect, kind, top, total in result["failures"]:
        print(f"MISS [{kind}] {query!r} want {expect} -> {top} ({total} results)")
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
