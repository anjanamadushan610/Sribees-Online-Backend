# The hybrid assistant: answer in milliseconds, learn from the model

Status: active by default since 2026-11-01 (`HYBRID_MODE`). A rule answers only
when one product matches every word of the search; otherwise the model answers.
If the model is down, `engine.rescue` shows cards for the product words and says
it is only a search.

Design, 2026-10-25. Owner's goal: the SRIBEES Online assistant should
answer "like Gemini" but, over the years, handle ~95% of messages without any
paid model call, in milliseconds, and get better the more it is used.

## 1. The one idea everything rests on

**Learn the decision, not the answer.**

A model's reply cannot be cached: "Anchor 400g kiyada?" was Rs 1,300 today and
may be Rs 1,350 or out of stock tomorrow. What *can* be reused is what the
model decided to do:

```
"Anchor 400g kiyada?"  ->  search_products(query="milk powder", brand="Anchor", size="400g")
                           respond(kind="price_answer", products=<results>)
```

So every turn the model handles becomes a training example of the form
**message -> plan** (tool calls with arguments, and the kind of answer). The
next time a message of the same shape arrives, the plan is replayed against
*live* tools -- live prices, live stock, the customer's real orders -- and the
reply is written from the results with a multilingual template. No model call,
no stale facts, a few milliseconds.

## 2. Request path: a cascade of answerers

Each layer either answers with confidence or passes the message down. Cheap
and certain first, expensive and flexible last.

```
customer message
      │
      ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │ L0  Normalise      search_text folding: Sinhala/Tamil script →      │
 │                    Singlish key space, spellings folded, numbers,    │
 │                    pack sizes and order references pulled out.      │
 ├──────────────────────────────────────────────────────────────────────┤
 │ L1  Exact memory   normalised text seen before with a verified plan │  ~0.1 ms
 ├──────────────────────────────────────────────────────────────────────┤
 │ L2  Rules          hand-written grammar for the frequent, safe      │  ~1 ms
 │                    shapes: a bare product name, "<X> one/thiyenawada│
 │                    /kiyada", order tracking, open-a-page, greetings │
 │                    and thanks, "<X> <n>k cart ekata danna"           │
 ├──────────────────────────────────────────────────────────────────────┤
 │ L3  Learned        templates mined from past model turns:           │  ~2 ms
 │     patterns       "mage <ORDER?> eka koheda" → my_orders;           │
 │                    "<PRODUCT> wenuwata wena brand" → alternatives;   │
 │                    slot filling from the catalogue vocabulary.       │
 │                    Only patterns promoted through shadow testing.    │
 ├──────────────────────────────────────────────────────────────────────┤
 │ L4  Model agent    the existing Gemini tool loop (assistant_agent)  │  5-10 s
 │                    for everything open-ended or uncertain.           │
 └──────────────────────────────────────────────────────────────────────┘
      │ every layer produces the same thing: a Plan
      ▼
 Executor: runs the plan's tools (catalogue search, own orders, previews…),
 Composer: writes the reply from the results (templates in en/si/ta, or the
           model's own validated sentence for L4), Guard: the same server
           checks the agent has today (ids from tools only, real prices).
      ▼
 Recorder: logs the turn (scrubbed), so the learner can improve L1/L3.
```

Routing rule: a fast layer answers only when it is *sure*. "Sure" is measured
(section 5), never assumed. When in doubt, the message goes to the model --
an unnecessary model call costs Rs 2; a wrong instant answer costs trust.

## 3. The Plan: one language for every layer

```python
Plan(
    steps=[ToolCall("search_products", {"query": "milk powder", "brand": "Anchor", "size": "400g"})],
    reply=ReplySpec(kind="price_answer"),   # how to word the answer
    source="L2", confidence=0.98, signature="price_q|<PRODUCT>|<SIZE>",
)
```

The executor and the tools are the ones the agent already uses, so every
safety property carries over: writes stay previews the customer confirms,
orders are looked up by the authenticated user only, product cards come from
tool results only.

## 4. The learning loop

```
 live turns ──► assistant_turns (scrubbed log)
                     │ nightly / on demand, no model calls
                     ▼
               Miner: normalise → abstract entities (<PRODUCT> <BRAND> <SIZE>
               <N> <ORDER>) → group model turns by signature → for each group,
               how consistent was the model's plan shape?
                     │ consistency ≥ 95 % over ≥ N examples
                     ▼
               assistant_patterns (status = candidate)
                     │
                     ▼
               Shadow: on live traffic the pattern's plan is computed but the
               model still answers; agreement is counted per pattern
                     │ agreement ≥ 98 % over ≥ M shadow turns
                     ▼
               status = active  → answered by L3, free
                     │
                     ▼
               Canary: a small share (e.g. 2 %, configurable, 0 = off) of
               active-pattern turns is re-checked by the model; disagreement
               demotes the pattern automatically. Shopper signals (rephrased
               right away, ignored the cards, "no"/"wena eka") count against.
```

Every promotion is evidence-based and reversible. An admin page lists the
patterns, their traffic, agreement and status, with approve / disable.

### What the log stores (and does not)

`assistant_turns`: when, branch, language, the normalised text, the
abstracted signature, the layer that answered, the plan (tool names and
arguments), reply kind, latency, model tokens/cost, and outcome signals (card
tapped, added to basket, rephrased within 30 s, conversation ended). Customer
identity is a salted hash (for "did the same person rephrase"), never the id;
names, phone numbers, addresses and order numbers are replaced by
placeholders before anything is written. Retention is bounded (e.g. 180 days
for raw text; patterns keep only signatures). Customers can still clear their
chat; the anonymised statistics are not personal data.

## 5. Measuring "sure"

* **Offline**: the eval suite (`evals/assistant/cases.jsonl` + the catalogue
  snapshot) runs every layer with no model and no cost. Each fast layer must
  keep **precision ≥ 99 %** on the cases it claims; recall is what grows.
* **Shadow agreement** (section 4) per pattern on live traffic.
* **Dashboards**: share of turns per layer, p50/p95 latency per layer, model
  cost per day, agreement and demotions. The target curve is the share of
  free turns climbing toward 95 % while precision holds.

## 6. Why not "a local LLM" or another language runtime

* Open models are weak at Sinhala and especially Singlish (see the research
  notes in the chatbot-architect skill); a CPU box answers in tens of seconds
  and a GPU box costs more per month than years of Gemini at today's volume.
* The fast path is lookups and string work over ≤ 4,000 products and a few
  thousand patterns: microseconds to milliseconds in Python, in-process. The
  slow part of any turn is the network to the database, not the CPU. A Go or
  Rust service would add a deploy unit and an RPC hop for no measurable gain.
  The router is written as a pure, dependency-free module so it *can* be
  ported if a profile ever says so.

## 7. Everything a shopper can do in the app

The fast path and the agent share one tool registry. Coverage grows by phase
(see ROADMAP.md): catalogue and product details, basket, orders and tracking,
returns and cancellation requests, reorder, wishlist, wallet and loyalty
points, coupons and offers, referrals, addresses and delivery location,
notifications and settings, app help.
