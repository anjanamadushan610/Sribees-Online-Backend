-- =============================================================================
-- Migration 048: Marketing Dashboard foundations
--
-- Four independent additions, all feeding the branch Marketing Dashboard:
--
--   1. coupons.branch_id      -- coupons become branch-ownable. NULL keeps the
--                                existing "network-wide" meaning, so every row
--                                that exists today is unchanged and still
--                                applies everywhere. Only a Super Admin may
--                                create/edit a NULL-branch coupon; a branch's
--                                Marketing Manager is pinned to their own.
--   2. branches.delivery_runs -- the branch's daily dispatch rounds (3 by
--                                default) as JSONB, so the dashboard's "Next
--                                Delivery Run" countdown has a real source
--                                instead of a hardcoded 2:00 PM.
--   3. products.is_perishable -- flags short-shelf-life stock, which is what
--                                Quick Sale exists to clear.
--   4. banner_events          -- impression/click log behind the real banner
--                                CTR (there was no counter of any kind before,
--                                so the old dashboard's CTR was invented).
--
-- SQLAlchemy create_all() adds all of this on a fresh DB. Idempotent -- safe
-- to re-run.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Branch-scoped coupons
-- -----------------------------------------------------------------------------
ALTER TABLE coupons
    ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES branches(branch_id) ON DELETE CASCADE;

COMMENT ON COLUMN coupons.branch_id IS
    'Owning branch. NULL = network-wide (Super Admin only). A branch coupon is '
    'only redeemable on orders fulfilled by that branch.';

-- Redemption checks filter on (code, branch) — index the branch side.
CREATE INDEX IF NOT EXISTS idx_coupons_branch_id ON coupons (branch_id);

-- -----------------------------------------------------------------------------
-- 2. Per-branch daily delivery runs
-- -----------------------------------------------------------------------------
-- Shape: [{"label": "Morning", "dispatch_time": "10:00", "cutoff_minutes": 30}, ...]
--   dispatch_time  -- 24h "HH:MM" in Asia/Colombo, when the van leaves
--   cutoff_minutes -- how many minutes before dispatch ordering closes
ALTER TABLE branches
    ADD COLUMN IF NOT EXISTS delivery_runs JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN branches.delivery_runs IS
    'Daily dispatch rounds for this branch, ordered by dispatch_time. Each entry '
    '{label, dispatch_time "HH:MM" Asia/Colombo, cutoff_minutes}. Empty = branch '
    'has not configured its runs yet.';

-- Seed the standard three rounds onto every branch that has none yet, so the
-- dashboard is meaningful from the first deploy. A branch that has already
-- configured its own runs is left alone.
UPDATE branches
SET delivery_runs = '[
    {"label": "Morning",   "dispatch_time": "10:00", "cutoff_minutes": 30},
    {"label": "Afternoon", "dispatch_time": "14:00", "cutoff_minutes": 30},
    {"label": "Evening",   "dispatch_time": "18:00", "cutoff_minutes": 30}
]'::jsonb
WHERE delivery_runs IS NULL OR jsonb_array_length(delivery_runs) = 0;

-- -----------------------------------------------------------------------------
-- 3. Perishable products
-- -----------------------------------------------------------------------------
ALTER TABLE products
    ADD COLUMN IF NOT EXISTS is_perishable BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN products.is_perishable IS
    'Short shelf life — the stock Quick Sale exists to clear before it spoils.';

-- The dashboard counts perishable rows inside the branch's live Quick Sale set.
CREATE INDEX IF NOT EXISTS idx_products_is_perishable
    ON products (is_perishable) WHERE is_perishable;

-- -----------------------------------------------------------------------------
-- 4. Banner impression / click tracking
-- -----------------------------------------------------------------------------
-- One row per event rather than a counter column: a counter cannot answer
-- "CTR over the last 7 days", which is the only form of the number a Marketing
-- Manager can act on. Rows are pruned by the retention job, not kept forever.
CREATE TABLE IF NOT EXISTS banner_events (
    event_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    banner_id   UUID NOT NULL REFERENCES banners(banner_id) ON DELETE CASCADE,
    -- 'impression' = the banner was rendered on screen; 'click' = it was tapped.
    event_type  VARCHAR(20) NOT NULL,
    -- The branch whose storefront showed it. A platform-wide banner therefore
    -- reports per-branch CTR, which is what the branch's manager needs.
    branch_id   UUID REFERENCES branches(branch_id) ON DELETE SET NULL,
    -- NULL for a guest; never joined for display, only for de-duplication.
    user_id     UUID REFERENCES users(user_id) ON DELETE SET NULL,
    -- Per-device dedupe key so one screen view is not counted many times.
    session_key VARCHAR(64),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_banner_events_type CHECK (event_type IN ('impression', 'click'))
);

-- The dashboard's only query shape: events for these banners, in this branch,
-- since a cutoff.
CREATE INDEX IF NOT EXISTS idx_banner_events_banner_created
    ON banner_events (banner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_banner_events_branch_created
    ON banner_events (branch_id, created_at DESC);

-- One impression per banner per session per hour: without this, a customer
-- scrolling the home screen up and down inflates impressions and craters CTR.
CREATE UNIQUE INDEX IF NOT EXISTS uq_banner_events_impression_dedupe
    ON banner_events (banner_id, session_key, date_trunc('hour', created_at AT TIME ZONE 'UTC'))
    WHERE event_type = 'impression' AND session_key IS NOT NULL;
