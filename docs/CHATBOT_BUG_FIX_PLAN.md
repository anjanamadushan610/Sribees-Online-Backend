# AI Shopping Chatbot — Bug Fix Plan

Source reports: `C:\Users\Lenovo\Downloads\chatbot\` (bug01–bug04, QA-reported, screenshots + `chatbot_bugs.md`).

Code: [chat_service.py](../app/services/chat_service.py), [chat.py](../app/api/v1/chat.py).

## Root cause (one architectural flaw, not four separate bugs)

`ChatService.chat()` made **one** Gemini call that produced *both* the free-text
conversational `reply` *and* the `search_terms`, before any database lookup ran.
Gemini has no idea what's actually in stock, so it improvised confident-sounding
answers ("Ow, thiyenawa!") — then a completely separate, brand/size/price-blind
keyword search ran afterward. The reply and the product list were never
cross-checked against each other. That single gap explains every report:

| # | Symptom | Why |
|---|---|---|
| 1 | Hallucinates Kotmale/Pelwatte milk, Vaseline lotion that don't exist | Reply generated with zero DB visibility |
| 1 | Shows generic "Fresh Milk" card instead of saying the brand is missing | No brand ever extracted/filtered — `_match_products` searched only generic terms |
| 2 | "cake" search shows butter | System prompt's own few-shot example ("a cake" → flour/sugar/eggs/**butter**) fired even when the customer wanted to *buy* a cake, not bake one |
| 2 | Follow-ups ("ko", "ko nane") return no product card + a made-up excuse ("podi delay ekakata") | Each Gemini call only looked at the current message; a pronoun-only follow-up produced empty `search_terms`, and the reply was still invented independently |
| 3 | 330ml shampoo confirmed when only 440ml exists | No variant/size was ever extracted or filtered — `_match_products` had no such parameter |
| 3 | Rs. 2,450 shampoo shown for a "< Rs. 2,000" request | `max_price`/`min_price` already exist on `ProductService.get_all_for_branch` — chat simply never passed them through |
| 4 | Face wash / cleanser / moisturizer conflated | Loose fuzzy/description matching in the shared search with no category-relevance check on the chat side |

## Fix — grounded, two-stage pipeline (single Gemini call, kept inside the existing free-tier quota)

1. **System prompt rewritten** ([chat_service.py](../app/services/chat_service.py)):
   - Gemini is told it has **no inventory visibility** and must never assert
     availability either way — reply stays a neutral acknowledgement.
   - Explicit `intent` classification: `buy_product` vs `recipe_ingredients`
     vs `chitchat`, with the cake case called out by name so "I want to buy a
     cake" no longer degrades into an ingredient list.
   - Structured extraction added: `brand`, `variant`, `max_price`, `min_price`,
     `is_followup` — instead of folding everything into fuzzy `search_terms`.
   - Told to keep the customer's own category word (e.g. "face wash") rather
     than substituting a related-but-different one ("cleanser").
2. **`_match_products` rewritten** to:
   - Forward `max_price`/`min_price` into `ProductService.get_all_for_branch`
     (that filter already existed in the DB layer — chat just never used it).
   - Filter candidates to ones actually relevant to the searched word (name/
     category/sub-category token overlap), so a loosely-matched description
     doesn't surface an unrelated product.
   - Return both a *strict* match list (brand/variant enforced) and a
     *broader* one (before that enforcement), so the reply can say "we don't
     have the 330ml, but here's the 440ml" using a **real** product name
     instead of the LLM's guess.
3. **Deterministic reply grounding** (`_ground_reply`): once the real matches
   are known, the LLM's reply is overridden with a plain, DB-grounded
   sentence whenever it *can't* have known the truth — zero matches → "not
   available"; brand/variant unavailable but a close match exists → names the
   real alternative. This removes the LLM as the source of truth for
   availability entirely, which is what caused the hallucination and the fake
   "podi delay" apology.
4. **Follow-up context carry-forward**: the last turn's `search_terms` /
   `brand` / `variant` / price bounds are now stored in Redis chat history and
   reused when a message is a pronoun-only follow-up ("ko", "ko nane") with no
   new product info — fixing the missing-card bug on the second/third message
   of a thread.

## Status

- [x] Read all 4 bug folders + `chatbot_bugs.md` in full before touching code.
- [x] Root-caused against the real `chat_service.py` / `product_service.py`.
- [x] Rewrite system prompt (grounding + intent + structured extraction).
- [x] Rewrite `_match_products` (price filter wiring, brand/variant/category
      relevance filtering, strict + broad match sets).
- [x] Add deterministic `_ground_reply`.
- [x] Add follow-up search-context carry-forward via Redis history.
- [x] Sanity-check with `python -m py_compile` / import (no live Gemini key in
      this environment, so no end-to-end call was made — see "Not done"
      below).

## Not done / needs a human before shipping

- **No live re-test against Gemini** — this dev machine has no working
  `GEMINI_API_KEY` session budget to burn (prod key is capped at 20
  req/day/model), so the new prompt's JSON shape was verified by code review
  and local parsing logic only, not a real round-trip. Re-run the exact 4 bug
  scenarios in staging before considering this closed.
- Deterministic fallback sentences are plain English ("Sorry, we don't have
  X in stock right now.") rather than matching the customer's Sinhala/Singlish
  style the rest of the reply uses — correct and unambiguous, but a stylistic
  follow-up if product wants full language parity here.
- The category-crossover fix (bug04: cleanser/face wash/moisturizer) is a
  best-effort relevance filter scoped to the chat path only; it does not touch
  `ProductService`'s shared fuzzy-search thresholds used by the main search
  bar, since that has a much larger blast radius than this bug report covers.
  If cross-category bleed still shows up in staging, that's the next place to
  look.
