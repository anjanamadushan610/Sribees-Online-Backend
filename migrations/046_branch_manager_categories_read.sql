-- =============================================================================
-- Migration 046: grant the Branch Manager system role `categories:read`
--
-- Branch Managers need view-only visibility over Categories enabled for their
-- branch (configured under Settings -> My Branch -> Category Visibility).
-- Runtime authority is the role_permissions table (seeded by 037); this wires
-- the extra grant onto the `branch_manager` system role.
--
-- Mirrors app/core/permission_catalog.py SYSTEM_ROLE_PERMISSIONS["branch_manager"].
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
WHERE r.system_key = 'branch_manager'
ON CONFLICT DO NOTHING;
