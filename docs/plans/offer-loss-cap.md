# Offer loss cap (owner, 2026-11-11)

## Task

The owner puts chosen products on Quick Sale below the normal price (11.11: Sakwa
salmon 560 -> 400, white sugar 260 -> 199 per kg, Munchee 490g 480 -> 390, Colmans
Mango 290 -> 200, two Eva 10+2 packs 270 -> 198). Each customer may take at most a
hidden amount of "loss" from one offer, e.g. Rs 1,000, summed over every item of
that offer. The customer is never told the amount; only when they reach it does
the cart say so.

Loss of a line = (normal price - offer price) x quantity. Example: one kg of sugar
= 260 - 199 = Rs 61, so about 16 kg fit in Rs 1,000; if a salmon tin (Rs 160) is
already in the cart, the room left is shared.

## Owner's decisions (MCQ, 2026-11-11)

| Question | Answer |
|---|---|
| Period the cap counts over | The whole offer (all orders together), not per order or per day |
| Which products count | Only products the admin puts in the offer (older small Quick Sale discounts are untouched) |
| Cashback in the loss | No: price difference only (cashback is separate) |
| Where the cap is set | Per branch, by admin. 0 / no offer = no cap |
| Offer start / end | Admin sets start and end. A new offer starts everyone at 0. At the end the prices go back by themselves |
| Cancel / return | A cancelled order gives its loss back; a return does not |
| Last item that would cross the cap | Refused: the cap is never exceeded |
| At the cap | Further offer items CANNOT be added (blocked, not sold at normal price) |
| Message (si) | "ඔබගේ offer සීමාව සම්පූර්ණයි. මෙම offer භාණ්ඩය තවදුරටත් එකතු කළ නොහැක." (en/ta same meaning) |

## Design

Tables (migration 111, additive, replay-safe):

- `offer_caps` -- one offer: branch, name, cap_amount, starts_at, ends_at, status
  (scheduled / live / ended / cancelled), applied_at, restored_at, created_by.
- `offer_cap_items` -- product + offer price, and the branch_inventory values before
  the offer (branch_price, discount_percentage, is_on_sale) so the end restores them.
- `offer_cap_usage` -- one row per order per offer: amount of loss the order took;
  released_at set when the order is cancelled. Unique (offer_id, order_id).

Prices: the offer is applied as an ordinary Quick Sale (branch_price = normal price,
discount% to the offer price; weight options follow the product's discount as they
already do). A worker applies at starts_at and restores at ends_at. Nothing in the
price code changes.

Gates, all server-side:

1. `POST /cart/items`, `PUT /cart/items/{id}` (increase only): if the cart after the
   change would take used + cart loss above the cap -> 400 with the message in the
   request language. The app already shows a 400 `detail` as the cart error, so no
   app release is needed.
2. Every cart response carries `offer_limit: {reached, message}` (reached = no offer
   unit can be added any more, or a merge/sync pushed the cart over).
3. Checkout (`create_from_cart`) is the authoritative gate: under the customer's row
   lock it recomputes from the catalogue, refuses an order above the cap, and writes
   the usage row in the same transaction.
4. Cancellation (`_apply_cancellation_effects`) sets released_at. Returns do not.

Admin API (super admin): list / create / edit (before start) / end-now offers for a
branch; per-offer usage report. Admin page in the admin repo follows.

Loss is computed with Decimal from the catalogue (never the client's numbers):
normal unit price = branch_price or product price without discount (weight option:
its original price); offer unit price = what checkout charges.

## Done = these pass

`tests/test_offer_cap.py`:

1. Sugar 61/kg, cap 1000: 16 kg fit (976), the 17th is refused (1037 > 1000) on add,
   on update and at checkout.
2. Salmon (160) then sugar: room is shared (160 + 13 x 61 = 953; 14th refused).
3. Two orders: the second sees the first order's usage.
4. Cancelled order -> its usage released -> can buy again. Returned order -> not.
5. A product not in the offer, and an older Quick Sale item, never counted.
6. Offer ended / not started -> no cap, prices restored exactly to the saved values.
7. Merge/sync pushing the cart over -> `offer_limit.reached` + checkout refused.
8. Message language follows Accept-Language (si / en / ta); never shows the amount.
9. Two concurrent checkouts cannot both take the last room (row lock).

Plus: migration 111 replayed twice on a throwaway Postgres; full suite green;
fresh-context reviewer (money = high risk) before main.
