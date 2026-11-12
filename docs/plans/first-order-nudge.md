# PLAN: first-order nudge push

Owner decisions: 2026-11-12, two Sinhala MCQ rounds. Code: `app/services/first_order_nudge_worker.py`,
migration 114, templates `nudge.first_order.*` in `notification_i18n.py`.

## Task

Send one push to a customer who registered 2 days ago and has not placed an order yet. The push invites them to
place their first order. It is modelled on the pushes sent by hand on 2026-11-12.

## Rules (owner)

| # | Rule |
|---|------|
| R1 | **Who:** role `customer`, active, not blocked, not deleted, no order in any status. |
| R2 | **Everyone** gets it, with or without an address. |
| R3 | **New customers only:** registered after `first_order_nudge_state.since`, which the worker writes once, the first time it runs on a server (deploy time). Older customers never get it. Owner can reach them with Push Campaigns. |
| R4 | **When:** 48 h or more after registration. Sweeps run only between **18:30 and 21:00 Asia/Colombo**. |
| R5 | **Once only**, ever. A `first_order_nudges` row (PK `user_id`) is written before the push. |
| R6 | **Offers switch:** category `offers`. A customer with offers switched off gets nothing. |
| R7 | **Tap:** `data.screen = "offers"`. The app already opens the Offers page for this. |
| R8 | **Wording follows the live offer** (below). |

### Message (R8)

Push title is always "Sribees Online". Templates are in en/si/ta. Variants:

| Variant | When | Says |
|---|---|---|
| `offer_min` | Served address, branch offer live, minimum > 0 | name, area, "free delivery over Rs. {min}, until {end}" |
| `offer` | Served address, offer live, no minimum | name, area, "free delivery on every order, until {end}" |
| `area` | Served address, no live offer | name, area, no offer claim |
| `general` | No address, or the address is not served | name only; "add your address and see if we deliver" |

- `{area}` is the address's postal city in English (the DB has no Sinhala/Tamil post office names).
- `{end}` is the offer end date in Colombo time ("දෙසැම්බර් 2").
- No name on the account → "යාළුවා" / "there" / "நண்பரே".

## Guardrails

- No money, wallet or order code changes. New tables, a worker and templates only.
- `since` is written by the worker, not the migration: on a deploy the app boots (`create_all`) before the
  migrations run.
- Kill switch: `FIRST_ORDER_NUDGE_ENABLED=false` in `.env` stops the worker (default on; owner asked for it live).
- Logs name the user_id only. The sweep logs the exception type when it fails.

## Done = these checks pass

1. `tests/integration/test_first_order_nudge.py` green: copy per variant, minimum always stated, date, R1/R3/R6
   exclusions, 47 h waits, the 18:30-21:00 window, two concurrent workers send once, `since` written once,
   `screen == offers`.
2. `tests/test_notification_i18n.py` green (placeholders match in en/si/ta, no invisible characters).
3. `ruff check .` clean; the full suite shows no new failures against HEAD.
4. Migration 114 replays twice on a throwaway Postgres 18.
5. After the prod deploy: `first_order_nudge_state.since` is set, and a read-only SQL of who is due returns 0 rows
   on deploy day (R3).
