# Mock Courier — DEV/TEST ONLY

Stand-in for the courier described in [`../docs/COURIER_API.md`](../docs/COURIER_API.md).
**Not the real courier.** Never point a production `COURIER_BASE_URL` at this.

It exists so the backend's courier client (`app/services/courier_service.py`)
and the delivery-verification-code flow (`COURIER_API.md` section 15) can be
tested end-to-end before the real, in-house courier service is built.

## Running it

```bash
cd fastapi_backend
uvicorn mock_courier.main:app --port 9100 --reload
```

Then point the real backend's `.env` at it:

```
COURIER_BASE_URL=http://localhost:9100
COURIER_API_KEY=sribees-mock-courier-key
COURIER_INBOUND_API_KEY=sribees-mock-courier-inbound-key
```

Or run it as an opt-in Docker Compose service (`--profile courier-dev`) — see
the `mock_courier` service block in the root `docker-compose.yml`.

Environment variables this process reads (all optional, sensible defaults):

| Var | Default | Purpose |
|---|---|---|
| `MOCK_COURIER_API_KEY` | `sribees-mock-courier-key` | The key it requires on every `/v1/*` call (must match the backend's `COURIER_API_KEY`) |
| `MOCK_COURIER_BACKEND_URL` | `http://localhost:8000` | Where the simulate-delivery helper calls back into the real backend |
| `MOCK_COURIER_INBOUND_KEY` | `sribees-mock-courier-inbound-key` | The key it sends on that callback (must match the backend's `COURIER_INBOUND_API_KEY`) |

## Deterministic pricing

```
amount = 300.00 + max(0, weight_kg - 1) * 60.00 + (1% of order_value_lkr if payment_method == "cod")
```

First 1kg is free; LKR 60/kg after that. A test can assert an exact figure for
a given input.

## Forcing error scenarios

All via magic values in the request body — no separate config needed.

| Trigger | Behavior |
|---|---|
| `parcel.total_weight_kg == 999` | `POST /v1/rates` → `422 weight_exceeds_maximum` |
| `destination.post_office == "UNSERVICEABLE_TEST"` | `POST /v1/rates` → `200 {"serviceable": false, ...}` |
| `destination.post_office == "TIMEOUT_TEST"` | `POST /v1/rates` sleeps 5s before responding — exercises the backend's 3s timeout + flat-fee fallback |
| `destination.post_office == "ERROR_500_TEST"` | `POST /v1/rates` → `500` — exercises the backend's one-retry-then-fallback |
| `parcel.total_weight_kg == 998` | Quote's `expires_at` is 5s out instead of 15 minutes — lets a test observe real quote expiry (`409 quote_expired` at booking) without waiting |
| Missing/wrong `X-Api-Key` | `401` on any `/v1/*` route (a real check, not a magic value) |
| Re-`POST /v1/shipments` with a seen `order_number` | `200` with the *existing* shipment (real idempotency) |
| `POST /v1/shipments` with an expired `quote_id` | `409 quote_expired` |

## Test-harness-only endpoint

`POST /v1/shipments/{shipment_id}/simulate-delivery` — body `{"code": "482"}`.

**Not part of the real courier contract.** Lets a developer manually drive
"the rider entered this code at the door" during local testing, since no real
rider app exists yet. Marks the shipment `delivered` and calls the real
backend's `POST /courier/webhooks/shipment-status` and
`POST /courier/webhooks/orders/{order_number}/delivery-code/confirm` with it.

## Resetting state

State is a plain in-memory dict (`mock_courier/state.py`) — restart the
process to reset everything. There is no persistence, by design; this is a
test double, not a database.
