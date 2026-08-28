# Mock Courier — DEV/TEST ONLY

Stand-in for SribeesExpress as described in
[`../docs/SRIBEESEXPRESS_ECOMMERCE_API.md`](../docs/SRIBEESEXPRESS_ECOMMERCE_API.md).
**Not the real courier.** Never point a production `COURIER_BASE_URL` at this.

It exists so the backend's courier client (`app/services/courier_service.py`),
the handover-code flow and the signed-webhook path can be exercised end-to-end
before real SribeesExpress sandbox keys are issued.

## Running it

```bash
cd fastapi_backend
uvicorn mock_courier.main:app --port 9100 --reload
```

Then point the real backend's `.env` at it:

```
COURIER_BASE_URL=http://localhost:9100
COURIER_API_KEY=sk_test_sribees-mock-courier-key
COURIER_WEBHOOK_SECRET=sribees-mock-webhook-secret
```

`COURIER_BASE_URL` is the **origin only** — the client appends
`/api/v1/ecommerce` itself.

Environment variables this process reads (all optional, sensible defaults):

| Var | Default | Purpose |
|---|---|---|
| `MOCK_COURIER_API_KEY` | `sk_test_sribees-mock-courier-key` | The key it requires on every `/api/v1/ecommerce/*` call (must match the backend's `COURIER_API_KEY`) |
| `MOCK_COURIER_BACKEND_URL` | `http://localhost:8000` | Where the `_advance` helper pushes webhooks back into the real backend |
| `MOCK_COURIER_WEBHOOK_SECRET` | `sribees-mock-webhook-secret` | HMAC secret it signs those pushes with (must match the backend's `COURIER_WEBHOOK_SECRET`) |

## What it implements

| Contract section | Endpoint |
|---|---|
| 5a | `POST /api/v1/ecommerce/locations/resolve` |
| 5b | `GET /api/v1/ecommerce/cities` |
| 5c | `GET /api/v1/ecommerce/coverage` |
| 6 | `POST /api/v1/ecommerce/rates` |
| 7 | `POST /api/v1/ecommerce/shipments` |
| 7a | `GET /api/v1/ecommerce/shipments` |
| 8 | `GET /api/v1/ecommerce/shipments/{id}` |
| 8a | `PATCH /api/v1/ecommerce/shipments/{id}` |
| 9 | `POST /api/v1/ecommerce/shipments/{id}/cancel` |
| 10.1 | `POST /api/v1/ecommerce/webhook-endpoints` |
| 12 | `POST`/`GET`/`PATCH`/`DELETE /api/v1/ecommerce/pickup-locations` |

## Deterministic pricing

```
amount = 100.00 + max(0, weight_kg - 1) * 50.00
```

First 1kg at the base rate; LKR 50/kg after that. **Weight only** — `cod_amount`
and `payment_method` are accepted and ignored, exactly as the real contract
says (section 6). Pricing COD differently here would hide a production bug
behind a green local test.

## Post-office directory

A small hardcoded table in `mock_courier/state.py`, deliberately incomplete so
every `resolved: false` reason is reachable locally:

| Post office | Resolves to |
|---|---|
| `Colombo` / `Welipenna` / `Kandy` / `Galle` | a real `city_id` |
| `Nugegoda` | `ambiguous_post_office` (two districts) — resend with `district` |
| `Medawachchiya`, `NOT_CONFIGURED_TEST` | `not_yet_configured` |
| `NO_DELIVERY_TEST` | `no_delivery_to_this_area` |
| anything else | `post_office_not_found` |

## Forcing error scenarios

All via magic values in the request body — no separate config needed.

| Trigger | Behavior |
|---|---|
| `weight_kg >= 900` | `POST /rates` → `200 {"serviceable": false, "reason": "sandbox: simulated unserviceable area"}` (the contract's own section 11 trigger) |
| `weight_kg <= 0` or `> 1000` | `POST /rates` → `422 invalid_weight` |
| `weight_kg == 899` | Quote's `expires_at` is 5s out instead of 30 minutes — lets a test observe real expiry (`409 quote_expired` at booking) without waiting |
| `city_id == 500` | `POST /rates` → `500` — exercises the backend's one-retry-then-fallback |
| `city_id == 504` | `POST /rates` sleeps 5s — exercises the 3s timeout + flat-fee fallback |
| unknown `city_id` | `200 {"serviceable": false}` — a place we don't deliver to is an answer, not an error |
| Missing/wrong `X-Api-Key` | `401` on any `/api/v1/ecommerce/*` route (a real check, not a magic value) |
| Re-`POST /shipments` with a seen `merchant_order_ref` | `200` with the *existing* shipment (real idempotency) |
| `POST /shipments` with a used `quote_id` | `409 quote_already_used` |
| `POST /shipments` with an expired `quote_id` | `409 quote_expired` |
| `handover_code` alongside a non-zero `cod_amount` | `400 handover_code_not_allowed_for_cod` — the real service's prepaid-only rule |
| `handover_code_required: true` with no code | `400 handover_code_required_without_code` |
| `PATCH /shipments/{id}` once `out_for_delivery` | `409 shipment_not_correctable` |
| `POST /shipments/{id}/cancel` after pickup | `409 conflict` |

## Test-harness-only endpoint

`POST /api/v1/ecommerce/shipments/{shipment_id}/_advance`

```json
{ "status": "delivered", "handover_code": "4821", "cod_collected": true }
```

**Not part of the real contract** — hence the leading underscore. Drives a
shipment to any status and pushes a properly **HMAC-signed**
`shipment.status_changed` webhook back into the real backend, so the whole
delivered / handover / COD path can be exercised without a rider app.

Two things it deliberately does *not* shortcut:

- The push is signed for real (`X-Sribees-Signature`, section 10.3). An
  unsigned convenience path would let the backend's signature verification rot
  untested — and that verification is the only thing standing between the
  orders table and the open internet.
- A code-required shipment refuses to reach `delivered` without a matching
  `handover_code`, mirroring the real rule. Otherwise local testing would pass
  on a path production rejects.

Note that `cod_collected` is a separate field from `status`, on purpose: the
real contract flips `cod.collected` independently of `delivered`, so the mock
lets you produce the awkward case — delivered, cash *not* collected — that the
backend has to handle.

## Resetting state

State is a plain in-memory dict (`mock_courier/state.py`) — restart the
process to reset everything. There is no persistence, by design; this is a
test double, not a database.
