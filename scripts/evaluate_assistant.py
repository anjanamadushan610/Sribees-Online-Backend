"""Opt-in live intent/language smoke test using synthetic Sri Lankan utterances.

Run from the backend root: python -m scripts.evaluate_assistant --live --limit 3
Uses the configured Gemini model/key and consumes provider quota. No DB access.
"""

import argparse
import asyncio
import json

from app.services.chat_service import ChatService
from app.services.embedding_service import GeminiAPIError

CASES = [
    ("singlish_tracking", "mage order eka dan koheda?", "track_order", None),
    ("singlish_navigation", "mage cart page eka open karanna", "navigate", "cart"),
    ("sinhala_guide", "මේ ඇප් එකෙන් ඇණවුමක් දාන්නේ කොහොමද?", "app_help", "checkout"),
    ("order_history", "මම කලින් දාපු ඇණවුම් පෙන්නන්න", "recent_orders", None),
    ("sinhala_product", "මට කිරි පැකට් එකක් ගන්න ඕන", "buy_product", None),
    ("recipe", "mata cake ekak hadanna one", "recipe_ingredients", None),
    ("buy_not_recipe", "mata chocolate cake ekak ganna one", "buy_product", None),
    ("greeting", "kohomada oya?", "chitchat", None),
]


COMMERCE_CASES = [
    ("cart_change", "cart eke thiyena kiri packet gana 3 karanna", "cart_edit", None),
    ("compare_prices", "කිරිපිටි පැකට්වල මිල සසඳලා පෙන්නන්න", "compare_products", None),
    ("alternatives", "මේ කිරි වෙනුවට වෙන brand එකක කිරි පෙන්නන්න", "alternatives", None),
    ("save_preference", "අපේ ගෙදර 4 දෙනෙක් ඉන්නවා. ඒක මතක තියාගන්න", "memory", None),
    ("reminder", "මට දවස් 7කට වරක් කිරි ගන්න මතක් කරන්න", "reminders", None),
    (
        "pantry",
        "ගෙදර බත්, බිත්තර, ලූනු තියෙනවා. ඒවයින් දෙන්නෙකුට කෑමක් හදන හැටි කියන්න",
        "pantry_recipe",
        None,
    ),
]


async def evaluate(limit: int, suite: str = "core") -> int:
    failed = 0
    cases = COMMERCE_CASES if suite == "commerce" else CASES
    for case_id, message, intent, target in cases[:limit]:
        try:
            result = await asyncio.wait_for(
                ChatService._ask_gemini(message, [], None, None, lang="si"), timeout=30
            )
        except (GeminiAPIError, asyncio.TimeoutError) as exc:
            print(
                json.dumps(
                    {
                        "case": case_id,
                        "error": type(exc).__name__,
                        "detail": str(exc)[:100],
                        "cause": type(exc.__cause__).__name__
                        if exc.__cause__
                        else None,
                        "status_code": getattr(exc, "status_code", None),
                    }
                )
            )
            return 2
        script_ok = any(0x0D80 <= ord(c) <= 0x0DFF for c in result["reply"])
        passed = (
            result["intent"] == intent
            and script_ok
            and (target is None or result["target"] == target)
        )
        failed += not passed
        print(
            json.dumps(
                {
                    "case": case_id,
                    "passed": passed,
                    "intent": result["intent"],
                    "target": result["target"],
                    "sinhala_script": script_ok,
                }
            )
        )
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use configured provider credentials and quota",
    )
    parser.add_argument(
        "--limit", type=int, choices=range(1, len(CASES) + 1), default=len(CASES)
    )
    parser.add_argument("--suite", choices=["core", "commerce"], default="core")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this command consumes provider quota")
    raise SystemExit(asyncio.run(evaluate(args.limit, args.suite)))
