-- =============================================================================
-- 010_seed_branches.sql — The branch network.
--
-- Numbered 010 on purpose: it has to land BEFORE 021_seed_catalog.sql, which
-- stocks products with `CROSS JOIN branches`. Run 021 against an empty branches
-- table and the cross join produces zero rows — the catalog exists but nothing
-- is visible to shoppers and every per-branch report is empty. Seeding the
-- branches first is what makes the rest of the seed data mean anything.
--
-- The `branches` table itself is created by SQLAlchemy create_all() when the
-- app boots, which the deploy runs before any migration (see step 7b).
--
-- Runs on EVERY deploy, so it is idempotent: ON CONFLICT (code) keeps re-runs
-- as refreshes, never duplicates.
--
-- Conflict target is `code`, not `branch_id`. Production already had branches
-- created through the admin UI (app-generated UUIDs) before this migration
-- existed, so the fixed UUIDs below only apply to brand-new codes — an
-- existing row is matched by its `code` and refreshed in place. `branch_id`
-- is intentionally never written by DO UPDATE: BranchInventory, orders, and
-- banners all carry a live FK to it, and CASCADE deletes on branches.branch_id
-- would take those rows with it if this migration ever tried to renumber it.
--
-- Five branches across five provinces, which is the point: a single branch
-- cannot demonstrate branch isolation, and a Super Admin comparing branch
-- performance needs enough rows for the comparison to say something.
--
-- DO UPDATE on name/district/province: structural facts about the network,
-- safe to keep in sync. `is_active` is deliberately NOT overwritten — a branch
-- an admin deactivated must stay deactivated across the next deploy.
-- =============================================================================

INSERT INTO branches (branch_id, name, code, address, post_office, district, province, phone, is_active) VALUES
    ('b0000000-0000-4000-a000-000000000001', 'Colombo Main',   'CMB',
     'No. 142, Galle Road, Colombo 03',   'Colombo 03', 'Colombo',    'Western',       '+94112345601', TRUE),
    ('b0000000-0000-4000-a000-000000000002', 'Kandy City',     'KDY',
     'No. 27, Peradeniya Road, Kandy',    'Kandy',      'Kandy',      'Central',       '+94812345602', TRUE),
    ('b0000000-0000-4000-a000-000000000003', 'Galle Fort',     'GLE',
     'No. 8, Church Street, Galle',       'Galle',      'Galle',      'Southern',      '+94912345603', TRUE),
    ('b0000000-0000-4000-a000-000000000004', 'Negombo Beach',  'NGB',
     'No. 55, Lewis Place, Negombo',      'Negombo',    'Gampaha',    'Western',       '+94312345604', TRUE),
    ('b0000000-0000-4000-a000-000000000005', 'Kurunegala Hub', 'KUR',
     'No. 19, Colombo Road, Kurunegala',  'Kurunegala', 'Kurunegala', 'North Western', '+94372345605', TRUE)
ON CONFLICT (code) DO UPDATE SET
    name        = EXCLUDED.name,
    address     = EXCLUDED.address,
    post_office = EXCLUDED.post_office,
    district    = EXCLUDED.district,
    province    = EXCLUDED.province,
    phone       = EXCLUDED.phone,
    updated_at  = NOW();
