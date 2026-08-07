-- =============================================================================
-- 022_banners.sql — Home-screen promotional banners, curated per branch.
--
-- Replaces the three slides that were hardcoded in the Flutter app, so branch
-- managers can run their own promotions without an app release.
--
-- branch_id NULL = platform-wide (Super Admin), shown in every branch.
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

CREATE TABLE IF NOT EXISTS banners (
    banner_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title       VARCHAR(120) NOT NULL,
    subtitle    VARCHAR(200),
    image_url   VARCHAR(500),
    link_type   VARCHAR(20),
    target_id   UUID,
    branch_id   UUID REFERENCES branches(branch_id) ON DELETE CASCADE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- The storefront's only read pattern: active banners for one branch (plus the
-- platform-wide ones), in display order.
CREATE INDEX IF NOT EXISTS idx_banners_branch_active_order
    ON banners (branch_id, is_active, sort_order);

-- -----------------------------------------------------------------------------
-- Seed the three slides the app used to ship hardcoded, as platform-wide rows,
-- so the carousel is populated the moment this lands rather than empty until
-- someone opens the admin panel.
--
-- DO NOTHING on conflict: once these exist an admin owns them, and a redeploy
-- must not resurrect a banner they deliberately edited or deactivated.
-- -----------------------------------------------------------------------------
INSERT INTO banners (banner_id, title, subtitle, branch_id, sort_order, is_active) VALUES
    ('b0000000-0000-4000-a000-000000000001', 'Fresh Farm Produce',
     'Delivered straight to you.', NULL, 1, TRUE),
    ('b0000000-0000-4000-a000-000000000002', '20% Off Greens',
     'This weekend only.', NULL, 2, TRUE),
    ('b0000000-0000-4000-a000-000000000003', 'Earn 10% Cash Back',
     'On every single order.', NULL, 3, TRUE)
ON CONFLICT (banner_id) DO NOTHING;
