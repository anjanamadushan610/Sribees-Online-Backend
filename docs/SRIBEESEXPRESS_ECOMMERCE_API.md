# Ecommerce Integration API — Integration Contract

**Status:** implemented and tested (`app/ecommerce_api/`, 208 passing tests
across the whole backend as of 2026-08-28, including everything this domain
touches). This document describes what actually exists, not a spec to be
built later.

## Response to your open items (2026-08-28, round 2 — `SRIBEESEXPRESS_OPEN_ITEMS.md`)

Round 1 (`SRIBEESEXPRESS_INTEGRATION_GAPS.md`) is closed — every item in it
shipped, was answered, or was explicitly declined with a reason, as you
confirmed in §0 of your open-items doc. This section responds to round 2.

| # | Item | Verdict | Where |
|---|---|---|---|
| §1 COD remittance | **Built** — full ledger + a `cod-balance` read; payout policy below (not invented, given to you straight) | §14 |
| §2 `updated_at` missing | **Confirmed bug, fixed** | §7, §7a, §8 |
| §3 Failure reason | **Built `will_retry`** (the cheaper option, taken) — no `next_attempt_at`, see why below | §8 |
| §4 Same-day/express tier | **Roadmap: yes, ~3–6 months out** (targeting Q4 2026 / Q1 2027) — pricing not built yet | — |
| §5 §7a load-bearing | **Acknowledged** — two commitments below | §7a |
| §6 Cold chain | **No capability today, none planned** — cap your categories accordingly | — |
| §7 Sandbox & keys | **Sandbox key issued** — see the note below before you try to call it | §11 |
| §8 Field-level questions | **Answered inline**, item by item | §15 |

### §1 — COD remittance/settlement

You were right and we were wrong to put this back on you first — the cash
sits with us, so the payout terms are ours to state, not yours to author.
Built:

- `GET /ecommerce/remittances` — every remittance run for your account.
- `GET /ecommerce/remittances/{remittance_id}/shipments` — which shipments
  a given run covers (§7's shape, `remittance_id` filled in).
- `GET /ecommerce/cod-balance` — collected-but-not-yet-remitted right now,
  no policy needed to compute it (pure arithmetic over data we already hold).
- `remittance_id` on every shipment read (§7/§7a/§8) — `null` until settled.

**The policy, in prose, as asked:**

- **Cadence:** weekly. There is no scheduler in this backend yet (the same
  gap already flagged for webhook retry) — a run is triggered by our ops
  team per client, not on an automatic clock. If a week is ever missed,
  nothing is lost: every unremitted collection just carries forward into
  the next run.
- **Deductions:** delivery charge only, today. Nothing else comes off the
  top in this cut.
- **A shipment that had COD collected but ends up `returned`:** the cash
  simply stays uncredited to any remittance — it keeps showing up in
  `cod-balance` and in the eligible pool for whichever run picks it up
  next. There's no automatic reversal; if you need it resolved sooner than
  the next scheduled run, ask ops directly.

Generating a run and marking one paid (with a bank reference) are
staff-only actions on our side — never exposed on this public API — so you
won't see write endpoints for them here, only the read side above.

### §2 — `updated_at`

Confirmed as a real bug, not a decision. `updated_at` now appears on §7's
booking response, every row of §7a's list, and §8's read. Two follow-ups
you asked for:

- `updated_since` is **inclusive** (`>=`) — re-reading one row at the
  boundary is free, so we'd rather that than risk skipping one.
- `updated_at` bumps on **every** field write to the order, not just a
  status transition — a §8a `PATCH` correcting a phone number bumps it too,
  so your sweep will see it.

### §3 — Failure reason

We built the cheaper of your two options: **`will_retry: boolean | null`**,
present on every shipment read.

```json
{ "status": "failed", "will_retry": true, "failure_note": "Called 3 times, no answer" }
```

- `true` while the shipment is sitting in the internal `failed_to_deliver`
  state — our own state machine still allows either a redispatch or a move
  into the return branch from there, so a retry is genuinely still possible.
- `false` once ops has moved it into the return branch (on its way to
  `returned`).
- `null` for anything else (this field only means something once `status`
  is `failed`).

**Deliberately no `next_attempt_at`.** Nothing in this backend schedules a
redispatch on a clock — a human decides when to re-dispatch. Shipping a
timestamp would claim a capability we don't have; `will_retry` is the
honest version of the same signal.

Your two no-build questions:

- **How many attempts before `returned`?** No fixed number — ops decides
  case by case today. There's no automatic cutoff.
- **Can you request one-attempt-only for a perishable order?** Not today.
  That needs a new per-order flag our dispatch/rider side actually
  respects — a real ask, not built this round given everything else in it;
  tell us if it's worth prioritizing next.

### §4 — Same-day/express tier

Roadmap: **yes**, targeting roughly 3–6 months out from today (so
Q4 2026 into Q1 2027). Not committing to a firmer date yet. The
`service_level` request/pricing shape you sketched is the right one when we
get there — nothing to build on your side now beyond what you're already
sending (`requested_delivery_date`/`_window`, captured only).

### §5 — §7a as load-bearing infrastructure

Both commitments, taken:

1. We will tell you before §7a's shape or availability changes.
2. Confirmed — §7a shares your account's normal per-key rate limit (the
   same dependency every route in this domain runs through). It is not
   separately throttled.

The webhook-delivery replay endpoint you floated (`POST
/ecommerce/webhook-endpoints/{id}/deliveries/{delivery_id}/replay`) is
noted, not built this round — flagged as real future work, not forgotten.

### §6 — Cold chain

**No temperature-controlled or expedited handling exists today, and none is
planned right now.** Cap frozen/chilled/ice-cream categories out of what
ships through this API and say so on your storefront — better that than a
melted delivery and a refund. `handling` tags remain capture-only this
round (not yet printed on a rider job card) — a real, cheap follow-up, just
not this pass.

### §7 — Sandbox & keys

A **sandbox client and key were issued** using the real issuance code path
(`POST /identity/clients/{id}/api-keys`) against a fully-migrated database —
proving the mechanism end-to-end. **Important caveat, told straight rather
than left for you to discover:** this backend has not yet been deployed to
any host reachable from outside this dev machine. The key that was
generated during this verification only exists against a throwaway local
database that no longer exists — **it is not usable by you today.** A real,
callable sandbox key (and the base URL to call it at) will follow once
SribeesExpress actually deploys this backend somewhere you can reach; that
is a real, separate, outstanding step on our side, not yet done.

Answers to the rest of §7:

- **Rate-limit ceiling:** mechanism is per-key
  (`ApiClientKey.rate_limit_per_minute`, default 60/min), not per-account —
  your sandbox and production keys can carry different limits without
  sharing a budget. Tell us your expected volume and we'll set your
  production key's ceiling accordingly at issuance.
- **`/locations/resolve` shares the limit:** confirmed — every route in
  this domain runs through the same per-key check, no exceptions.
- **`429` always carries `Retry-After`:** confirmed — the same shared
  dependency computes and attaches it on every rate-limited response here,
  burst or sustained.
- **Sandbox isolation in remittances/cod-balance:** **not yet isolated** —
  told straight, this is the same shallow-sandbox-isolation limitation
  already flagged in this doc's design notes (sandbox rows are tagged, but
  nothing downstream filters on `environment` yet). A sandbox booking's
  collected COD would currently show up in `cod-balance`/a remittance run
  the same as a live one. Don't run sandbox-key COD tests expecting them to
  stay invisible to billing until this is tightened.
- **`merchant_order_ref` namespace — fixed.** This was a real gap: your
  sandbox and live keys no longer share the same idempotency scope for a
  given ref. A test booking with `FC-20260828-A7K2M9` on your sandbox key
  can never collide with (or be mistaken for a retry of) a live order using
  the same ref.
- **Webhook secret rotation invalidates the old one immediately:**
  confirmed — the stored secret is overwritten in place, no dual-secret
  grace window.

### §8 — Field-level questions

**Lengths and formats**

1. `recipient_name` max **150**, `recipient_phone` max **30**,
   `recipient_address` max **255** (unchanged, now written down explicitly).
2. **Phone format is not validated or normalized** — send whatever string
   fits in 30 chars. If you need E.164 consistency, normalize before
   sending; nothing here will do it for you.
3. `handling` is genuinely **free-form** — no recognized vocabulary,
   nothing is dropped. Whatever list you send is stored and echoed back
   as-is.
4. `requested_delivery_window` is an **opaque string** — not parsed, no
   assumed separator or timezone. `"09:00-12:00"` is a convention between
   us, not a contract.

**Idempotency and identifiers**

5. `merchant_order_ref` idempotency **never expires** — the uniqueness is a
   permanent DB constraint, not a time-windowed cache. Retry after any
   outage length, safely.
6. `quote_id` is **permanently single-use** — once booked (or attempted),
   it can never be reused; stop retrying it and re-quote instead, exactly
   as you're already doing.
7. **`pickup_location_id` uniqueness — fixed, and answered differently than
   either option you proposed.** A second `POST` with a `name` already
   registered for your account now returns `409
   pickup_location_name_already_registered` — it neither silently
   duplicates nor silently returns the existing row. Same precedent as this
   domain's webhook-endpoint-URL uniqueness. A restore-from-backup that
   re-runs your registration sync will surface a clear, actionable error
   instead of silently forking your branch registry.
8. **Deactivated pickup locations — fixed.** Booking against a deactivated
   `pickup_location_id` now `409`s (`pickup_location_deactivated`) instead
   of silently succeeding.

**§7a specifics**

9. **Maximum `limit` — now capped at 200** (was uncapped). Your `limit=100`
   still works unchanged.
10. **`status_filter` now accepts a comma-separated list** —
    `status_filter=in_transit,out_for_delivery` fetches both in one call.
11. **Terminal-shipment retention:** no separate retention window exists or
    is planned — `updated_since` is the intended guard, confirmed. A
    year-old `delivered` shipment stays in an unfiltered sweep forever;
    filter by `updated_since` to bound your pagination cost.
12. **`handover`/`cod` on §7a rows — confirmed yes, always present**, same
    as §8's single read. Both endpoints build the response the same way
    internally; there's no divergence to worry about.

**Behaviour**

13. **`cod.collected` can flip independently of `delivered` — confirmed.**
    A COD collection is recorded as its own action on our side, with no
    check against the shipment's current status. Your assumption matches
    reality.
14. **`cod` on a `returned` shipment that had already been collected:**
    `collected` **stays `true`**, the amount is unchanged — nothing
    reverts it automatically. See §1 above: that cash simply sits
    unremitted until ops resolves it, rather than a refund happening out
    of band automatically.

---

**Audience:** any 3rd-party e-commerce platform integrating with
SribeesExpress for delivery — quote a price, book a shipment, track it,
cancel it, and receive push updates. Written to be **vertical-agnostic**:
nothing here assumes groceries, fashion, or any specific catalog shape. If
you are the developer/AI agent for a merchant platform (e.g. SRIBEESonline),
build to this document.

> This is a **separate, newer** surface from the older `/client-portal/api/*`
> integration routes on this backend. If you were pointed at those earlier,
> switch to this one — it is the maintained public contract going forward.

---

## 1. Who calls whom

You are the **client**. SribeesExpress exposes the API; you call it.

| When | Endpoint | Purpose |
|---|---|---|
| Customer opens checkout | `POST /ecommerce/rates` | Show the delivery fee before they pay |
| Customer places the order | `POST /ecommerce/shipments` | Book the pickup, get a waybill |
| Customer opens an order | `GET /ecommerce/shipments/{id}` | Show tracking status |
| Order is cancelled | `POST /ecommerce/shipments/{id}/cancel` | Release the booking |
| (once, at setup) | `GET /ecommerce/cities` | Resolve your delivery destinations to a `city_id` |
| (once, at setup) | `POST /ecommerce/webhook-endpoints` | Register a URL for push status updates |

`POST /ecommerce/rates` is the one that matters first — build that before
anything else.

---

## 2. Base URL & versioning

```
<origin>/api/v1/ecommerce/...
```

`<origin>` is environment-specific (dev: `http://localhost:8000`; staging/
production URLs are issued when SribeesExpress ops onboards your account,
along with your API keys). `/api/v1` is the version marker for this entire
backend — a breaking change to this contract ships as `/api/v2`, with a
deprecation window on `/v1`; additive fields are not breaking.

---

## 3. Authentication

Static API key in a header, over HTTPS in any non-local environment:

```http
X-Api-Key: <key issued to you>
```

Keys are issued by SribeesExpress staff (`POST
/identity/clients/{client_id}/api-keys`, not self-service) as either:

| Environment | Key prefix | Behavior |
|---|---|---|
| `live` | `sk_live_...` | Real bookings |
| `sandbox` | `sk_test_...` | See §11 |

Ask SribeesExpress for both a sandbox and a production key. Do not put the
key in a query string.

**401** on any missing/invalid key. **429** with a `Retry-After` header if
you exceed your account's rate limit (ask SribeesExpress for your ceiling —
`ApiClientKey.rate_limit_per_minute`, 60/min by default).

---

## 4. Standard error response

Every error (this domain only — other parts of this backend may differ)
returns a consistent body:

```json
{
  "error": {
    "code": "quote_expired",
    "message": "This quote has expired — request a new one."
  }
}
```

`code` is a stable, machine-readable string — branch your error handling on
it, not on `message` (which is for logs/support, and may be reworded).
Proper HTTP status codes are always used alongside it: `400`, `401`, `403`,
`404`, `409`, `422`, `429`. **Never** a `200` with an error inside it — the
deliberate exceptions are `serviceable: false` in §6 and `resolved: false`
in §5a, both of which are answers, not failures.

---

## 5. Resolving a delivery destination to a `city_id`

Every quote/booking needs a `city_id` — but `"city"` here is **not** an
administrative city, it's an internal grouping SribeesExpress creates when
assigning post offices to a branch's coverage area. You almost certainly
don't have this id lying around. Use **§5a** if your addresses are in Sri
Lanka's standard postal form (post office/district/province — the national
convention nearly every Sri Lankan merchant already has); use **§5b** only
if you already know a specific `city_id` (e.g. from a staff-provided list,
or because you cached one from a prior §5a resolution).

### 5a. `POST /ecommerce/locations/resolve` — recommended (post office → city_id)

Same auth as every other endpoint here.

**Request:**
```json
{ "post_office": "Welipenna", "district": "Kalutara", "province": "Western" }
```
`post_office` is required; `district`/`province` are optional narrowing
filters — send `district` whenever you have it, it's the practical
disambiguator for the rare duplicate post-office-name case.

**Response — always `200`:**
```json
{ "resolved": true, "city_id": 42, "city_name": "Colombo 07" }
```
or
```json
{ "resolved": false, "reason": "post_office_not_found", "candidates": [] }
```

| `reason` | Meaning |
|---|---|
| `post_office_not_found` | No matching post office in SribeesExpress's directory. |
| `not_yet_configured` | The post office is known but hasn't been assigned to a delivery area yet — ask SribeesExpress ops to configure it. |
| `no_delivery_to_this_area` | Configured, but that area currently has no active pricing zone. |
| `ambiguous_post_office` | More than one post office matches — `candidates` lists each match's `district`/`province`; resend with `district` set to disambiguate. |

**Never `404`** — an unresolvable location is a normal answer (same
philosophy as `/rates`'s `serviceable: false`, §6), and `resolved: false`
deliberately never distinguishes "doesn't exist" from
"exists-but-unconfigured" via HTTP status, only via `reason` — both just
mean "can't route you today."

**Cache the result** per address the way you'd cache anything else you
resolved once — there's no need to call this on every checkout, only when
an address is first saved or changed.

### 5b. `GET /ecommerce/cities` — direct lookup, if you already have a name

- **Query:** `search` (optional, case-insensitive substring match on name)
- **Response `200`:**
```json
[ { "id": 42, "name": "Colombo 07", "district": "Colombo" } ]
```

Mostly useful for support/debugging, or a merchant who was handed a
specific `city_id` list by SribeesExpress directly. Most integrations
should use §5a instead.

### 5c. `GET /ecommerce/coverage` — bulk export of serviceable post offices

For nightly-sync use cases (gap §11.4): you resolve a fulfilment branch from
a post office using your own coverage table, we resolve a `city_id` using
ours — they can disagree at the edges. Sync this list to pre-empt that
rather than discovering it only when `/locations/resolve` rejects an
already-saved address.

- **Response `200`:**
```json
[ { "post_office": "Welipenna", "district": "Kalutara", "province": "Western", "city_id": 42 } ]
```
Only post offices that would resolve `serviceable: true` right now — a post
office known but not yet assigned to a delivery area, or assigned to an area
with no active pricing zone, is excluded. This can change day to day as
SribeesExpress ops configures new areas; a nightly sync is reasonable, more
than that is unlikely to be worth it.

---

## 6. `POST /ecommerce/rates` — quote a delivery charge

Side-effect-free (no order is created), so call it freely on every checkout
page load.

### Request

```json
{
  "city_id": 42,
  "origin_branch_id": null,
  "weight_kg": 2.5,
  "cod_amount": 0.00,
  "payment_method": "prepaid",
  "latitude": 6.5854,
  "longitude": 79.9607
}
```

| Field | Required | Notes |
|---|---|---|
| `city_id` | yes | From §5a (recommended) or §5b. |
| `origin_branch_id` | no | Accepted and passed through to the eventual booking; does not currently affect the price (pricing is zone/weight-only). |
| `weight_kg` | yes | `0 < weight_kg <= 1000`. Total order weight — resolve this from your own product data. |
| `cod_amount` | no | Accepted for forward compatibility; **does not currently affect `amount_lkr`** — there is no COD collection fee in the pricing formula today. Don't rely on it changing the quote. |
| `payment_method` | no | `"cod"` or `"prepaid"` (default). Accepted, currently inert for the same reason as `cod_amount`. |
| `latitude` / `longitude` | no | An optional GPS pin for the delivery address (e.g. a Google Maps marker your app already captures). Carried through to the booked shipment (§7/§8) for future rider-navigation use — **not currently used for pricing or zone resolution**, that's separate future geocoding work. |

### Response — `200`, serviceable

```json
{
  "serviceable": true,
  "reason": null,
  "quote_id": "qt_9f2K7xQm3vLpR8sT1uWz2A",
  "amount_lkr": 175.00,
  "currency": "LKR",
  "expires_at": "2026-08-25T13:00:00+00:00",
  "breakdown": { "first_kg": "100.00", "after_kg_rate": "50.00", "weight_kg": "2.500" }
}
```

`amount_lkr` is the **total** — nothing is added on top of it at booking
time as long as you book before `expires_at` (30 minutes from issuance).
`breakdown` is informational, not guaranteed stable across versions.

### Response — `200`, not serviceable

```json
{ "serviceable": false, "reason": "No delivery to this city.", "quote_id": null, "amount_lkr": null, "currency": "LKR", "expires_at": null, "breakdown": null }
```

A place SribeesExpress doesn't deliver to is a **normal answer**, not an
error — show the customer accordingly rather than a generic failure.

---

## 7. `POST /ecommerce/shipments` — book the pickup

Called once, after payment is accepted. **Idempotent** on
`merchant_order_ref` — safe to retry on a network timeout.

### Request

```json
{
  "merchant_order_ref": "MYSHOP-20260825-A7K2M9",
  "quote_id": "qt_9f2K7xQm3vLpR8sT1uWz2A",
  "recipient_name": "A. Perera",
  "recipient_phone": "+94771234567",
  "recipient_address": "12 Galle Road, Colombo 03",
  "cod_amount": 4820.00,
  "handover_code": "4821",
  "handover_code_required": true,
  "pickup_location_id": 7,
  "requested_delivery_date": "2026-08-29",
  "requested_delivery_window": "09:00-12:00",
  "handling": ["perishable"]
}
```

| Field | Notes |
|---|---|
| `merchant_order_ref` | **Your** stable, unique order identifier (max 150 chars). Retry a failed/timed-out request with the *same* ref — you'll get your existing booking back, never a duplicate. A different ref always books a new shipment. |
| `quote_id` | From §6. `city_id`/`weight_kg`/`latitude`/`longitude` are **not** sent here — they're taken from the quote server-side, so the price (and pin) you were shown is what you're charged/booked, by construction. |
| `cod_amount` | What the rider collects on delivery. Omit or send `0` for prepaid. |
| `address_line1`/`address_line2`/`post_office`/`district`/`province`/`postal_code` | Optional structured alternative to `recipient_address` — send these instead of pre-concatenating and we'll format them for you (comma-joined). `recipient_address` itself has a **255-char max**; if both are sent, the structured fields win. |
| `handover_code` | **Prepaid orders only — never send this for a `cod` order.** 4-6 digits, generated by you once payment is confirmed. Required together with `handover_code_required: true`; sending one without the other is a `400`. |
| `handover_code_required` | Default `false`. When `true`, the rider **cannot** mark the shipment `delivered` without a matching code — see §7a for the verified/attempts fields this unlocks on tracking reads. |
| `pickup_location_id` | From §12 — omit to use whatever default origin SribeesExpress has on file for your account. The pickup address is **snapshotted onto the shipment at booking time**; editing the pickup location afterward never changes an already-booked shipment. |
| `requested_delivery_date` / `requested_delivery_window` | Captured and returned on reads — **not scheduled or enforced**, no rider job-card visibility yet. Best-effort informational only; don't promise the customer a guaranteed window based on SribeesExpress acknowledging this field. |
| `handling` | Free-form tags (`"perishable"`, `"fragile"`, etc.) — captured and returned on reads only. No surcharge, no cold-chain service, no rider-app visibility yet. If you need an actual temperature-controlled service, ask — none exists today, so cap what you ship through this API accordingly in the meantime. |

### Response — `201` (new booking) or `200` (idempotent replay)

```json
{
  "shipment_id": "SXP0000012345",
  "merchant_order_ref": "MYSHOP-20260825-A7K2M9",
  "status": "booked",
  "amount_lkr": 175.00,
  "cod_amount": 4820.00,
  "recipient_name": "A. Perera",
  "recipient_phone": "+94771234567",
  "recipient_address": "12 Galle Road, Colombo 03",
  "latitude": 6.5854,
  "longitude": 79.9607,
  "pickup_location_name": "Colombo 07 Grocery Branch",
  "pickup_contact_phone": "0112223344",
  "pickup_address": "12 Ward Place, Colombo 07",
  "handover": { "required": true, "code_verified": false, "attempts": 0 },
  "cod": { "collected": false, "amount": 4820.00 },
  "failure_note": null,
  "will_retry": null,
  "remittance_id": null,
  "requested_delivery_date": "2026-08-29",
  "requested_delivery_window": "09:00-12:00",
  "handling": ["perishable"],
  "created_at": "2026-08-25T12:35:00+00:00",
  "updated_at": "2026-08-25T12:35:00+00:00",
  "tracking_url": "https://<web-app-origin>/track?waybill=SXP0000012345"
}
```

`latitude`/`longitude`/`pickup_*`/`requested_delivery_*`/`handling` are
`null` (or `[]`) if you didn't send them. `handover.required: false` with
`code_verified`/`attempts` both `null` for every COD/no-code shipment — see
§7a for the field meanings. `cod.amount` is what you sent as `cod_amount`
(the *asked* amount); `cod.collected` flips to `true` once a rider actually
records collecting it — see §8's `delivered`-event note. `will_retry` is
`null` until `status` is `failed` (see §8). `remittance_id` is `null` until
this shipment's COD has been paid out to you (see §14) — always `null` at
booking time.

A `200` (not `201`) means this was a replay of an existing booking — you'll
get the exact same `shipment_id` back, not a new one.

### Errors specific to this endpoint

| `code` | Status | Meaning |
|---|---|---|
| `quote_not_found` | 404 | Unknown `quote_id`. |
| `quote_not_owned` | 403 | That quote belongs to a different API key. |
| `quote_already_used` | 409 | Already booked once — book with a fresh `/rates` call instead. |
| `quote_expired` | 409 | Past `expires_at` — re-quote (price may have changed; re-confirm with the customer before charging). |
| `quote_not_serviceable` | 422 | You tried to book a quote that came back `serviceable: false`. |
| `handover_code_required_without_code` | 400 | Sent `handover_code_required: true` without a `handover_code`. |
| `pickup_location_not_found` | 404 | Unknown `pickup_location_id`. |
| `pickup_location_not_owned` | 403 | That pickup location belongs to a different API key. |
| `pickup_location_deactivated` | 409 | That pickup location was deactivated — register a new one or omit `pickup_location_id`. |

### 7a. `GET /ecommerce/shipments` — reconciliation-poll list (gap §6.2)

The practical fallback for §10.4's "no automatic webhook retry" — poll this
instead of hammering `GET /shipments/{id}` per parcel.

- **Query:** `updated_since` (ISO-8601 datetime, optional, **inclusive**),
  `status_filter` (one or more of the §8 external values,
  **comma-separated**, optional), `limit` (default 50, **max 200**),
  `offset`.
- **Response `200`:** a list of the **same per-shipment shape as §7's
  response** (`ShipmentOut`, `handover`/`cod`/`remittance_id` included) —
  deliberately **without** the `history` array §8 includes; a history join
  on every row isn't worth paying for at merchant scale. Call §8 for a
  specific shipment's full history.

```http
GET /ecommerce/shipments?updated_since=2026-08-28T09:00:00Z&status_filter=out_for_delivery,failed&limit=100
```

`status_filter=in_transit` matches every internal status that collapses
onto that external value (§8's note on granularity) — you don't need to
know or care about the internal states, filtering is always in terms of
the 8-value vocabulary. No server-side retention window on terminal
shipments — `updated_since` is the guard against unbounded pagination cost.

---

## 8. `GET /ecommerce/shipments/{shipment_id}` — tracking

`shipment_id` is the `shipment_id`/`waybill_id` from §7 (e.g.
`SXP0000012345`).

### Response — `200`

```json
{
  "shipment_id": "SXP0000012345",
  "merchant_order_ref": "MYSHOP-20260825-A7K2M9",
  "status": "picked_up",
  "amount_lkr": 175.00,
  "cod_amount": 4820.00,
  "recipient_name": "A. Perera",
  "recipient_phone": "+94771234567",
  "recipient_address": "12 Galle Road, Colombo 03",
  "latitude": 6.5854,
  "longitude": 79.9607,
  "pickup_location_name": "Colombo 07 Grocery Branch",
  "pickup_contact_phone": "0112223344",
  "pickup_address": "12 Ward Place, Colombo 07",
  "handover": { "required": true, "code_verified": true, "attempts": 1 },
  "cod": { "collected": false, "amount": 4820.00 },
  "failure_note": null,
  "will_retry": null,
  "remittance_id": null,
  "requested_delivery_date": "2026-08-29",
  "requested_delivery_window": "09:00-12:00",
  "handling": ["perishable"],
  "created_at": "2026-08-25T12:35:00+00:00",
  "updated_at": "2026-08-25T15:02:00+00:00",
  "tracking_url": "https://.../track?waybill=SXP0000012345",
  "history": [
    { "status": "booked", "at": "2026-08-25T12:35:00+00:00" },
    { "status": "picked_up", "at": "2026-08-25T15:02:00+00:00" }
  ]
}
```

Fixed status vocabulary (map this onto your own order statuses):

`booked` · `picked_up` · `in_transit` · `out_for_delivery` · `delivered` ·
`failed` · `returned` · `cancelled`

Internally SribeesExpress tracks more granular states (sorting center,
dispatch, etc.) — several of those collapse onto the same external value
here (e.g. everything between pickup and out-for-delivery reads as
`in_transit`), so consecutive `history` entries can repeat a status. This is
intentional simplification, not a bug.

**`handover`** (gap §1, prepaid orders only): `required` mirrors what you
sent at booking; `code_verified`/`attempts` are `null` unless `required` is
`true`. The rider cannot move a `handover.required: true` shipment to
`delivered` until `code_verified` is `true` — a status transition attempt
without it is rejected on our side, so `delivered` genuinely means the code
matched (or the order never required one).

**`cod`**: `amount` is what you sent as `cod_amount`; `collected` flips to
`true` once a rider records actually collecting it — usually, but not
strictly always, at the same moment as the `delivered` status (a rider can
mark delivered and record collection as two close-together actions on their
side). Check both fields together if you need to know "cash in hand,
confirmed."

**`failure_note`** (gap §5): the rider's free-text reason, present only when
`status` is `failed` or `returned`, `null` otherwise. **This is free text,
not a stable enum** — the rider app itself only ever collects free text
today (no Reason-ID dropdown), so a controlled vocabulary here would
misrepresent what's actually collected. Parse for keywords if you need
rough bucketing, but don't build hard business logic against exact strings.

**`will_retry`** (open item §3): `true`/`false` only when `status` is
`failed`, `null` otherwise. `true` means our state machine still allows a
redispatch from here; `false` means ops has moved it into the return
branch. No `next_attempt_at` — nothing here schedules a retry on a clock.

**`remittance_id`** (§14): `null` until this shipment's collected COD has
been included in a paid-out remittance run. Use `GET
/ecommerce/remittances/{remittance_id}/shipments` once it's set to see the
full run it belongs to.

`404` unknown shipment, `403` belongs to a different API key.

---

## 8a. `PATCH /ecommerce/shipments/{shipment_id}` — correct a booking (gap §8)

Fixes a typo'd phone number or incomplete address before the parcel is
actually out for delivery — `cancel` (§9) is too blunt an instrument for
this, and is illegal past pickup anyway.

### Request — all fields optional, only supplied ones change

```json
{
  "recipient_phone": "+94771234567",
  "recipient_address": "12 Galle Road, Colombo 03",
  "latitude": 6.5854,
  "longitude": 79.9607,
  "requested_delivery_date": "2026-08-30",
  "requested_delivery_window": "13:00-17:00"
}
```

Accepts the same structured `address_line1`/`.../postal_code` fields as §7
instead of a flat `recipient_address`, with the same fold-into-one-string
behavior.

### Response — `200`, the updated shipment (§7's response shape)

### Errors

| `code` | Status | Meaning |
|---|---|---|
| `shipment_not_correctable` | 409 | The shipment is `out_for_delivery` or beyond — too late to correct via the API. |
| `shipment_not_found` / `shipment_not_owned` | 404 / 403 | Same as every other shipment endpoint. |

---

## 9. `POST /ecommerce/shipments/{shipment_id}/cancel`

No request body. Returns the shipment (§7's shape) with `status:
"cancelled"` on success.

Only legal while the shipment hasn't been picked up yet. Once a rider has
collected the parcel, this returns `409` with a generic `conflict`/`error`
code — cancellation past that point is a support-desk conversation, not an
API call.

---

## 10. Webhook push (recommended over polling)

### 10.1 Register an endpoint

```http
POST /ecommerce/webhook-endpoints
```
```json
{ "url": "https://your-store.example.com/webhooks/sribees", "description": "prod" }
```

- Must be `https://`. The host is resolved and rejected if it points at a
  private/loopback/internal address.
- **Response `201`** includes `secret` — shown **exactly once**. Store it;
  it's needed to verify pushes (§10.3). Lost it? `POST
  /ecommerce/webhook-endpoints/{id}/rotate-secret` issues a new one (old one
  stops working immediately).
- `GET /ecommerce/webhook-endpoints[/{id}]` to list/inspect (never returns
  the secret again). `PATCH .../{id}` to change the URL/description/active
  flag. `DELETE .../{id}` deactivates it (204).
- `GET /ecommerce/webhook-endpoints/{id}/deliveries?limit=&offset=` — your
  own delivery attempt log (useful for debugging a missed push).

### 10.2 What gets pushed

One event type today: `shipment.status_changed`, fired whenever the
*external* status (§8's vocabulary) changes — not on every internal
transition.

```http
POST https://your-store.example.com/webhooks/sribees
Content-Type: application/json
X-Sribees-Event: shipment.status_changed
X-Sribees-Signature: t=1756123200,v1=5257a869e7ecebeda32affa62cdca3fa51cad7e77a0e56ff536d0ce8e108d8bd
```
```json
{
  "event": "shipment.status_changed",
  "event_id": "b3f1c2a4-...-uuid4",
  "shipment_id": "SXP0000012345",
  "status": "picked_up",
  "at": "2026-08-25T15:02:00+00:00"
}
```

`event_id` is a stable dedupe key — store it and ignore repeats if you ever
see one. Two extra blocks appear conditionally:

- On the transition **to `delivered`**: `handover` and `cod` (same shapes as
  §8's read response) — this is how you learn a COD collection actually
  happened, or that a prepaid handover code was verified, without a
  separate poll.
- On the transition **to `failed` or `returned`**: `failure_note` (§8's free
  text, `null` otherwise never sent as a key on other transitions).

```json
{
  "event": "shipment.status_changed",
  "event_id": "c8a2e5f1-...-uuid4",
  "shipment_id": "SXP0000012345",
  "status": "delivered",
  "at": "2026-08-28T15:02:00+00:00",
  "handover": { "required": true, "code_verified": true, "attempts": 1 },
  "cod": { "collected": true, "amount": 4820.00 }
}
```

### 10.3 Verifying the signature

`X-Sribees-Signature` is `t=<unix_ts>,v1=<hex hmac-sha256>` computed over
`"{t}.{raw request body}"` using your endpoint's secret. Recompute and
compare (constant-time) before trusting the payload — e.g. in Python:

```python
import hmac, hashlib

t, v1 = (p.split("=", 1)[1] for p in signature_header.split(","))
expected = hmac.new(secret.encode(), f"{t}.".encode() + raw_body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, v1)
```

### 10.4 Reliability — read this before relying on it exclusively

Each push attempt has a 5-second timeout and is logged (success or failure)
to your delivery log (§10.1). **There is currently no automatic retry on
failure.** If your endpoint is briefly down, that one push is lost — the
next status change will push again, but a status that changes only once
(e.g. straight to `delivered`) will not be resent. **This isn't scheduled
to change soon** — automatic retry needs a background job queue this
backend doesn't have wired up anywhere yet (not specific to webhooks; a
repo-wide gap). **Recommendation:** treat webhooks as a low-latency
notification, not your only source of truth — poll **§7a's `GET
/ecommerce/shipments` list** (not one-at-a-time `GET /shipments/{id}` calls,
which won't scale within your rate limit) periodically, or at least on app
startup / reconciliation jobs, as the real source of truth.

---

## 11. Sandbox

Use your `sandbox` key (`sk_test_...`) against the same endpoints — no
separate host. Real rows are created (bookings are real, just tagged
internally), so don't send real customer data through it.

One deterministic test trigger: `POST /ecommerce/rates` with `weight_kg >=
900` on a sandbox key always returns `serviceable: false, reason:
"sandbox: simulated unserviceable area"` — use this to test your
unserviceable-area handling without needing a real unserved city.

---

## 12. Pickup locations (gap §2 — multi-branch merchants)

Every shipment needs a pickup point. If you fulfil from a **single**
address, ignore this section — omit `pickup_location_id` on `/shipments`
and SribeesExpress uses the default on file for your account. If you fulfil
from **multiple** branches (resolved per-order, e.g. from the customer's
post office at checkout), register each one here once and reference it by
id at booking time.

### Register — `POST /ecommerce/pickup-locations`
```json
{
  "name": "Colombo 07 Grocery Branch",
  "phone": "0112223344",
  "address": "12 Ward Place, Colombo 07",
  "post_office": "Colombo",
  "district": "Colombo",
  "province": "Western",
  "latitude": 6.9061,
  "longitude": 79.8678
}
```
→ **`201`**, the created location including its `id` — use that as
`pickup_location_id` on §7. A `name` you've already registered for your
account returns `409 pickup_location_name_already_registered` instead of a
silent duplicate or a silent return-the-existing-row — the same applies to
`PATCH` if it would rename one location onto another's name. If you lost
your stored `id` and are re-running a registration sync, treat this `409`
as "already exists" and fetch the list (below) to recover the id.

### Manage
- `GET /ecommerce/pickup-locations[/{id}]` — list/inspect.
- `PATCH /ecommerce/pickup-locations/{id}` — update any field.
- `DELETE /ecommerce/pickup-locations/{id}` — deactivates (`204`), doesn't
  hard-delete (a soft `is_active: false`, same convention as webhook
  endpoints in §10.1).

**Editing or deactivating a pickup location never changes an
already-booked shipment** — its `pickup_location_name`/`_contact_phone`/
`_address` (§7's response) are a snapshot taken at booking time, not a live
reference. If a branch's address changes, update it here for *future*
bookings; nothing retroactively changes on parcels already in transit.

---

## 14. COD remittances (open item §1) — read-only

Generating a remittance run and marking one paid are staff-only actions on
our side (real money, a human confirming a bank transfer landed) — this
API only ever lets you **read your own remittances back**. See the policy
prose in the response section above (weekly, delivery-charge-only
deduction, no automatic scheduler) before relying on cadence assumptions.

### `GET /ecommerce/cod-balance` — right now, no remittance needed

```json
{
  "currency": "LKR",
  "collected_not_remitted": 184300.00,
  "shipment_count": 41,
  "oldest_collection_at": "2026-08-21T11:04:00+00:00"
}
```

Pure arithmetic over COD already collected and not yet tied to a
remittance run — safe to poll any time, no payout policy dependency.

### `GET /ecommerce/remittances?limit=&offset=`

```json
[
  {
    "id": 12,
    "gross_cod_amount": 184300.00,
    "deductions_amount": 6125.00,
    "net_amount": 178175.00,
    "shipment_count": 41,
    "status": "pending",
    "bank_reference": null,
    "created_at": "2026-08-28T09:00:00+00:00",
    "paid_at": null
  }
]
```

`status` is `pending` until ops confirms the bank transfer landed, then
`paid` with `bank_reference`/`paid_at` filled in.

### `GET /ecommerce/remittances/{remittance_id}/shipments`

Returns the shipments (§7's shape) that this specific run covers —
`remittance_id` on each one matches. `403` if the remittance belongs to a
different account, `404` if unknown.

---

## 15. Checklist for your integration

- [ ] Get a sandbox key from SribeesExpress; test end-to-end before asking for a production key
- [ ] `POST /ecommerce/locations/resolve` (§5a) when an address is saved/changed — resolve post_office/district/province to a `city_id` once, cache it
- [ ] `POST /ecommerce/rates` at checkout — handle `serviceable: false` as a normal UI state, not an error
- [ ] `POST /ecommerce/shipments` with a **stable `merchant_order_ref`** (your own order id) so retries are safe
- [ ] Handle `quote_expired`/`quote_already_used`/`quote_not_serviceable`/`handover_code_required_without_code`/`pickup_location_not_found`/`pickup_location_not_owned` explicitly — don't just show a generic error
- [ ] Register a webhook endpoint and verify signatures (§10.3) — don't trust an unverified payload
- [ ] Poll **§7a's `GET /ecommerce/shipments` list** periodically as the real fallback/source of truth (§10.4) — not one-at-a-time reads
- [ ] Handle `429` by backing off for `Retry-After` seconds
- [ ] Map the 8-value status vocabulary (§8) onto your own order-status display
- [ ] Handle `resolved: false` (§5a) by `reason` — `ambiguous_post_office` means resend with `district`, the others mean "can't deliver here yet"
- [ ] If you fulfil from more than one branch, register each as a pickup location (§12) and send `pickup_location_id` per order
- [ ] Send `handover_code`/`handover_code_required` **only for prepaid orders**, never COD (§7)
- [ ] Read `handover`/`cod`/`failure_note` on the `delivered`/`failed`/`returned` webhook events and §8 reads (§8, §10.2) — don't assume `delivered` alone means "cash confirmed" for COD
- [ ] Advance your §7a `updated_since` cursor from your own clock at the *start* of each sweep, not the end — see §5's commitment above for why that matters with no webhook retry
- [ ] Poll `GET /ecommerce/cod-balance` (§14) to reconcile "cash collected but not yet remitted" independently of the weekly remittance cadence
- [ ] Handle `pickup_location_name_already_registered` (§8 field answers) on a re-run registration sync — it means the location already exists, not that creation failed
