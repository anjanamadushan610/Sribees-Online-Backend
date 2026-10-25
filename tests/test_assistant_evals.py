"""Offline checks for the assistant eval suite: the case file and the graders."""

from scripts.run_assistant_evals import UsageMeter, grade, load_cases

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


def test_usage_meter_prices_cached_tokens_cheaper():
    meter = UsageMeter()
    meter.record({"promptTokenCount": 1000, "cachedContentTokenCount": 800, "candidatesTokenCount": 100})
    price = {"input": 1.0, "cached": 0.1, "output": 4.0}
    assert abs(meter.usd(price) - (200 * 1.0 + 800 * 0.1 + 100 * 4.0) / 1_000_000) < 1e-12
