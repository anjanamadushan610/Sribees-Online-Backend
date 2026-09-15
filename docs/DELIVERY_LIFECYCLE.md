# Delivery lifecycle — cart to doorstep

How a SRIBEESonline order gets delivered, end to end, after the 2026-09-13
change that removed the admin-set delivery fee and made SribeesExpress the sole
source of the delivery charge.

Read `SRIBEESEXPRESS_ECOMMERCE_API.md` for the courier contract itself; this
document is our side of it.

## The one rule

**We never invent a delivery charge.** Every charge shown to a customer is a
SribeesExpress quote for the cart's real weight, priced between two postal
cities. If SribeesExpress cannot price it, the cart shows *why* and checkout is
refused — it does not fall back to a flat fee. `flat_delivery_fee`,
`delivery_fee` and `free_delivery_threshold` are gone from platform settings and
the admin API rejects them (`422`).

## Vocabulary

"Post office" is now **postal city** everywhere in code, API and UI. The
database tables and columns keep their old names (`post_office_directory`,
`branches.post_office`, `addresses.post_office`) because every migration is
replayed on each deploy, which makes a physical rename unsafe; the ORM maps them
to `postal_city`. Requests still accept the legacy `post_office` / `postOffice`
spellings so an installed mobile app keeps working (`POSTAL_CITY_ALIASES` in
`app/schemas/branch.py`), and responses carry both keys.

## Who is who

Each **branch is its own SribeesExpress client**, with its own API key
(encrypted in `branches.courier_api_key_*`, never in env). That is what makes
origin pricing work without any extra plumbing: SribeesExpress identifies the
branch from the key and prices from the postal city that client registered with.
Branches that share the account key are registered as **outlets** instead
(`POST /admin/courier/outlets/sync`), one per branch, each with its own postal
city.

## The flow

| # | Where | What happens |
|---|---|---|
| 1 | Customer saves an address | `courier_location_service.resolve_address` matches it to a SribeesExpress postal city id and stores it on the address. No id ⇒ `delivery_available: false` on that address. |
| 2 | Cart page (`GET /cart`, optional `address_id`) | `delivery_quote_service.quote_for_cart` totals the cart's weight (`cart_weight_kg`), takes the customer's postal city and the branch's key, and calls `POST /ecommerce/rates`. The result is cached in Redis for 10 minutes (`courier:cart-quote:v1`). |
| 3 | Cart response | `totals.shipping` is the quote; `delivery` explains it: `status` is `quoted`, `empty_cart`, `no_address`, `not_serviceable` or `unavailable`, plus `available`, `fee`, `message`, `postal_city`. A courier outage never fails the cart — it shows `unavailable` with a reason. |
| 4 | Checkout preview (`POST /orders/quote`) | Same quote, plus `delivery_available` / `delivery_message` so the app can block Place Order before the customer taps it. |
| 5 | Order placed (`POST /orders`) | `order_service.create_from_cart` refuses (`DeliveryNotAvailableError`) unless the address has a postal city, a serving branch, and a live quote. It then calls `POST /ecommerce/rates/{quote_id}/confirm`, holding that price for **7 days**, and stores `courier_quote_amount` / `courier_quote_confirmed_at` on the order. **Nothing is booked with the courier yet.** |
| 6 | Payment | COD is settled at the door; a gateway order only reaches a dispatchable state once payment confirms (`payment_blocks_dispatch`). |
| 7 | Branch manager packs it and moves the order to **Ready for Courier Pickup** (`ready_for_pickup`) | This is the booking trigger. `book_courier_shipment_now` sends `POST /ecommerce/shipments` with the held quote, the `payment_method`, and for COD the **full** amount the rider collects (goods + delivery). An expired hold is re-quoted rather than failing the parcel. Booking failure keeps the status change from succeeding (`409` to the admin UI) instead of silently leaving a parcel nobody collects. |
| 8 | SribeesExpress dispatch board | The parcel appears under the branch's pickup postal city. A dispatcher assigns a rider for that area in bulk. |
| 9 | Status updates | Webhooks (per-account secrets, verified against every stored secret) push `picked_up`, `in_transit`, `out_for_delivery`, `delivered`, `failed`; mirroring is **forward-only**, so a late webhook cannot walk an order backwards. Because SribeesExpress does not retry webhooks, `POST /admin/courier/sync` sweeps **every branch account** and is the real source of truth — run it on a cron every 10-15 minutes. |
| 10 | COD money | Collected by the rider, then remitted by SribeesExpress. `GET /admin/courier/cod-balance` and `/remittances` show what is owed and what has been paid, per branch. |

## Order statuses

`ready_for_pickup` ("Awaiting Courier Pickup") sits between `packed` and
`handed_to_courier`. It is the only status that books with the courier, and the
only non-terminal one a customer cannot cancel from.

## Operational checklist for a new branch

1. Give the branch its SribeesExpress API key (Settings → Courier → Credentials).
2. Probe it (Test key) — it should report the number of postal cities visible.
3. Sync postal cities, so saved addresses carry courier ids.
4. Sync outlets — a branch with its own key reports `own_account` and needs none.
5. Register the status webhook for that account.
6. Set the branch's coverage postal cities, so customers there resolve to it.
7. Schedule the reconciliation sweep.

## Where the code is

- `app/services/delivery_quote_service.py` — cart quote + cache + `delivery` payload
- `app/services/pricing_service.py` — totals; no flat fee anywhere
- `app/services/order_service.py` — confirm at checkout, book at `ready_for_pickup`
- `app/services/courier_service.py` — the SribeesExpress client
- `app/services/courier_credentials.py` — per-branch keys and webhook secrets (encrypted)
- `app/services/courier_coverage_service.py` — postal city sync and address re-matching
- `app/services/courier_sync_service.py` — multi-account reconciliation sweep
- `app/api/v1/courier_webhooks.py` — signature verification against every stored secret
