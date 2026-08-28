-- =============================================================================
-- Migration 043: Branch Category Visibility
--
-- Categories are a global taxonomy, but whether a branch's customers see one
-- is now per-branch, mirroring branch_inventory for products: a category
-- created from now on starts hidden in every branch until that branch's own
-- manager turns it on, and a branch created from now on starts with every
-- category hidden until its manager turns each one on.
--
-- Backfill: every EXISTING (category, branch) pair is seeded ACTIVE, so
-- today's live storefront does not go dark retroactively — only pairs
-- created after this migration (new category or new branch) default to
-- hidden, via the application layer (see admin_catalog.create_category /
-- admin_branches.create_branch).
-- =============================================================================

CREATE TABLE IF NOT EXISTS branch_categories (
    branch_category_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category_id         UUID NOT NULL REFERENCES categories(category_id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(branch_id) ON DELETE CASCADE,
    is_active            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_branch_category_category_branch UNIQUE (category_id, branch_id)
);

CREATE INDEX IF NOT EXISTS idx_branch_category_branch_active ON branch_categories(branch_id, is_active);
CREATE INDEX IF NOT EXISTS idx_branch_category_category ON branch_categories(category_id);

-- Backfill every existing (category, branch) pair as ACTIVE — preserves
-- today's live storefront. New rows created going forward default to FALSE.
INSERT INTO branch_categories (category_id, branch_id, is_active)
SELECT c.category_id, b.branch_id, TRUE
FROM categories c
CROSS JOIN branches b
ON CONFLICT (category_id, branch_id) DO NOTHING;
