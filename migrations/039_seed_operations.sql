-- =============================================================================
-- 039_seed_operations.sql — Stock transfers and audit history.
--
-- The last two tables left empty by 026. Both live here rather than there
-- because neither exists until later in the sequence: stock_transfers is
-- created by 033, audit_logs by 038, and migrations run in filename order.
--
-- Runs on EVERY deploy, so it is idempotent: ids are md5-derived from stable
-- natural keys and every insert is guarded, so a re-run adds nothing and a real
-- transfer or a genuine audit entry is never touched.
--
-- Development / QA database only.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Demo seed: skipped on a database marked
--     ALTER DATABASE <db> SET sribees.demo_seed = 'off';
-- which production is. Deploys replay every migration, so without this the
-- seed re-creates demo customers, orders, admins and catalog rows every time
-- they are cleared. Unmarked databases (dev, QA, a fresh local one) still seed.
-- ---------------------------------------------------------------------------
SELECT coalesce(nullif(current_setting('sribees.demo_seed', true), ''), 'on') <> 'off' AS demo_seed \gset
\if :demo_seed
\else
\echo 'Skipped: sribees.demo_seed is off for this database.'
\quit
\endif

BEGIN;

CREATE OR REPLACE FUNCTION pg_temp.demo_hash(t text) RETURNS int
LANGUAGE sql IMMUTABLE STRICT AS
$$ SELECT ('x' || substr(md5(t), 1, 7))::bit(28)::int $$;

-- -----------------------------------------------------------------------------
-- 1. Stock transfers between branches
--
-- One at every stage of the lifecycle, not five pending ones. A transfers
-- screen with a single status on it cannot be judged: the status filter, the
-- approve/ship/complete actions and the timestamp columns all look correct
-- against a list that never varies.
--
-- The timestamps are filled in to match the status — an approved transfer has
-- an approved_at and nothing after it — because a row claiming to be completed
-- with no completed_at is the kind of inconsistency that sends someone hunting
-- for a bug in code that is fine.
-- -----------------------------------------------------------------------------
INSERT INTO stock_transfers (
    transfer_id, from_branch_id, to_branch_id, product_id, quantity, status,
    requested_by_id, approved_by_id, notes,
    requested_at, approved_at, shipped_at, completed_at
)
SELECT
    md5('sribees:demo-transfer:' || v.from_code || ':' || v.to_code || ':' || p.sku)::uuid,
    src.branch_id,
    dst.branch_id,
    p.product_id,
    v.quantity,
    v.status,
    requester.admin_id,
    CASE WHEN v.status <> 'pending' THEN approver.admin_id END,
    v.notes,
    NOW() - (v.age_days || ' days')::interval,
    CASE WHEN v.status IN ('approved', 'in_transit', 'completed')
         THEN NOW() - ((v.age_days - 1) || ' days')::interval END,
    CASE WHEN v.status IN ('in_transit', 'completed')
         THEN NOW() - ((v.age_days - 2) || ' days')::interval END,
    CASE WHEN v.status = 'completed'
         THEN NOW() - ((v.age_days - 3) || ' days')::interval END
FROM (VALUES
    ('CMB', 'KDY', 'FP-BAN-001', 40, 'pending',    9, 'Kandy is short on bananas this week.'),
    ('CMB', 'GLE', 'DE-MLK-001', 25, 'approved',   8, 'Covering a delayed dairy delivery.'),
    ('KDY', 'NGB', 'PS-RIC-001', 60, 'in_transit', 7, 'Rice moved ahead of the long weekend.'),
    ('GLE', 'CMB', 'FP-TOM-002', 30, 'completed',  6, 'Surplus tomatoes returned to the main branch.'),
    ('NGB', 'KUR', 'DE-EGG-003', 20, 'cancelled',  5, 'Cancelled — Kurunegala restocked locally.')
) AS v(from_code, to_code, sku, quantity, status, age_days, notes)
JOIN branches src ON src.code = v.from_code
JOIN branches dst ON dst.code = v.to_code
JOIN products p   ON p.sku    = v.sku
-- The requester is the sending branch's own manager, and the approver is the
-- Super Admin. Any other pairing would describe a workflow the app does not
-- actually have.
JOIN admins requester ON requester.branch_id = src.branch_id
                     AND requester.role = 'BRANCH_MANAGER'
CROSS JOIN (
    SELECT admin_id FROM admins WHERE role = 'SUPER_ADMIN' ORDER BY email LIMIT 1
) approver
WHERE NOT EXISTS (
    SELECT 1 FROM stock_transfers st
    WHERE st.transfer_id =
        md5('sribees:demo-transfer:' || v.from_code || ':' || v.to_code || ':' || p.sku)::uuid
);

-- -----------------------------------------------------------------------------
-- 2. Audit history
--
-- audit_logs fills itself once people start using the system, so seeding it is
-- only about the first day: an audit screen with nothing in it cannot be told
-- apart from an audit screen whose query is broken.
--
-- Deliberately mixed severities. A log of nothing but successful logins proves
-- the list renders; it says nothing about whether the severity filter, the
-- failed-login view, or the actor columns work.
-- -----------------------------------------------------------------------------
INSERT INTO audit_logs (
    id, created_at, action, severity, actor_type, actor_id, actor_email,
    actor_ip, actor_user_agent, target_type, target_id, description,
    metadata, endpoint, method
)
SELECT
    md5('sribees:demo-audit:' || a.email || ':' || v.action)::uuid,
    NOW() - (v.age_hours || ' hours')::interval,
    v.action,
    v.severity,
    'admin',
    a.admin_id,
    a.email,
    '203.0.113.' || (pg_temp.demo_hash(a.email) % 250 + 1),
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    v.target_type,
    NULL,
    replace(v.description, '{name}', a.full_name),
    jsonb_build_object('seeded', TRUE, 'role', a.role::text),
    v.endpoint,
    v.method
FROM admins a
JOIN (VALUES
    ('admin.login',             'info',     2,  'admin',   '/api/v1/admin/auth/login',    'POST',
     '{name} signed in to the admin panel.'),
    ('admin.login_failed',      'warning',  26, 'admin',   '/api/v1/admin/auth/login',    'POST',
     'Failed sign-in attempt for {name} — wrong password.'),
    ('product.update',          'info',     30, 'product', '/api/v1/admin/products',      'PUT',
     '{name} updated a product.'),
    ('order.status_change',     'info',     34, 'order',   '/api/v1/admin/orders',        'PATCH',
     '{name} moved an order to the next fulfilment stage.'),
    ('admin.permission_change', 'critical', 48, 'admin',   '/api/v1/admin/users',         'PUT',
     '{name} changed an admin account''s permissions.')
) AS v(action, severity, age_hours, target_type, endpoint, method, description)
    -- Only the Super Admin gets the permission-change entry: it is the one
    -- action nobody else can perform, and spreading it across every account
    -- would misrepresent who is allowed to do what.
    ON (v.action <> 'admin.permission_change' OR a.role = 'SUPER_ADMIN')
WHERE a.email LIKE '%@sribeesonline.lk'
  AND NOT EXISTS (
    SELECT 1 FROM audit_logs al
    WHERE al.id = md5('sribees:demo-audit:' || a.email || ':' || v.action)::uuid
  );

COMMIT;
