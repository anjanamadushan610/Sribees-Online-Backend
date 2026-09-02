-- =============================================================================
-- Migration 045: grant the Customer Support system role `categories:read` (B5)
--
-- Support agents get a view-only Categories screen for catalog reference while
-- assisting customers. Runtime authority is the role_permissions table (seeded
-- by 037); this wires the extra grant onto the `customer_support` system role.
--
-- Mirrors app/core/permission_catalog.py SYSTEM_ROLE_PERMISSIONS["customer_support"].
-- Idempotent (ON CONFLICT DO NOTHING) -- safe to re-run.
-- =============================================================================

-- The pair must exist in `permissions` (037 seeds categories:read already, but
-- be defensive on a partially-migrated DB).
INSERT INTO permissions (resource, action)
VALUES ('categories', 'read')
ON CONFLICT (resource, action) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM roles r
JOIN permissions p ON (p.resource, p.action) = ('categories', 'read')
WHERE r.system_key = 'customer_support'
ON CONFLICT DO NOTHING;
