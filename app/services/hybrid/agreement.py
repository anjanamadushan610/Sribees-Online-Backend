"""
Did the fast path's plan agree with what the model did? (P2 of the roadmap)

In shadow mode every turn records both: the rule's plan and the model's tool
trace. A rule is promoted to answer for itself only when it agrees with the
model on nearly every turn it claims. "Agree" means the same thing happened,
not the same bytes:

* the same tools, regardless of order or repeats;
* a catalogue search looked for the same thing -- the rule's words and the
  model's words share a folded token (the model often searches the English
  word for a Singlish one: "seeni" vs "sugar" -- the catalogue keywords make
  both find the same products, so overlap is judged on folded tokens and,
  where the model used a different word, on the products found);
* the same order reference, help topic, or basket item.
"""

from typing import Any, Dict, List, Optional

from app.services.search_text import tokenize


def _tools(steps: List[Dict[str, Any]]) -> set:
    return {s.get("name") for s in steps if s.get("name")}


def _search_terms(steps: List[Dict[str, Any]]) -> set:
    terms = set()
    for s in steps:
        if s.get("name") == "search_products":
            terms.update(tokenize(str((s.get("args") or {}).get("query", ""))))
    return terms


def agree(shadow_plan: Optional[Dict[str, Any]], model_trace: Optional[List[Dict[str, Any]]], same_products: Optional[bool] = None) -> Optional[bool]:
    """
    True / False when both sides exist; None when there is nothing to compare
    (no shadow plan, or the model answered without tools, e.g. chit-chat --
    those are judged by the reply kind instead: a social rule agrees with a
    tool-less model turn).
    """
    if not shadow_plan:
        return None
    rule_steps = shadow_plan.get("steps") or []
    model_steps = model_trace or []
    if not rule_steps:  # greetings and thanks
        return not model_steps
    if _tools(rule_steps) != _tools(model_steps):
        return False
    names = _tools(rule_steps)
    if "search_products" in names:
        if not (_search_terms(rule_steps) & _search_terms(model_steps)) and not same_products:
            return False
    for tool, key in (("my_orders", "order_reference"), ("app_help", "topic")):
        if tool in names:
            ours = {(s.get("args") or {}).get(key) for s in rule_steps if s.get("name") == tool}
            theirs = {(s.get("args") or {}).get(key) for s in model_steps if s.get("name") == tool}
            if ours != theirs:
                return False
    if "my_orders" in names:
        ours = {bool((s.get("args") or {}).get("recent")) for s in rule_steps if s.get("name") == "my_orders"}
        theirs = {bool((s.get("args") or {}).get("recent")) for s in model_steps if s.get("name") == "my_orders"}
        if ours != theirs:
            return False
    return True
