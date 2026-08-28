# Ecommerce Integration API — Integration Contract

**Status:** implemented and tested (`app/ecommerce_api/`, 27 passing integration
tests as of 2026-08-25). This document describes what actually exists, not a
spec to be built later.

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
  "cod_amount": 4820.00
}
```

| Field | Notes |
|---|---|
| `merchant_order_ref` | **Your** stable, unique order identifier (max 150 chars). Retry a failed/timed-out request with the *same* ref — you'll get your existing booking back, never a duplicate. A different ref always books a new shipment. |
| `quote_id` | From §6. `city_id`/`weight_kg`/`latitude`/`longitude` are **not** sent here — they're taken from the quote server-side, so the price (and pin) you were shown is what you're charged/booked, by construction. |
| `cod_amount` | What the rider collects on delivery. Omit or send `0` for prepaid. |

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
  "created_at": "2026-08-25T12:35:00+00:00",
  "tracking_url": "https://<web-app-origin>/track?waybill=SXP0000012345"
}
```

`latitude`/`longitude` are `null` if you didn't send them on `/rates`.

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
  "created_at": "2026-08-25T12:35:00+00:00",
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

`404` unknown shipment, `403` belongs to a different API key.

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
see one.

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
(e.g. straight to `delivered`) will not be resent. **Recommendation:**
treat webhooks as a low-latency notification, not your only source of
truth — poll `GET /ecommerce/shipments/{id}` periodically (or at least on
app startup / reconciliation jobs) as a fallback.

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

## 12. Checklist for your integration

- [ ] Get a sandbox key from SribeesExpress; test end-to-end before asking for a production key
- [ ] `POST /ecommerce/locations/resolve` (§5a) when an address is saved/changed — resolve post_office/district/province to a `city_id` once, cache it
- [ ] `POST /ecommerce/rates` at checkout — handle `serviceable: false` as a normal UI state, not an error
- [ ] `POST /ecommerce/shipments` with a **stable `merchant_order_ref`** (your own order id) so retries are safe
- [ ] Handle `quote_expired`/`quote_already_used`/`quote_not_serviceable` explicitly — don't just show a generic error
- [ ] Register a webhook endpoint and verify signatures (§10.3) — don't trust an unverified payload
- [ ] Still poll `GET /ecommerce/shipments/{id}` periodically as a fallback (§10.4)
- [ ] Handle `429` by backing off for `Retry-After` seconds
- [ ] Map the 8-value status vocabulary (§8) onto your own order-status display
- [ ] Handle `resolved: false` (§5a) by `reason` — `ambiguous_post_office` means resend with `district`, the others mean "can't deliver here yet"
