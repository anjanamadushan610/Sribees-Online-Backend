# Per-product referral commission

Status: built and tested on `development` (2026-10-19). Not on `main` until the owner approves.

## 1. Why

Today every referred order pays one flat rate on the whole basket:
`settings.referral_commission_percent` (2%) to level 1 and
`settings.referral_level2_percent` (1%) to a partner's upline, both applied to
`orders.subtotal`. Some products cannot carry a commission at all (thin
margins, regulated goods), so a flat rate either loses money on those lines or
under-rewards the rest.

## 2. Decisions (owner, 2026-10-19)

| Topic | Decision |
|---|---|
| Who it applies to | Both: professional partners and customer app invites |
| Rate fields | One per product: the level-1 %. Level 2 = level-1 x `level2_share` (default 0.5, the old 2:1 ratio) |
| Who sets it | Super Admin sets the product default. Branch Manager overrides it for their own branch |
| Unset rate | 0%. A product earns nothing until someone sets a rate |
| Basis | What the customer paid for the goods: line subtotal minus that line's share of the coupon discount. Excludes tax and delivery. Wallet is a payment method, not a discount, so it does not reduce the basis |
| Branch cap | 10% by default. Super Admin can change it in Settings |
| Partner dashboard | Per order: product name, quantity, rate, Rs earned. Plus a "top earning products" summary. Never the basket value |
| Customer app text | Server-driven en/si/ta sentence. Mobile change goes on `development` only |

## 3. Rules

These are the invariants. The tests assert them.

1. **Rates resolve as branch override, then product default, then 0.** The
   rate is resolved once, at checkout, from the database. A cart can sit in
   Redis for days, so the cart's own copy of the rate is never used.
2. **The rate is a snapshot.** It is frozen onto `order_items` at checkout.
   Changing a rate later never changes what an order already earned (same rule
   as `cashback_percentage`, migration 065).
3. **Money is `Decimal`, never float.** Each line rounds half-up to the cent.
   The commission header is exactly the sum of its lines, so the ledger and
   the dashboard can never disagree by a cent.
4. **The coupon discount is shared out exactly.** It is spread over the lines
   the coupon was allowed to touch, in proportion to their value. Any leftover
   cents go to the lines with the largest remainders, so
   `sum(order_items.coupon_discount) == orders.discount_amount` always holds.
5. **A zero-rate line earns nothing and writes no line row.** An order whose
   lines all earn zero writes no commission header either (today's behaviour
   for a zero amount).
6. **A return takes back only what the returned units earned.** It is
   computed per line as `(quantity - returned_quantity) / quantity` of that
   line's commission. A paid row is clawed back by the difference only, as
   today.
7. **Rows written before this change keep working.** They have no lines.
   Reads show them as one "whole order" line, and returns use the old
   proportional path.
8. **The cap is enforced on the server.** The admin UI also refuses a value
   over the cap, but only the server check counts. Every change is audited
   (who, which product, which branch, old value, new value).

## 4. Schema (migration 081, idempotent, replayed on every deploy)

- `products.referral_commission_percentage NUMERIC(5,2) NULL`, CHECK 0..100.
  NULL means no default (earns 0 unless a branch overrides).
- `branch_inventory.referral_commission_percentage NUMERIC(5,2) NULL`,
  CHECK 0..100. NULL means inherit the product default.
- `order_items.referral_commission_percentage NUMERIC(5,2) NULL`: the
  level-1 snapshot. NULL marks an order placed before this feature.
- `order_items.coupon_discount NUMERIC(10,2) NOT NULL DEFAULT 0`: this line's
  share of the coupon discount.
- `referral_commission_lines`: `line_id`, `commission_id` (FK, cascade),
  `order_item_id` (FK), `product_id`, `product_name` (snapshot), `quantity`,
  `basis`, `rate_percentage NUMERIC(6,3)`, `amount`, `returned_quantity`.
  UNIQUE (`commission_id`, `order_item_id`), with CHECKs on non-negative
  money and `0 <= returned_quantity <= quantity`.
- `referral_rate_changes` (audit): `product_id`, `branch_id` (NULL for the
  product default), `old_percentage`, `new_percentage`, `changed_by`,
  `changed_at`.
- No new permission row. The routes are role-gated (`super_admin`,
  `branch_manager`), the same as partners and settings. A Branch Manager
  cannot delegate this to a staff role, and Inventory Managers can edit stock
  but not money.
- `app_settings` keys: `referral_branch_max_percent` (10) and
  `referral_level2_share` (0.5).

## 5. Code shape

```
app/services/referral_commission/
  calculator.py   pure functions, no DB: allocate_discount(), build_lines()
  rates.py        CommissionRateResolver (batch lookup: branch -> product -> 0)
  policy.py       LevelPolicy: the rate each level earns from a line's level-1 rate
  config.py       ReferralProgramConfig, read from app_settings with safe defaults
```

- **Pure core, thin shell.** All arithmetic lives in `calculator.py` and takes
  plain values, so the money logic is unit-tested exhaustively without a
  database. The services only load rows, call the core and store the result.
- **Strategy for levels.** `LevelPolicy` decides what level 1 and level 2 earn
  from a line. If the rules change later (for example a separate level-2
  field), only the policy changes.
- **Checkout** (`OrderService.create_from_cart`) resolves the rates with
  `CommissionRateResolver`, shares out the coupon discount with
  `allocate_discount`, and stamps both values on each `OrderItem`. This sits
  beside the existing cashback snapshot.
- **Accrual** (`ReferralService.accrue_for_order`) builds the lines from the
  order items for each beneficiary or level. The header's `order_subtotal` is
  the sum of the bases and its `amount` is the sum of the lines. The header's
  `rate_percentage` holds the blended rate, kept for older readers only.
- **Returns** go through `ReferralService.reduce_for_return(order)`. It is
  line-based when the commission has lines and falls back to the old
  proportional path when it does not.

### API

| Route | Who | What |
|---|---|---|
| `PATCH /admin/products/{id}/referral-commission` | Super Admin | Set or clear the product default |
| `PATCH /admin/inventory/{id}/referral-commission` | Super Admin, Branch Manager (own branch) | Set or clear the branch override, capped |
| `GET/PUT /admin/settings/referral-program` | Super Admin (read: anyone with the permission) | Cap and level-2 share |
| `GET /admin/inventory`, product reads | existing | Add the override, the default and the effective rate |
| `GET /partner/commissions` | Partner | Each row carries `lines` |
| `GET /partner/top-products` | Partner | Earnings grouped by product |
| `GET /partner/me`, `GET /referrals/...` | existing | `commissionPercent` becomes the highest live rate (keeps old apps working). New `commissionText` in en/si/ta |

## 6. Frontends

- **Admin** (310, `development`):
  - Product form: a commission field for Super Admins.
  - Branch Inventory: a commission column and an edit control that shows the
    product default and the cap.
  - Settings: a Referral programme card with the cap and the level-2 share.
- **Partner portal**:
  - Commission rows expand into product lines.
  - A Top products card.
  - The header text comes from the server.
  - Tested locally against the dev API, because this repo has no dev
    deployment.
- **Mobile** (`development` only): the invite screen shows `commissionText`.

## 7. Test plan

1. **Unit tests** for the calculator and the policy:
   - discount allocation is exact (property-style over many random baskets);
   - a coupon restricted to some lines discounts only those lines;
   - rounding;
   - a zero rate earns nothing;
   - level 2 is half of level 1;
   - partial and full returns;
   - legacy rows without lines.
2. **Resolver precedence**: branch override, then product default, then 0.
3. **API tests**:
   - the cap is enforced;
   - a branch manager is limited to their own branch;
   - an inventory manager is refused;
   - every change is audited.
4. **Real Postgres 18** (throwaway database): migration 081 replayed twice,
   then an end-to-end order, commission, return and claim.
5. **Dev server**: deploy `development`, set rates on dev, place an order with
   an OTP test phone and a partner code, check the partner dashboard, then
   return one item.

## 8. Rollout

Backend `development`, then admin `development` (310), then the partner portal
(local against dev), then mobile `development`. **`main` waits for the
owner's explicit go-ahead** (Play review runs from `main`).

Note: when this reaches production, every product earns 0 until rates are set.
That is the owner's choice. Set the rates before announcing the change to
partners.

## 9. Return refunds and coupons (fixed 2026-10-19, owner's go-ahead)

`OrderService._compute_return_refund` used to refund `unit_price x quantity`
and ignore the coupon discount. A returned item from a couponed order was
therefore refunded more than the customer paid for it. Each returned unit
now refunds its price less its share of the coupon (`order_items.coupon_discount`,
pro rata per unit), so a whole-order return refunds exactly
`subtotal - discount`. Orders from before 081 have a discount but no shares,
so their discount is spread over every line by value. Commission rows from
before 081 still reduce on shelf value (`_returned_gross`), the basis they
were computed on. Tax and delivery are not refunded, as before.
