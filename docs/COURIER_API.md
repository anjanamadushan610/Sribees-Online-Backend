# Courier API — Integration Contract

**Status:** specification. Not implemented in this backend yet.
**Audience:** whoever builds the courier service. Build to this document and the
integration on our side will fit without changes.

---

## 1. Why this exists

Delivery is charged **by weight**, and that calculation is not ours to make —
rates, zones, fuel surcharges and service levels belong to the courier. Today
SRIBEESonline charges a flat fee from platform settings
(`flat_delivery_fee`, currently LKR 350), applied to any non-empty cart.

This contract replaces that: SRIBEESonline sends **where it is going** and **how
heavy it is**, the courier returns **what it costs**.

The seam already exists on our side — `PricingService._delivery_fee()` is a
single isolated method, and the courier call drops in there without touching the
rest of the pricing pipeline.

---

## 2. Who calls whom

SRIBEESonline is the **client**. The courier exposes the API. We call it:

| When | Endpoint | Purpose |
|---|---|---|
| Customer opens checkout | `POST /v1/rates` | Show the delivery fee before they pay |
| Customer places the order | `POST /v1/shipments` | Book the pickup, get a waybill |
| Customer opens an order | `GET /v1/shipments/{id}` | Show tracking status |
| Order is cancelled | `POST /v1/shipments/{id}/cancel` | Release the booking |

`POST /v1/rates` is the one that matters first — the other three can come later.
**If you build only one endpoint, build `/v1/rates`.**

---

## 3. Authentication

Static API key in a header, over HTTPS only:

```http
X-Api-Key: <key issued to SRIBEESonline>
```

Issue us separate **sandbox** and **production** keys. Do not put the key in a
query string — it ends up in access logs.

If you prefer OAuth2 client-credentials instead, say so before you build; it
changes our side and we would rather know now than later.

---

## 4. `POST /v1/rates` — quote a delivery charge

The important one. Called on every checkout page load, so it must be fast
(see §8) and must not have side effects.

### Request

```json
{
  "origin": {
    "name": "Colombo Main",
    "address_line1": "123 Galle Road, Colombo 03",
    "post_office": "Colombo",
    "district": "Colombo",
    "province": "Western",
    "phone": "+94112345678"
  },
  "destination": {
    "recipient_name": "A. Perera",
    "phone": "+94771234567",
    "address_line1": "12 Galle Road",
    "address_line2": "Colombo 03",
    "post_office": "Colombo",
    "district": "Colombo",
    "province": "Western",
    "postal_code": "00300"
  },
  "parcel": {
    "total_weight_kg": 7.25,
    "item_count": 4
  },
  "order_value_lkr": 4820.00,
  "payment_method": "cod"
}
```

**Field notes**

| Field | Notes |
|---|---|
| `origin` | The branch fulfilling the order. Every order is served by exactly one branch, so there is always exactly one pickup point. |
| `destination` | The customer's saved address. Sri Lankan addresses here are **post-office based**, not street-geocoded — `post_office` + `district` + `province` is the authoritative location triplet. Treat `postal_code` as a hint, not a key. |
| `parcel.total_weight_kg` | **Always kilograms.** See §7 on how we compute it. |
| `parcel.item_count` | Total units, not distinct lines. Send in case your rates have a handling component. |
| `order_value_lkr` | Order subtotal after discount. Needed if you charge a COD collection fee or insurance as a percentage. |
| `payment_method` | `"cod"` or `"prepaid"`. COD usually carries a collection fee; we need it priced into the quote, not surprised onto the invoice. |

We do **not** send parcel dimensions. If your rates are volumetric, tell us —
we currently have no dimension data on products and would have to add it.

### Response — 200

```json
{
  "quote_id": "qt_01HX9K2M4Q",
  "amount_lkr": 480.00,
  "currency": "LKR",
  "service_level": "standard",
  "estimated_delivery_days": 2,
  "expires_at": "2026-08-08T12:30:00Z",
  "breakdown": {
    "base": 350.00,
    "weight_surcharge": 100.00,
    "cod_fee": 30.00
  }
}
```

| Field | Required | Notes |
|---|---|---|
| `quote_id` | yes | Echoed back when we book, so the price we showed is the price you honour. |
| `amount_lkr` | yes | The **total** we charge the customer. Must already include every surcharge — we add nothing on top. |
| `expires_at` | yes | How long you honour `amount_lkr`. See §6. |
| `estimated_delivery_days` | no | Displayed to the customer if present. |
| `breakdown` | no | Not shown to customers; we log it so we can answer "why did this cost more". |

`amount_lkr` must be a number, not a string, and must not be negative.

### Response — destination not served (200, not an error)

```json
{
  "serviceable": false,
  "reason": "No delivery to this post office."
}
```

A place you do not deliver to is a **normal answer, not a failure**. Return 200
with `serviceable: false` so we can tell the customer plainly instead of showing
them a generic error. Do not use 404 for this.

---

## 5. `POST /v1/shipments` — book the pickup

Called once, after payment is accepted.

### Request

```json
{
  "quote_id": "qt_01HX9K2M4Q",
  "order_number": "FC-20260808-A7K2M9",
  "origin": { "...": "same shape as /rates" },
  "destination": { "...": "same shape as /rates" },
  "parcel": { "total_weight_kg": 7.25, "item_count": 4 },
  "payment_method": "cod",
  "cod_amount_lkr": 4820.00
}
```

`cod_amount_lkr` is what your rider collects. Send `0` for prepaid orders.

### Response — 201

```json
{
  "shipment_id": "shp_01HX9K3P7B",
  "waybill_number": "SL123456789",
  "amount_lkr": 480.00,
  "status": "booked",
  "tracking_url": "https://courier.example.lk/track/SL123456789",
  "estimated_pickup_at": "2026-08-08T14:00:00Z"
}
```

### Idempotency

We send `order_number`, and it is unique and stable on our side.

**If we retry a booking with an `order_number` you already have, return the
existing shipment with 200 — do not create a second one.** Our retry on a
network timeout must never turn into two riders and two invoices.

---

## 6. Quote expiry and price honouring

`amount_lkr` from `/v1/rates` is what the customer sees and agrees to pay.

- While `expires_at` is in the future, booking with that `quote_id` **must**
  charge exactly that amount.
- If the quote has expired, reject the booking with `409` and error code
  `quote_expired`. We will re-quote and, if it changed, show the customer the
  new price before charging them.
- **Never** silently book at a different price than the quote. A price that
  changes after the customer has paid is a support ticket and a refund.

Make `expires_at` at least **15 minutes** out. Checkout is not instant.

---

## 7. How we compute `total_weight_kg`

Read this — it determines whether your rates are correct.

Weight comes from our product catalog: each product has a `weight` and a
`weight_unit`. The order total is:

```
total_weight_kg = Σ (product.weight_in_kg × line.quantity)
```

We convert to kilograms before sending, so **you will always receive kg**,
regardless of how the product was entered.

### Known gaps you should design around

These are real limitations in our data today, not hypotheticals:

1. **Weight is nullable.** A product with no weight contributes nothing to the
   total. A cart of unweighed items would arrive as `total_weight_kg: 0`.
   **Do not treat 0 as free.** Price it at your minimum billable weight and
   return that. We are separately making weight mandatory for new products, but
   older rows exist.

2. **Product variants have no weight of their own.** A 500 g and a 2 kg variant
   of the same product currently report the same weight. If your pricing is
   sensitive at that granularity, tell us and we will add per-variant weight.

3. **No dimensions anywhere.** Volumetric pricing is not possible against our
   current data (see §4).

State a **minimum billable weight** in your rate card so behaviour at 0 kg and
at very light orders is defined rather than accidental.

---

## 8. Reliability requirements

`/v1/rates` sits directly in the checkout path. If it is slow, checkout is slow;
if it is down, we cannot quote.

| Requirement | Value |
|---|---|
| `/v1/rates` p95 latency | ≤ 800 ms |
| Our request timeout | 3 s, then we fall back (see below) |
| Our retry policy | 1 retry on timeout or 5xx, with backoff. **No retry on 4xx.** |
| Rate limit | Tell us the ceiling. Checkout page loads are the driver, not orders. |

**Our fallback when you are unreachable:** we charge the flat
`flat_delivery_fee` from platform settings and let checkout proceed. We will not
block a sale on a courier outage — but that means an outage costs us money on
heavy parcels, so treat `/v1/rates` availability as the priority.

---

## 9. Errors

Use HTTP status codes properly and return a consistent body:

```json
{
  "error": {
    "code": "quote_expired",
    "message": "This quote is no longer valid."
  }
}
```

| Status | When |
|---|---|
| 400 | Malformed request — a field is missing or the wrong type |
| 401 | Missing or invalid API key |
| 409 | `quote_expired`, or a booking conflict |
| 422 | Well-formed but unprocessable (e.g. weight above your maximum) |
| 429 | Rate limited — include `Retry-After` |
| 5xx | Your fault. We retry once, then fall back. |

`code` must be a stable machine-readable string. `message` is for our logs and
support staff, not for showing to customers verbatim.

**Do not return 200 with an error inside it.** The one deliberate exception is
`serviceable: false` in §4, which is an answer rather than a failure.

---

## 10. Tracking (later)

`GET /v1/shipments/{shipment_id}` returns current status:

```json
{
  "shipment_id": "shp_01HX9K3P7B",
  "waybill_number": "SL123456789",
  "status": "in_transit",
  "updated_at": "2026-08-09T06:12:00Z",
  "history": [
    { "status": "booked", "at": "2026-08-08T12:35:00Z" },
    { "status": "picked_up", "at": "2026-08-08T15:02:00Z" },
    { "status": "in_transit", "at": "2026-08-09T06:12:00Z" }
  ]
}
```

Use this fixed status vocabulary so we can map it to our own order statuses
without guessing:

`booked` · `picked_up` · `in_transit` · `out_for_delivery` · `delivered` ·
`failed` · `returned` · `cancelled`

A **webhook** to us on status change is better than us polling. If you can offer
one, we will provide an endpoint and a shared secret for signature verification.

---

## 11. Sandbox

Before we integrate we need a sandbox that:

- uses a separate API key and never dispatches a real rider,
- returns deterministic prices for given weights, so our tests can assert exact
  figures rather than "some number",
- can be made to return each error in §9 on demand (a magic weight, an address
  flag — your choice, just document it).

---

## 12. Versioning

The path carries the version (`/v1/...`). Within `v1`:

- adding a new optional response field is fine, we ignore what we do not read;
- removing a field, renaming one, or changing a type is **breaking** — ship it
  as `/v2` and give us a deprecation window on `/v1`.

---

## 13. Checklist for the courier build

- [ ] `POST /v1/rates` — weight-based quote, `serviceable: false` for unserved areas
- [ ] `X-Api-Key` auth, separate sandbox and production keys
- [ ] `quote_id` honoured until `expires_at` (≥ 15 min)
- [ ] Minimum billable weight defined, including behaviour at 0 kg
- [ ] `POST /v1/shipments` idempotent on `order_number`
- [ ] Consistent error body with stable `code` values
- [ ] `GET /v1/shipments/{id}` with the §10 status vocabulary
- [ ] Sandbox with deterministic pricing and forceable errors
- [ ] Rate limit and p95 latency documented

---

## 14. What changes on the SRIBEESonline side

For reference, when this is implemented here:

- `PricingService._delivery_fee()` calls `/v1/rates` instead of returning the
  flat fee. It already receives the cart items, so weight can be resolved there.
- Product weights get looked up at quote time from the database — not read from
  the Redis cart, for the same reason cashback rates are not: a cart can be days
  old and must not fix a stale price. (See `OrderService._attach_cashback_rates`
  for the existing pattern.)
- `quote_id` is stored on the order so booking can reference the exact quote the
  customer accepted.
- The courier base URL and API key become settings, so sandbox and production
  differ by configuration only.

---

## 15. Delivery verification code (prepaid orders)

For prepaid orders (anything not `payment_method: "cod"`), SRIBEESonline
generates a short numeric code once payment is confirmed. The customer reads
it aloud to your rider at handover; your rider (or your app) confirms it with
us before marking the parcel delivered. This is an anti-fraud handover check,
not a payment mechanism — do not collect money against it.

### What you get

Nothing extra in `/v1/rates` or `/v1/shipments` — the code is generated after
booking and is not part of either request or response. Your rider learns it
lives by asking the customer at the door, the same way an OTP-at-door works
for other delivery apps.

### Confirming a code

```http
POST https://api.sribees.lk/api/v1/courier/webhooks/orders/{order_number}/delivery-code/confirm
X-Api-Key: <your inbound API key -- different from the key you use to call us>
Content-Type: application/json

{ "waybill_number": "SL123456789", "code": "482" }
```

Response — correct code:

```json
{ "success": true, "message": "Code confirmed." }
```

Response — wrong code (ask again, up to 5 attempts total):

```json
{ "success": false, "message": "Code did not match. 3 attempt(s) left." }
```

After 5 wrong attempts the order is flagged for manual support review and
this endpoint stops accepting attempts for it (`success: false`, "too many
attempts").

### Shipment status push

Push shipment status changes (the section 10 vocabulary) to:

```http
POST https://api.sribees.lk/api/v1/courier/webhooks/shipment-status
X-Api-Key: <your inbound API key>
Content-Type: application/json

{ "shipment_id": "shp_01HX9K3P7B", "status": "out_for_delivery", "at": "2026-08-09T06:12:00Z" }
```

We map `status: "delivered"` straight onto the order — send it whenever you
actually consider the parcel delivered, independent of whether the code has
been confirmed (confirm the code at or before marking delivered, whichever
order fits your flow).

### Notes

- The code is always exactly 3 digits (000–999). It is single-use per order —
  once confirmed it cannot be confirmed again.
- `payment_method: "cod"` orders never get a code and can be delivered
  without calling either endpoint above.
- Both endpoints use a **separate inbound API key** from the one you receive
  for us to call — ask us for it alongside your sandbox `/v1/*` credentials.
