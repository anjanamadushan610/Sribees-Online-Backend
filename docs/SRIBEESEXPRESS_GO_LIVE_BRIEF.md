# SribeesExpress integration — what is left on your side

**Date:** 2026-09-11
**From:** the SribeesExpress team
**Status of our side:** deployed, seeded, and verified against a real sandbox key
today. Every request and response quoted below is a real one from
`https://devapiexpress.sribees.com`, not an example.

You have a sandbox API key for this merchant. It is a credential — put it in
`.env` (already gitignored), never in a committed file.

---

## 1. It works end to end today. Here is the proof.

Using your sandbox key against the live development API, this whole sequence
succeeded a few minutes before this document was written:

| Step | Call | Result |
|---|---|---|
| Discover | `GET /api/v1/ecommerce/cities` | `[{"id":1,"name":"Matugama","district":"Kalutara"}]` |
| Resolve | `POST /api/v1/ecommerce/locations/resolve` `{"post_office":"Matugama","district":"Kalutara"}` | `{"resolved":true,"city_id":1,"city_name":"Matugama"}` |
| Quote | `POST /api/v1/ecommerce/rates` `{"city_id":1,"weight_kg":"1.5","cod_amount":"4500.00"}` | `380.00 LKR`, `quote_id: qt_…` |
| Book | `POST /api/v1/ecommerce/shipments` with that `quote_id` | `SXP0000000001`, status `booked` |
| Read back | `GET /api/v1/ecommerce/shipments/SXP0000000001` | same order |
| Track (public, no auth) | `GET /api/v1/public/track/SXP0000000001` | `{"current_status":"Pending","destination_city":"Matugama", …}` |
| Reconcile | `GET /api/v1/ecommerce/shipments?limit=5` | the order |

The Kalutara district is configured: all **84** of its post offices are attached
to the `Matugama` delivery city, which sits in an active pricing zone served by
the Matugama branch. `GET /api/v1/ecommerce/coverage` returns those 84 rows.

So nothing on our side is blocking you. What follows is entirely in your repo.

---

## 2. Three blockers in your code

These were found by reading `fastapi_backend/app/`, not guessed. **No API key
will work until the first two are fixed**, because they are not authentication
problems — they are wiring problems.

### 2.1 Your client calls a path that does not exist

`app/services/courier_service.py` builds every URL as:

```python
url = f"{settings.courier_base_url.rstrip('/')}{path}"   # line ~75
```

and every `path` is written as `/v1/rates`, `/v1/shipments`,
`/v1/shipments/{id}`, `/v1/shipments/{id}/cancel`.

Our endpoints are `/api/v1/ecommerce/rates` and friends. **There is no value of
`COURIER_BASE_URL` that produces that from a `/v1/…` path** — setting the base to
`https://devapiexpress.sribees.com/api/v1/ecommerce` yields
`…/ecommerce/v1/rates`, which is a 404.

**Fix:** drop the `/v1` segment from the paths in `courier_service.py` and set

```
COURIER_BASE_URL=https://devapiexpress.sribees.com/api/v1/ecommerce
```

so `/rates` becomes `…/api/v1/ecommerce/rates`. It is a one-file change.

### 2.2 Your webhook receiver expects an API key; we send a signature

`app/api/v1/courier_webhooks.py`'s `require_courier_key` demands
`X-Api-Key == settings.courier_inbound_api_key`.

We do not send `X-Api-Key` at all. Every webhook we deliver carries:

```
X-Sribees-Event: shipment.status_changed
X-Sribees-Signature: t=<unix_ts>,v1=<hex hmac-sha256>
```

The signed message is `"{t}.{raw_request_body}"`, keyed with the webhook secret
we return to you once when you register the endpoint via
`POST /api/v1/ecommerce/webhook-endpoints`. The timestamp is inside the signed
material so a captured signature cannot be replayed against a different body.

**Fix:** replace `require_courier_key` with HMAC verification —

1. read the **raw** body (not the parsed model; re-serialising changes bytes),
2. split `X-Sribees-Signature` on `,` into `t=…` and `v1=…`,
3. recompute `hmac_sha256(secret, f"{t}.".encode() + raw_body).hexdigest()`,
4. compare with `hmac.compare_digest`,
5. reject a timestamp older than ~5 minutes.

`settings.courier_inbound_api_key` becomes dead once this lands.

### 2.3 Your post office names do not match ours — two of your three are wrong

This one would have failed silently as "that address is not serviceable".
Matching is **exact and case-insensitive**; there is no fuzzy fallback.

| Your `migrations/018_post_office_directory.sql` | Our directory | Resolves? |
|---|---|---|
| `Mathugama` | `Matugama` | **No** — `post_office_not_found` |
| `Meegahathanna` | `Meegahatenna` | **No** — `post_office_not_found` |
| `Welipenna` | `Welipenna` | Yes |

**Do not hand-correct these three.** Our directory holds 2,111 post offices
covering all 25 districts, and typing them is how the next mismatch happens.

**Fix:** populate `post_office_directory` from
`GET /api/v1/ecommerce/coverage`, which exists for exactly this purpose. It
returns every post office we can deliver to, in our spelling:

```json
[{"post_office":"Agalawatta","district":"Kalutara","province":"Western Province","city_id":1}, …]
```

Re-sync it periodically — coverage grows as districts are assigned to cities.
A post office missing from that response is one we cannot deliver to yet, which
is the honest thing to show at checkout.

---

## 3. One trap that will silently lose the street address

`POST /ecommerce/shipments` accepts either a flat `recipient_address` **or** the
structured fields (`address_line1`, `address_line2`, `post_office`, `district`,
`province`, `postal_code`). When structured fields are present they are used
**instead of** the flat string, not merged with it.

We booked a test order sending both:

```json
{"recipient_address": "12 Temple Road, Matugama",
 "post_office": "Matugama", "district": "Kalutara", "province": "Western Province"}
```

and the stored address came back as `"Matugama, Kalutara, Western Province"` —
the house number and street were dropped, because they were only in the flat
string and `address_line1` was empty.

**Put the street in `address_line1`** whenever you send structured fields.

---

## 4. Things we do not call, so decide what to do with them

- `POST /courier/webhooks/orders/{order_number}/delivery-code/confirm` — we never
  call this. Handover verification happens on our side: the merchant supplies
  `handover_code` at booking (**prepaid orders only**), and our rider enters it
  in the rider app, which verifies it against our own record. Keep the endpoint
  only if something else uses it.
- COD orders take **no** handover code. A rider holding the customer's cash is
  its own proof of delivery, and requiring a code as well just fails deliveries.

---

## 5. Configuration

```
COURIER_BASE_URL=https://devapiexpress.sribees.com/api/v1/ecommerce
COURIER_API_KEY=<the sk_test_… key, from the password manager — not in git>
COURIER_REQUEST_TIMEOUT_SECONDS=3.0
# COURIER_INBOUND_API_KEY is obsolete once 2.2 lands — the webhook secret
# returned by POST /ecommerce/webhook-endpoints replaces it.
```

The key is environment-scoped: a `sk_test_` key books sandbox orders, a
`sk_live_` key books real ones. The same code path serves both, so switching
environments is a credential change and nothing else.

Rate limit is attached to the key itself, not to a server setting. Yours is set
per-key; if you need it raised, ask rather than retrying into a 429.

---

## 6. Suggested order of work

1. **2.1** (paths) — unblocks every call. Verify with
   `POST /rates` returning a `quote_id`.
2. **2.3** (coverage sync) — unblocks checkout for real addresses.
3. **3** (`address_line1`) — one line, prevents undeliverable parcels.
4. Wire `PricingService._delivery_fee()` to `CourierService.get_rates()` instead
   of `mock_courier`, and book on order confirmation.
5. **2.2** (webhook HMAC) — needed for status updates to flow back, but the
   forward path works without it, so it need not block a first end-to-end test.

## 7. What a full lifecycle test looks like

Against the Matugama branch, with a real customer address in Kalutara:

1. Checkout quotes delivery — your `PricingService` → our `/rates`.
2. Order confirmed — your order service → our `/shipments`, store the returned
   `shipment_id` (our waybill) against your order.
3. We move it: `pending → pickup_scheduled → picked_up →
   collected_at_sorting_center → dispatched_to_destination →
   received_at_destination → out_for_delivery → delivered`.
4. Each transition fires a webhook to your registered endpoint (needs 2.2).
5. `GET /ecommerce/shipments?status_filter=delivered` reconciles what we think
   against what you think — run it even after webhooks work, because a webhook
   that never arrives is invisible otherwise.
6. COD is remitted weekly: `GET /ecommerce/remittances` and
   `GET /ecommerce/cod-balance`.

Ask us for anything in section 1 to be re-checked against the live API — it is
a two-minute job on our side and beats guessing.
