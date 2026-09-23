-- =============================================================================
-- Migration 057: Customer Support Tickets
--
-- Adds support_tickets table for issues/inquiries submitted by customers via
-- the mobile app's Help & Support screen.
-- Supports role-based branch scoping (Branch Manager sees their branch,
-- Customer Support sees their branch, Super Admin sees all branches).
-- =============================================================================

CREATE TABLE IF NOT EXISTS support_tickets (
    ticket_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_number      VARCHAR(30) UNIQUE NOT NULL,
    user_id            UUID REFERENCES users(user_id) ON DELETE SET NULL,
    branch_id          UUID REFERENCES branches(branch_id) ON DELETE SET NULL,
    contact_phone      VARCHAR(30) NOT NULL,
    customer_name      VARCHAR(255),
    category           VARCHAR(50) NOT NULL DEFAULT 'general',
    message            TEXT NOT NULL,
    status             VARCHAR(30) NOT NULL DEFAULT 'pending',
    assigned_admin_id  UUID REFERENCES admins(admin_id) ON DELETE SET NULL,
    resolution_notes   TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at        TIMESTAMPTZ
);

ALTER TABLE support_tickets ALTER COLUMN ticket_id SET DEFAULT gen_random_uuid();

CREATE INDEX IF NOT EXISTS idx_support_tickets_branch  ON support_tickets (branch_id);
CREATE INDEX IF NOT EXISTS idx_support_tickets_status  ON support_tickets (status);
CREATE INDEX IF NOT EXISTS idx_support_tickets_created ON support_tickets (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_support_tickets_phone   ON support_tickets (contact_phone);

-- Seed support permissions
INSERT INTO permissions (resource, action)
VALUES 
    ('support', 'read'),
    ('support', 'update')
ON CONFLICT (resource, action) DO NOTHING;

-- Grant support permissions to super_admin, branch_manager, and customer_support system roles
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM roles r
CROSS JOIN permissions p
WHERE r.system_key IN ('super_admin', 'branch_manager', 'customer_support')
  AND p.resource = 'support'
ON CONFLICT DO NOTHING;
