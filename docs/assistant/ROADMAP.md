# Hybrid assistant roadmap

Design: [HYBRID_ASSISTANT.md](HYBRID_ASSISTANT.md). Each phase ships behind a
switch, is measured offline first (no model calls, no cost), and goes to
production only when its acceptance numbers hold. Paid model runs for testing
happen only when the owner approves them.

| Phase | What ships | Done when |
|---|---|---|
| **P0 Foundations** | `Plan` / executor / composer extracted from the agent so every layer speaks one language; `assistant_turns` log with scrubbing; per-turn layer, latency and cost recorded | all existing assistant tests pass; a turn writes one scrubbed row; no personal data in rows (tests) |
| **P1 Rules (L2)** | the frequent safe shapes answered without the model: bare product names, "<X> one / thiyenawada / kiyada / dennako", order tracking and history, open a page / how-to, greetings and thanks, "<X> <n>k cart ekata danna" previews; replies in en/si/ta from templates | offline: precision ≥ 99 % on the cases L2 claims, the rest pass through; p95 < 50 ms incl. DB; shadow mode on prod first, then active |
| **P2 Memory + miner (L1, L3)** | exact-text memory; nightly miner turning model turns into candidate patterns; shadow agreement per pattern; automatic promotion and demotion | patterns promoted only past the thresholds; a forced disagreement demotes (test); miner runs with no model calls |
| **P3 Signals + canary + admin** | outcome signals (cards tapped, basket adds from chat, quick rephrase); canary re-checks of active patterns (configurable share); admin page to review patterns and the free-turn share | dashboards show share per layer, latency, cost/day; admin can disable a pattern instantly |
| **P4 Do anything in the app** | tools for product details, wishlist, wallet and loyalty points, coupons/offers, returns and cancellation requests, reorder, referrals, addresses and delivery location, notifications and settings — for the agent and the fast path alike | each tool has offline cases (positive, negative, cross-user refusal) |
| **P5 Scale** | log partitioning and retention jobs, pattern compiler, profiling; a port of the router only if a profile shows CPU as the bottleneck | p95 holds at 100× today's traffic in a load test |

## Targets over time

| | free turns | p50 latency (free / model) | model cost per 1,000 turns |
|---|---|---|---|
| today (agent only) | 0 % | – / ~9 s | ~Rs 2,000 |
| after P1 | 40–60 % | < 50 ms / ~9 s | ~Rs 1,000 |
| after P2–P3, a few months of traffic | 70–85 % | < 50 ms / ~9 s | ~Rs 400 |
| years of traffic | ~95 % | < 50 ms / ~9 s | ~Rs 100 |

The free share depends on real traffic and is reported, not assumed.

## Rules that do not bend

1. A fast layer answers only what it can answer with measured precision; the
   model handles the rest. Wrong-and-fast is worse than slow-and-right.
2. Facts come from live tools every time. Nothing a customer sees about stock,
   price or an order is replayed from a log.
3. Writes stay previews the customer confirms; orders are looked up by the
   authenticated user only.
4. The log is scrubbed before it is written; identity is a salted hash.
5. Every layer has an off switch; turning one off sends its traffic to the
   next layer down, never to an error.
