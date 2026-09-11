-- =============================================================================
-- Migration 049: Coupon financial governance, targeting, and banner scheduling
--
-- Three groups of additive changes:
--
--   1. coupons.*            -- campaign budget cap + auto-stop, Quick Sale
--                              exclusion, first-order restriction, per-customer
--                              limit, public/private visibility, and 1-to-1
--                              targeting (the "Assign Promo" retention flow).
--   2. coupon_categories /  -- catalog eligibility. Absence of rows means the
--      coupon_products         coupon applies to the entire catalog, so every
--                              existing coupon keeps its current behaviour.
--   3. banners.starts_at /  -- scheduled banners. NULL on both = "always
--      banners.ends_at         active", which is what every existing row is.
--
-- Why a budget cap at all: usage_limit caps how MANY times a code is redeemed,
-- not how much it COSTS. A 10% coupon with a 2000-redemption limit on an
-- average Rs 4,000 basket is an Rs 800,000 liability nobody approved. The cap
-- below is denominated in money, which is the thing the branch actually has a
-- finite amount of.
--
-- SQLAlchemy create_all() adds all of this on a fresh DB. Idempotent -- safe
-- to re-run.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Coupon financial protection
-- -----------------------------------------------------------------------------

-- Total discount this campaign may ever give away, in LKR. NULL = uncapped,
-- which is the behaviour every coupon has today.
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS budget_cap NUMERIC(12, 2);

-- Running total of discount actually given. Denormalised on purpose, exactly
-- like used_count: checkout needs to test the cap under a row lock, and
-- re-aggregating orders on every redemption would be both slow and racy.
-- `orders.discount_amount` remains the reconcilable source of truth.
ALTER TABLE coupons
    ADD COLUMN IF NOT EXISTS budget_spent NUMERIC(12, 2) NOT NULL DEFAULT 0;

-- When true (the default), hitting the cap deactivates the coupon instead of
-- merely refusing the next order — so a manager is not left with a code that
-- silently fails for customers while still looking Active on the dashboard.
ALTER TABLE coupons
    ADD COLUMN IF NOT EXISTS auto_stop_on_budget BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN coupons.budget_cap IS
    'Maximum total discount (LKR) this campaign may give away. NULL = uncapped.';
COMMENT ON COLUMN coupons.budget_spent IS
    'Discount given so far. Reconcilable against SUM(orders.discount_amount) '
    'for this code over non-cancelled, non-refunded orders.';

-- -----------------------------------------------------------------------------
-- 2. Eligibility and targeting
-- -----------------------------------------------------------------------------

-- Clearance stock is already sold at or below cost. Stacking a coupon on top
-- is the "double discounting" that turns a promotion into a loss, so the
-- default for NEW coupons is to exclude it -- but existing rows keep FALSE so
-- nothing silently changes price mid-campaign.
ALTER TABLE coupons
    ADD COLUMN IF NOT EXISTS exclude_quick_sale BOOLEAN NOT NULL DEFAULT FALSE;

-- Acquisition coupons: redeemable only by a customer with no prior completed
-- order from this branch.
ALTER TABLE coupons
    ADD COLUMN IF NOT EXISTS first_order_only BOOLEAN NOT NULL DEFAULT FALSE;

-- Max redemptions by one customer. NULL = unlimited (today's behaviour).
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS per_user_limit INTEGER;

-- Listed in the mobile "Available Offers" feed. FALSE = a private code that
-- only works if the customer was given it.
ALTER TABLE coupons
    ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT TRUE;

-- 1-to-1 retention coupon issued to one named customer (the "Assign Promo"
-- flow on the customer profile). NULL = a normal campaign code.
ALTER TABLE coupons
    ADD COLUMN IF NOT EXISTS target_user_id UUID REFERENCES users(user_id) ON DELETE CASCADE;

COMMENT ON COLUMN coupons.target_user_id IS
    'Issued to exactly this customer; nobody else can redeem it. NULL = open '
    'campaign code. A targeted coupon is always private (is_public = FALSE).';

CREATE INDEX IF NOT EXISTS idx_coupons_target_user ON coupons (target_user_id)
    WHERE target_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_coupons_public_active ON coupons (is_public, is_active);

-- A targeted coupon that anyone could find in the public offers feed defeats
-- the point, so the two flags cannot disagree.
ALTER TABLE coupons DROP CONSTRAINT IF EXISTS ck_coupons_targeted_is_private;
ALTER TABLE coupons ADD CONSTRAINT ck_coupons_targeted_is_private
    CHECK (target_user_id IS NULL OR is_public = FALSE);

-- -----------------------------------------------------------------------------
-- 3. Catalog eligibility
-- -----------------------------------------------------------------------------
-- No rows for a coupon = the entire catalog is eligible. This is what makes
-- the feature additive: every coupon that exists today has no rows and so
-- keeps applying to everything, with no backfill.
CREATE TABLE IF NOT EXISTS coupon_categories (
    coupon_id   UUID NOT NULL REFERENCES coupons(coupon_id) ON DELETE CASCADE,
    category_id UUID NOT NULL REFERENCES categories(category_id) ON DELETE CASCADE,
    PRIMARY KEY (coupon_id, category_id)
);

CREATE TABLE IF NOT EXISTS coupon_products (
    coupon_id  UUID NOT NULL REFERENCES coupons(coupon_id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    PRIMARY KEY (coupon_id, product_id)
);

COMMENT ON TABLE coupon_categories IS
    'Categories a coupon is restricted to. No rows for a coupon = entire catalog.';
COMMENT ON TABLE coupon_products IS
    'Products a coupon is restricted to. No rows for a coupon = entire catalog.';

-- -----------------------------------------------------------------------------
-- 4. Banner scheduling
-- -----------------------------------------------------------------------------
-- Both NULL = always active, which is every banner that exists today.
ALTER TABLE banners ADD COLUMN IF NOT EXISTS starts_at TIMESTAMPTZ;
ALTER TABLE banners ADD COLUMN IF NOT EXISTS ends_at   TIMESTAMPTZ;

COMMENT ON COLUMN banners.starts_at IS
    'Banner appears from this moment. NULL = live as soon as is_active is set.';
COMMENT ON COLUMN banners.ends_at IS
    'Banner hides itself after this moment. NULL = no expiry. The storefront '
    'filters on it, so an expired banner disappears without anyone toggling it.';

-- The storefront reads "active banners for this branch, live right now".
CREATE INDEX IF NOT EXISTS idx_banners_schedule ON banners (is_active, starts_at, ends_at);

-- The existing link_type comment predates the Quick Sale destination; widen it
-- and enforce the accepted set so a typo cannot produce a banner that navigates
-- nowhere on the customer's phone.
COMMENT ON COLUMN banners.link_type IS
    'Tap destination: ''category'' | ''product'' | ''quick_sale'' | NULL (informational only).';

ALTER TABLE banners DROP CONSTRAINT IF EXISTS ck_banners_link_type;
ALTER TABLE banners ADD CONSTRAINT ck_banners_link_type
    CHECK (link_type IS NULL OR link_type IN ('category', 'product', 'quick_sale'));

-- category/product need something to point at; quick_sale is a fixed screen
-- and must not carry a stale target from a previous edit.
ALTER TABLE banners DROP CONSTRAINT IF EXISTS ck_banners_link_target;
ALTER TABLE banners ADD CONSTRAINT ck_banners_link_target
    CHECK (
        (link_type IN ('category', 'product') AND target_id IS NOT NULL)
        OR (link_type IS NULL OR link_type = 'quick_sale')
    );

-- -----------------------------------------------------------------------------
-- 5. Reporting support
-- -----------------------------------------------------------------------------
-- Budget-spend and revenue-generated columns aggregate orders by coupon code.
-- Without this index that is a sequential scan of the whole orders table on
-- every load of the Coupons page.
CREATE INDEX IF NOT EXISTS idx_orders_coupon_code ON orders (coupon_code)
    WHERE coupon_code IS NOT NULL;
