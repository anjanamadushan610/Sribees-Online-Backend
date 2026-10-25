"""Offline checks for the assistant eval suite: the case file and the graders."""

import pytest

from scripts.run_assistant_evals import (
    AgentHarness,
    UsageMeter,
    grade,
    grade_agent,
    load_cases,
)

INTENTS = {
    "buy_product", "recipe_ingredients", "chitchat", "track_order", "recent_orders",
    "app_help", "navigate", "cart_edit", "compare_products", "alternatives",
    "memory", "reminders", "pantry_recipe",
}


def _plan(**over):
    plan = {
        "reply": "ගැළපෙන භාණ්ඩ",
        "intent": "buy_product",
        "search_terms": ["milk powder"],
        "brand": None,
        "variant": None,
        "max_price": None,
        "order_reference": None,
        "target": None,
    }
    plan.update(over)
    return plan


def test_case_file_is_well_formed():
    cases = load_cases()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)) >= 50
    for case in cases:
        assert case["message"].strip()
        assert set(case["expect"]["intent"]) <= INTENTS, case["id"]
        assert case["expect"].get("lang") in (None, "si", "ta", "en"), case["id"]


def test_suite_has_negative_and_safety_cases():
    flows = {c["flow"] for c in load_cases()}
    assert {"negative", "safety", "multiturn", "multi"} <= flows


def test_grade_passes_matching_plan():
    case = {"expect": {"intent": ["buy_product"], "lang": "si", "terms_any": ["milk powder"]}}
    assert grade(case, _plan(), "si") == []


def test_grade_reports_each_failure():
    case = {
        "expect": {
            "intent": ["track_order"],
            "lang": "en",
            "no_terms": True,
            "forbid_reply": ["භාණ්ඩ"],
        }
    }
    failures = grade(case, _plan(), "si")
    assert len(failures) == 4


def test_terms_none_and_terms_all():
    case = {
        "expect": {
            "intent": ["buy_product"],
            "terms_none": ["flour"],
            "terms_all": [["sugar"], ["dhal", "dal"]],
        }
    }
    assert grade(case, _plan(search_terms=["sugar", "red dal"]), "en") == []
    failures = grade(case, _plan(search_terms=["sugar", "flour"]), "en")
    assert any("forbidden" in f for f in failures)
    assert any("dhal" in f for f in failures)


def test_order_reference_must_not_be_invented():
    case = {"expect": {"intent": ["app_help"], "order_reference": None}}
    assert grade(case, _plan(intent="app_help", search_terms=[], order_reference="SB-1"), "si")


@pytest.fixture
def harness(monkeypatch):
    from app.services import assistant_agent
    from app.services.chat_service import ChatService

    # Recorded so monkeypatch restores what the harness replaces.
    monkeypatch.setattr(ChatService, "_match_products", ChatService._match_products)
    for name in ("orders_result", "_feature", "_run_tool"):
        monkeypatch.setattr(assistant_agent, name, getattr(assistant_agent, name))
    return AgentHarness()


@pytest.mark.asyncio
async def test_snapshot_search_uses_sinhala_names_and_singlish_keywords(harness):
    for query in ("kiripiti", "කිරිපිටි", "maduru dagara"):
        strict, broad = await harness.match(None, None, [query], lang="si")
        assert strict, query
    strict, _ = await harness.match(None, None, ["milk powder"], brand="Anchor", variant="400g", lang="en")
    assert strict and all("Anchor" in p["name"] and "400g" in p["name"] for p in strict)


def test_grade_agent_checks_tools_cards_and_reply():
    case = {
        "expect": {
            "intent": ["buy_product", "track_order"],
            "lang": "si",
            "agent_tools_all": ["search_products", "my_orders"],
            "shows_any": ["සීනි"],
            "forbid_reply": ["විනාඩි"],
        }
    }
    calls = [{"name": "search_products", "args": {"query": "sugar"}}, {"name": "my_orders", "args": {}}]
    out = {"reply": "මෙන්න.", "language": "si", "products": [{"name": "සුදු සීනි 1kg"}]}
    assert grade_agent(case, out, calls) == []
    failures = grade_agent(case, {**out, "products": []}, calls[:1])
    assert any("my_orders" in f for f in failures) and any("cards" in f for f in failures)


def test_grade_agent_chitchat_must_not_search():
    case = {"expect": {"intent": ["chitchat"], "no_terms": True}}
    assert grade_agent(case, {"reply": "hi"}, []) == []
    assert grade_agent(case, {"reply": "hi"}, [{"name": "search_products", "args": {"query": "x"}}])


def test_usage_meter_prices_cached_tokens_cheaper():
    meter = UsageMeter()
    meter.record({"promptTokenCount": 1000, "cachedContentTokenCount": 800, "candidatesTokenCount": 100})
    price = {"input": 1.0, "cached": 0.1, "output": 4.0}
    assert abs(meter.usd(price) - (200 * 1.0 + 800 * 0.1 + 100 * 4.0) / 1_000_000) < 1e-12
