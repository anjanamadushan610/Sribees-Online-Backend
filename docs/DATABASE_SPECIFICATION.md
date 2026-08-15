# SRIBEESonline — Database Specification

**Last verified against the live schema:** 2026-08-10, by querying
`information_schema` directly against a freshly-provisioned database (not
copied from an earlier document) — every table, column, type, nullability,
default and foreign key below is real, not planned.

**Stack:** PostgreSQL 15+ with `pgvector` · Redis 7+ (cache/sessions, not
covered here) · SQLAlchemy 2.0 (async) · **schema managed by
`Base.metadata.create_all()`, not Alembic** — see [How the schema actually
gets built](#how-the-schema-actually-gets-built) below.

---

## Table of contents

1. [How the schema actually gets built](#how-the-schema-actually-gets-built)
2. [Conventions](#conventions)
3. [User accounts & auth](#1-user-accounts--auth)
4. [Admin & RBAC](#2-admin--rbac)
5. [Branches & delivery coverage](#3-branches--delivery-coverage)
6. [Product catalog](#4-product-catalog)
7. [Multi-branch inventory (the global-to-local pattern)](#5-multi-branch-inventory-the-global-to-local-pattern)
8. [Orders & fulfillment](#6-orders--fulfillment)
9. [Wallet & cashback](#7-wallet--cashback)
10. [Marketing: coupons & banners](#8-marketing-coupons--banners)
11. [Reviews & wishlist](#9-reviews--wishlist)
12. [Referrals & partners](#10-referrals--partners)
13. [Branch-to-branch stock transfers](#11-branch-to-branch-stock-transfers)
14. [Notifications](#12-notifications)
15. [App settings](#13-app-settings)
16. [Full foreign-key map](#full-foreign-key-map)
17. [Migration history](#migration-history)

---

## How the schema actually gets built

There is **no Alembic revision history** in this repository (`alembic` is listed in
`requirements.txt` but there's no `alembic/versions/` directory — it's an unused
dependency). Schema comes from two layers, applied in this order on every deploy and
every fresh local setup:

1. **`Base.metadata.create_all()`** — runs in `app/config/database.py`'s `init_db()`,
   called from `app/main.py`'s lifespan on every backend startup. This creates any
   table declared by a SQLAlchemy model under `app/models/` that doesn't already exist.
   This is the source of truth for the base shape of nearly every table.
2. **`fastapi_backend/migrations/*.sql`** — numbered raw SQL files, applied in order
   (by filename, not a version table) after the backend is up. Because `create_all()`
   only *creates missing tables* and never alters existing ones, this is where
   additive changes to already-existing tables live (new columns, new indexes, new
   constraints) — plus all seed data (branches, demo catalog, demo customers/orders,
   coupons, partners). These are written to be safely re-run on every deploy
   (`ON CONFLICT DO NOTHING` / `IF NOT EXISTS` throughout), which is also how a schema
   fix gets rolled out — a later migration `UPDATE`s or `ALTER`s what an earlier one
   got wrong, rather than editing history.

There is deliberately **no custom `sribees` schema** — despite an early prototype
(`docker/postgres/init.sql`) briefly building one. Everything lives in the default
`public` schema. That prototype file actively broke a fresh local database (its
`ALTER DATABASE ... SET search_path` repointed `create_all()`'s own tables into the
wrong namespace, colliding with its stale copies) and was trimmed down to just
`CREATE EXTENSION` statements on 2026-08-10.

---

## Conventions

- **Primary keys**: `uuid`, named `<entity>_id` (e.g. `product_id`), generated
  Python-side (`default=uuid4`), not by a Postgres default — `gen_random_uuid()` only
  shows up in hand-written migration SQL (seed data, additive tables), not in
  `create_all()`-managed columns.
- **Timestamps**: `created_at` / `updated_at` almost everywhere; mostly
  `timestamp with time zone`, but a handful of the earliest tables
  (`users`, `sessions`, `addresses`, `email_verifications`, `password_resets`) are
  `timestamp without time zone` — a real, historical inconsistency, not a typo in this
  doc.
- **Soft state, not soft delete**: most tables use an `is_active` boolean rather than
  a `deleted_at` — nothing in this schema does true soft-deletion.
- **Money**: `numeric` (no fixed precision declared at the column level — precision is
  enforced at the Pydantic schema layer, typically `(10, 2)`).
- **snake_case on the wire for most admin endpoints** (hand-built dicts); a few
  (`admin_auth`, some schemas) use Pydantic's alias mechanism to emit camelCase — see
  each API module's own docstring, this varies file by file and isn't a schema
  concern.

---

## 1. User accounts & auth

Two **completely separate** account systems — a customer signing in never touches the
`admins` table and vice versa. Different JWT claims, different session tables.

### `users` — customer accounts
| Column | Type | Notes |
|---|---|---|
| `user_id` (PK) | uuid | |
| `email` | varchar(255) NOT NULL | |
| `password_hash` | varchar(255) NOT NULL | Argon2 |
| `full_name` | varchar(100) NOT NULL | |
| `first_name`, `last_name` | varchar(50) | populated alongside `full_name` for some flows |
| `phone` | varchar(20) | |
| `profile_picture_url` | text | |
| `is_verified`, `is_phone_verified` | boolean NOT NULL | email vs. OTP phone verification, tracked separately |
| `role` | varchar(20) NOT NULL | almost always `'customer'` — not the admin RBAC role |
| `is_active` | boolean NOT NULL | |
| `two_factor_enabled` | boolean NOT NULL, `two_factor_secret` | present in schema; not seen wired to a UI flow |
| `referral_code` | varchar(12) | this user's own shareable code |
| `referred_by_user_id` → `users.user_id` (SET NULL) | who referred them, if it was another customer | |
| `referred_by_partner_id` → `partners.partner_id` (SET NULL) | or, if it was a partner/agent | |
| `referred_at` | timestamptz | |
| `created_at`, `updated_at` | timestamp (no tz) | |
| `last_login` | timestamp (no tz) | |

### `addresses` — delivery addresses
`address_id` PK · `user_id` → `users` (NO ACTION — the app must handle in-use
addresses itself) · `title`, `recipient_name`, `phone` · `address_line1` NOT NULL,
`address_line2` · `post_office` NOT NULL — the field that drives branch routing (see
§3) · `district`, `province`, `postal_code` NOT NULL · `is_default` boolean.

### `sessions` — customer refresh-token sessions
`session_id` PK · `user_id` → `users` (CASCADE) · `refresh_token_hash` (the raw token
is never stored) · `ip_address`, `user_agent` · `expires_at` NOT NULL.

### `email_verifications`, `password_resets`
Same shape: `*_id` PK, `user_id` → `users`, `token`, `expires_at`, `created_at`.
`password_resets` adds `used` boolean so a token can't be replayed.

### `wallets` / `wallet_transactions` — customer store credit
- **`wallets`**: one row per user (`user_id` → `users`, CASCADE), `balance` numeric NOT
  NULL, `currency` varchar(3) NOT NULL (always `LKR` in practice).
- **`wallet_transactions`**: `transaction_id` PK · `wallet_id` → `wallets` (CASCADE) ·
  `user_id` → `users` (CASCADE, denormalized for query convenience) · `order_id` →
  `orders` (SET NULL — a transaction outlives the order it came from) · `type`
  varchar(20) (credit/debit, e.g. cashback vs. redemption) · `title` NOT NULL ·
  `amount` NOT NULL · `balance_after` — the running balance snapshot at the time of
  this transaction, so history doesn't need to be replayed to show a statement.

---

## 2. Admin & RBAC

### `admins` — the 5-role RBAC system
| Column | Type | Notes |
|---|---|---|
| `admin_id` (PK) | uuid | |
| `email` | varchar(255) NOT NULL | |
| `password_hash` | varchar(255) NOT NULL | Argon2 |
| `full_name` | varchar(100) NOT NULL | |
| `role` | **`admin_role_enum`** (Postgres native enum) NOT NULL | `SUPER_ADMIN`, `BRANCH_MANAGER`, `MARKETING_MANAGER`, `INVENTORY_MANAGER`, `CUSTOMER_SUPPORT` |
| `branch_id` → `branches.branch_id` | nullable | **Required for Branch/Marketing/Inventory Manager; ignored for Super Admin and Customer Support** — see below |
| `is_active` | boolean NOT NULL | |
| `last_login` | timestamptz | |
| `created_at`, `updated_at` | timestamptz | |

**Branch scoping is enforced in code (`app/core/dependencies.py`,
`inject_branch_filter`), not by a database constraint.** Two roles are *unscoped*
(`UNSCOPED_ROLES = {"super_admin", "customer_support"}` — see fix on 2026-08-10) and
see every branch's data regardless of what `branch_id` they carry; Branch Manager,
Marketing Manager and Inventory Manager are each pinned to exactly one branch and
**rejected at the API layer** if `branch_id` is `NULL`. A Customer Support admin's
`branch_id`, if set, is purely informational (shown as a header tag) — it does not
restrict what orders/customers they can see.

Full role → permission matrix (which resource each role can read/write) lives in
`admin/src/types/admin.types.ts` (`ROLE_PERMISSIONS`) on the frontend and is mirrored
by `require_roles(...)` router-level dependencies per endpoint on the backend — there
is no single database table enumerating this; it's declared in code on both sides and
has to be kept in sync by hand.

### `admin_sessions`
Same shape as customer `sessions`: `session_id` PK, `admin_id` → `admins` (CASCADE),
`refresh_token_hash`, `device_info`, `ip_address`, `user_agent`, `expires_at`.

---

## 3. Branches & delivery coverage

### `branches`
`branch_id` PK · `name`, `code` (unique, short — `CMB`, `KDY`, `GLE`, `NGB`, `KUR` for
the five seeded branches) · `address`, `post_office`, `district`, `province` NOT NULL
· `phone` · `manager_id` → `admins.admin_id` (SET NULL) — informational, points at
that branch's Branch Manager · `is_active` NOT NULL.

### `post_office_branch_mapping` — address routing
Maps a Post Office name to the branch that serves it. `mapping_id` PK ·
`post_office` (unique) · `branch_id` → `branches` (CASCADE) · `branch_name`
(denormalized) · `district`, `province`. **This is what assigns an order's
`branch_id`**: a customer's delivery address carries a `post_office`; at checkout,
that's looked up here to resolve which branch fulfils the order.

### `post_office_directory` — the master list (Settings → Delivery Zones)
The full Sri Lanka Post Office → District → Province reference list, independent of
branch coverage — "which Post Offices exist" vs. `post_office_branch_mapping`'s "who
serves them". `id` PK · `post_office` (unique) · `district`, `province` NOT NULL ·
`is_active`.

---

## 4. Product catalog

### `categories`
`category_id` PK · `name`, `slug` (unique) NOT NULL · `description`, `image_url` ·
`parent_category_id` → `categories.category_id` (self-referential, NO ACTION — the
service layer enforces "max 2 levels deep" and blocks deleting a category with
children/products, see `admin_catalog.py`) · `is_active`.

### `products` — the global catalog
| Column | Type | Notes |
|---|---|---|
| `product_id` (PK) | uuid | |
| `name`, `slug` (unique) | NOT NULL | |
| `description`, `short_description` | | |
| `search_keywords` | text | free-text extra terms for the fuzzy/multilingual search fallback (029 migration) |
| `sku` (unique) | | |
| `price` NOT NULL, `compare_at_price`, `cost_price` | numeric | the *global* price — branches can override, see §5 |
| `category_id`, `subcategory_id` → `categories` | both NO ACTION | |
| `stock_quantity`, `low_stock_threshold` | integer | the *global* catalog-level figure; real sellable stock is per-branch, see §5 |
| `weight`, `weight_unit` | | |
| `is_active`, `is_featured` | boolean | |
| `discount_percentage`, `discount_price`, `is_on_sale` | | global discount; a branch can override via `branch_inventory` |
| `cashback_percentage` | double precision | global cashback rate; branch-overridable |
| `view_count` | integer | |
| `meta_title`, `meta_description` | | SEO |
| — *(not shown by `information_schema` as a plain column but present via pgvector)* | `embedding vector(768)` | added by a raw-SQL `ALTER TABLE` for semantic search, Gemini `text-embedding-004` dimension |

### `product_images`
`image_id` PK · `product_id` → `products` (CASCADE) · `image_url` NOT NULL ·
`alt_text` · `is_primary` · `sort_order`.

### `product_variants`, `variant_types`, `variant_options`
A generic variant system (e.g. size/weight options), largely superseded in practice by
the branch-override pattern for pricing but still present and wired into orders/
wishlist:
- **`variant_types`**: `variant_type_id` PK, `name`, `display_name` (e.g. "Size").
- **`variant_options`**: `option_id` PK, `variant_id` → `product_variants` (CASCADE),
  `variant_type_id` → `variant_types`, `value` (e.g. "500g").
- **`product_variants`**: `variant_id` PK, `product_id` → `products` (CASCADE), `name`,
  `sku` (unique), `price` NOT NULL, `compare_at_price`, `stock_quantity`, `image_url`,
  `is_active`, `sort_order`.

---

## 5. Multi-branch inventory (the global-to-local pattern)

This is the load-bearing idea of the whole platform: **one product, five branches,
each with its own override.**

### `branch_inventory`
| Column | Type | Notes |
|---|---|---|
| `inventory_id` (PK) | uuid | |
| `product_id` → `products` (CASCADE), `branch_id` → `branches` (CASCADE) | **UNIQUE (`product_id`, `branch_id`)** | one row per product per branch — this row's existence is what makes a product visible/purchasable at that branch at all |
| `branch_price` | numeric, nullable | `NULL` = inherit `products.price`. This "null means fall back to global" rule is the same for every override column below. |
| `stock_quantity` | integer | the real, sellable stock at this branch |
| `reserved_quantity` | integer NOT NULL, default 0 | units held by pending orders; `available = stock_quantity - reserved_quantity` |
| `low_stock_threshold` | integer NOT NULL, default 10 | branch-specific reorder point |
| `discount_percentage`, `is_on_sale` | | local override for the global discount/Quick Sale flag |
| `cashback_percentage` | double precision, nullable | local override for cashback rate |
| `is_active` | boolean | branch can hide a product even if globally active |

The "effective" values a customer actually sees (`effective_price`,
`effective_discount`, `effective_cashback`) are computed at read time by merging this
row over the global `products` row — see `ProductService.compute_effective_data()`.
Nothing precomputes or caches the merged value in the database.

---

## 6. Orders & fulfillment

### `orders`
The widest table in the schema — it accumulates payment, courier and
delivery-verification state directly on the row rather than in side tables.

**Core**: `order_id` PK · `user_id` → `users` (NO ACTION) · `order_number` (unique) ·
`branch_id` → `branches` (**SET NULL** — an order survives its branch being deleted) ·
`status` varchar(30) (`pending` → `confirmed` → `processing` → `shipped` →
`out_for_delivery` → `delivered`, or `cancelled` / `return_requested` /
`return_approved` / `refunded`) · `created_at`, `updated_at`, `shipped_at`,
`delivered_at`, `cancelled_at`.

**Pricing**: `subtotal`, `tax_amount`, `shipping_amount`, `discount_amount`,
`wallet_deduction`, `cashback_earned`, `total_amount` (NOT NULL) · `coupon_code`.

**Payment**: `payment_status`, `payment_id`, `payment_method` · Marx IPG gateway
fields — `gateway_tr_id`, `gateway_status`, `gateway_response_code`,
`gateway_confirmed_at`, `gateway_raw_response` (jsonb, the full raw callback payload)
· `cashback_credited_at`.

**Delivery verification**: `delivery_verification_code` (3 chars — an OTP-style code
given to the recipient at handoff), `delivery_verification_status`,
`delivery_verification_attempts`, `delivery_verification_confirmed_at`.

**Courier integration**: `courier_quote_id`, `courier_quote_expires_at`,
`courier_shipment_id`, `courier_waybill_number`, `courier_tracking_status`,
`courier_tracking_updated_at`, `courier_booking_status`.

**Delivery**: `delivery_address_id` → `addresses` (NO ACTION), `delivery_slot_date`,
`delivery_slot_time`, `notes`.

**Returns/refunds**: `return_reason`, `return_comments`, `return_items` (jsonb — which
line items, in what quantity), `return_requested_at`, `refund_amount`.

### `order_items`
`order_item_id` PK · `order_id` → `orders` (CASCADE) · `product_id` → `products` (NO
ACTION — deleting a product doesn't delete order history) · `variant_id` →
`product_variants` (NO ACTION) · `product_name`, `product_sku`, `product_image`
(**denormalized at order time** — so an order's line items still read correctly even
if the product is later renamed or deleted) · `quantity` NOT NULL · `unit_price`,
`subtotal` NOT NULL, `tax_amount`.

---

## 7. Wallet & cashback

Covered under §1 (`wallets` / `wallet_transactions` are customer-account tables) —
listed there since they hang off `users`, not `orders`, as their primary relationship.

---

## 8. Marketing: coupons & banners

### `coupons`
`coupon_id` PK · `code` (unique) NOT NULL · `description` · `discount_type` varchar(20)
(`percentage` | `fixed`) · `discount_value` NOT NULL · `min_order_value` NOT NULL ·
`usage_limit` (nullable = unlimited) · `used_count` NOT NULL · `valid_from`,
`valid_until` NOT NULL · `is_active`. **Not branch-scoped** — a coupon code applies
platform-wide by design.

### `banners`
Home-screen carousel entries. `banner_id` PK · `title` NOT NULL, `subtitle` ·
`image_url` · `link_type` (`category` | `product` | null), `target_id` (polymorphic —
which category/product it links to, no FK, resolved in code by `link_type`) ·
`branch_id` → `branches` (CASCADE), **nullable — `NULL` means platform-wide**, shown
in every branch and editable by Super Admin only; a non-null value scopes it to one
branch's carousel, editable by that branch's Marketing/Branch Manager too · `sort_order`
· `is_active`.

---

## 9. Reviews & wishlist

### `reviews`
`review_id` PK · `product_id` → `products` (CASCADE), `user_id` → `users` (CASCADE) ·
**UNIQUE (`product_id`, `user_id`)** — one review per customer per product ·
`rating` NOT NULL (1–5, enforced at the schema/service layer, not a DB CHECK) ·
`title`, `comment` · `is_verified_purchase` (set by cross-referencing `order_items`)
· `is_approved` (moderation flag — Customer Support/Branch Manager can update).

### `wishlist_items`
`wishlist_item_id` PK · `user_id` → `users` (CASCADE), `product_id` → `products`
(CASCADE), `variant_id` → `product_variants` (SET NULL) · **UNIQUE (`user_id`,
`product_id`, `variant_id`)** · `price_at_watch` — the price snapshotted when added ·
`added_at`.

> **No removal history.** A row is hard-deleted when a customer un-watches a product —
> there's no `removed_at` or audit table. This is why the admin "Watchlist Analytics"
> page's removals/trend/conversion-funnel widgets can't be made real without adding
> instrumentation first; see `.session-notes/QUESTIONS_FOR_USER.md` for the fuller
> writeup of that gap.

---

## 10. Referrals & partners

Two referral mechanisms share the `referral_commissions` ledger: a **customer**
referring another customer (`users.referred_by_user_id`), and a **partner** (a
professional referral-team member with their own login and portal) referring a
customer (`users.referred_by_partner_id`).

### `partners`
`partner_id` PK · `email` (implicitly unique via the auth flow, not a DB constraint) ·
`password_hash` · `full_name`, `phone` · `referral_code` varchar(12) · `parent_partner_id`
→ `partners.partner_id` (self-referential, **SET NULL**) — the multi-level tree: an
agent recruited by a lead partner points back at them · `is_active` · `last_login`.

### `referral_commissions`
The commission ledger paid out through the wallet system. `commission_id` PK ·
`order_id` → `orders` (CASCADE), `order_number` (denormalized) · `beneficiary_user_id`
→ `users` (CASCADE) **or** `beneficiary_partner_id` → `partners` (CASCADE) — whichever
type of referrer earns this row · `source_user_id` → `users` (CASCADE) — whose order
generated it · `level` smallint NOT NULL, default `1` — level-1 is the direct
referrer, level-2 is a partner's recruiter, etc. · `rate_percentage` NOT NULL ·
`order_subtotal`, `amount` NOT NULL · `status` varchar(20) NOT NULL, default
`'pending'` (`pending` → `claimed`) · `claimed_at` · `wallet_transaction_id` →
`wallet_transactions` (SET NULL) — links to the actual payout once claimed.

---

## 11. Branch-to-branch stock transfers

Added 2026-08-10 (migration 033) — previously the admin UI for this
(`StockTransfers.tsx`, `LowStockReport.tsx`'s "Request Transfer") existed with no
backend at all.

### `stock_transfers`
`transfer_id` PK · `from_branch_id`, `to_branch_id` → `branches` (both **RESTRICT** —
a branch with any transfer history can't be deleted out from under it), CHECK
`from_branch_id <> to_branch_id` · `product_id` → `products` (RESTRICT) · `quantity`
NOT NULL, CHECK `> 0` · `status` varchar(20) NOT NULL, default `'pending'`, CHECK IN
(`pending`, `approved`, `in_transit`, `completed`, `cancelled`) · `requested_by_id` →
`admins` (RESTRICT), `approved_by_id` → `admins` (SET NULL) · `notes` · `requested_at`,
`approved_at`, `shipped_at`, `completed_at`.

**Stock actually moves twice**, not once: `from_branch_id`'s `branch_inventory.
stock_quantity` is decremented on **approval** (committing the units so they can't
also be sold or transferred elsewhere while in transit); `to_branch_id`'s is
incremented on **completion** (arrival), creating the `branch_inventory` row via the
same upsert path as manually stocking a product if that branch didn't carry it yet.
Rejecting a still-`pending` request touches no stock.

---

## 12. Notifications

### `notifications`
`notification_id` PK · `user_id` → `users` (CASCADE) · `type` varchar(50) · `title`
NOT NULL, `message` NOT NULL · `reference_type`, `reference_id` — polymorphic pointer
at what this notification is about (an order, a review reply, etc.), no FK · `data`
jsonb — arbitrary extra payload for the client · `is_read`, `read_at` · `push_sent`,
`push_sent_at` — whether/when an FCM push was actually dispatched for this row.

### `push_tokens`
`token_id` PK · `user_id` → `users` (CASCADE) · `token` (unique) · `device_type`,
`device_name`, `device_id` · `is_active`.

---

## 13. App settings

### `app_settings`
A generic runtime key/value store — not branch-scoped, platform-wide config editable
from Settings → Platform / Settings → App in the admin dashboard. `setting_id` PK ·
`key` (unique) NOT NULL · `value` text · `description` · `is_active`. Known keys in use
include delivery fee, tax rate, minimum order value, splash video URL, support
phone/email, and a maintenance-mode flag — see `admin_settings.py` /
`app_settings_service.py` for the current enumerated set rather than trusting a static
list here, since new keys get added without a schema change.

---

## Full foreign-key map

Every FK in the live schema, with its delete behaviour (RESTRICT/CASCADE choices are
deliberate — CASCADE where the child row is meaningless without the parent, RESTRICT
where losing history silently would be worse than a blocked delete):

```
addresses.user_id                       → users.user_id                       (NO ACTION)
admin_sessions.admin_id                 → admins.admin_id                     (CASCADE)
banners.branch_id                       → branches.branch_id                  (CASCADE)
branch_inventory.branch_id              → branches.branch_id                  (CASCADE)
branch_inventory.product_id             → products.product_id                 (CASCADE)
branches.manager_id                     → admins.admin_id                     (SET NULL)
categories.parent_category_id           → categories.category_id              (NO ACTION)
notifications.user_id                   → users.user_id                       (CASCADE)
order_items.order_id                    → orders.order_id                     (CASCADE)
order_items.product_id                  → products.product_id                 (NO ACTION)
order_items.variant_id                  → product_variants.variant_id         (NO ACTION)
orders.branch_id                        → branches.branch_id                  (SET NULL)
orders.delivery_address_id              → addresses.address_id                (NO ACTION)
orders.user_id                          → users.user_id                       (NO ACTION)
partners.parent_partner_id              → partners.partner_id                 (SET NULL)
post_office_branch_mapping.branch_id    → branches.branch_id                  (CASCADE)
product_images.product_id               → products.product_id                 (CASCADE)
product_variants.product_id             → products.product_id                 (CASCADE)
products.category_id                    → categories.category_id              (NO ACTION)
products.subcategory_id                 → categories.category_id              (NO ACTION / SET NULL — two constraints exist, see note)
push_tokens.user_id                     → users.user_id                       (CASCADE)
referral_commissions.beneficiary_partner_id → partners.partner_id             (CASCADE)
referral_commissions.beneficiary_user_id → users.user_id                      (CASCADE)
referral_commissions.order_id           → orders.order_id                     (CASCADE)
referral_commissions.source_user_id     → users.user_id                       (CASCADE)
referral_commissions.wallet_transaction_id → wallet_transactions.transaction_id (SET NULL)
reviews.product_id                      → products.product_id                 (CASCADE)
reviews.user_id                         → users.user_id                       (CASCADE)
sessions.user_id                        → users.user_id                       (NO ACTION)
stock_transfers.approved_by_id          → admins.admin_id                     (SET NULL)
stock_transfers.from_branch_id          → branches.branch_id                  (RESTRICT)
stock_transfers.product_id              → products.product_id                 (RESTRICT)
stock_transfers.requested_by_id         → admins.admin_id                     (RESTRICT)
stock_transfers.to_branch_id            → branches.branch_id                  (RESTRICT)
users.referred_by_partner_id            → partners.partner_id                 (SET NULL)
users.referred_by_user_id               → users.user_id                       (SET NULL)
variant_options.variant_id              → product_variants.variant_id         (CASCADE)
variant_options.variant_type_id         → variant_types.variant_type_id       (NO ACTION)
wallet_transactions.order_id            → orders.order_id                     (SET NULL)
wallet_transactions.user_id             → users.user_id                       (CASCADE)
wallet_transactions.wallet_id           → wallets.wallet_id                   (CASCADE)
wallets.user_id                         → users.user_id                       (CASCADE)
wishlist_items.product_id               → products.product_id                 (CASCADE)
wishlist_items.user_id                  → users.user_id                       (CASCADE)
wishlist_items.variant_id               → product_variants.variant_id         (SET NULL)
```

*(`products.subcategory_id` genuinely has two FK constraints pointing at the same
column in the live database — an artifact of how the migration adding it was written;
harmless in practice since both target the same table/column, but worth a cleanup
migration at some point rather than treating it as intentional.)*

## Unique constraints (beyond primary keys)

```
branch_inventory (product_id, branch_id)
categories       (slug)
orders           (order_number)
product_variants (sku)
products         (sku)
products         (slug)
push_tokens      (token)
reviews          (product_id, user_id)
wishlist_items   (user_id, product_id, variant_id)
```

---

## Migration history

31 numbered files as of 2026-08-10 (`migrations/010_*.sql` through `033_*.sql` — there
is no `001`–`009`; the base schema comes entirely from `create_all()`, migrations only
begin once additive changes were needed on top of it). Grouped by what they actually
do:

| Range | What |
|---|---|
| 010 | Seeds the 5 branches (idempotent by `code`, not `branch_id` — see the file's own comment for why, it matters for §2's branch-scoping notes above) |
| 011–020 | Branch inventory stock fields, orders↔branch linkage, coupons, app_settings, push token device id, wallet tables, order returns, address contact fields, phone verification, Post Office directory, subcategories + branch pricing, category images |
| 021 | Seeds the global product catalog |
| 022–024 | Banners, product cashback fields, referrals |
| 025 | ~120 days of realistic demo order history across all 5 branches (fixed 2026-08-10 — previously only ever populated 2 of 5 branches, see the file's own header comment) |
| 026 | Full seed dataset: per-role/per-branch demo admins, branch-specific banners, app settings defaults |
| 027–028 | Partners table + seed partner tree |
| 029 | Fuzzy multilingual search support |
| 030 | Marx IPG payment fields + delivery verification code fields on `orders` |
| 031 | Courier integration fields on `orders` |
| 032 | Assigns real branches to the "generalist" demo admins that had been seeded with `branch_id = NULL` (2026-08-10) |
| 033 | `stock_transfers` table (2026-08-10) |

For exact column-level changes, read the migration file itself — they're short,
narrated, and are the actual source of truth; this table is just a map of what to go
looking for.
