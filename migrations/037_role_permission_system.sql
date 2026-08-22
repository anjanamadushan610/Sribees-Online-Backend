-- =============================================================================
-- Migration 037: Dynamic roles/permissions + delegated staff accounts
--
-- Lets any base-role admin (super_admin, branch_manager, marketing_manager,
-- inventory_manager, customer_support) create a "staff" account beneath
-- themselves, holding a custom-named role scoped to a subset of the
-- creator's own permissions. Hierarchy is capped at one level: a staff
-- account can never create further staff (enforced in application code via
-- require_base_role_admin, not by this schema).
--
-- Additive: admins.role (admin_role_enum) is untouched for the 5 existing
-- values — every existing require_roles()/RequireSuperAdmin gate keeps
-- working unchanged for base-role admins. One new enum value, 'staff', is
-- appended for delegated staff accounts; their custom role NAME and
-- permission set live in the new roles/permissions/role_permissions tables,
-- referenced by admins.role_id.
--
-- 'users', 'branches', 'partners', 'settings' are deliberately absent from
-- the seeded permission catalog below — those stay Super-Admin-exclusive
-- powers that can never be delegated to a staff role, no matter what the
-- creator holds (see app/core/permission_catalog.py EXCLUDED_RESOURCES).
--
-- Split into separate statements rather than one BEGIN/COMMIT block: step 1
-- adds a new enum value, and Postgres forbids using a brand-new enum value
-- inside the same transaction that added it. Every statement is idempotent
-- (IF NOT EXISTS / ON CONFLICT DO NOTHING), so this is safe to re-run.
--
-- Schema (roles/permissions/role_permissions tables, admins.role_id,
-- admins.parent_admin_id) is also created by SQLAlchemy
-- Base.metadata.create_all() on fresh DBs; run this against pre-existing
-- databases.
-- =============================================================================

-- 1. New enum value (own statement — cannot be used until this commits).
--    NOTE: admin_role_enum's Postgres labels are the Python enum MEMBER
--    NAMES ('SUPER_ADMIN', 'BRANCH_MANAGER', ...), not their lowercase
--    .value strings — that's how SQLAlchemy's Enum type persists a
--    (str, Enum) subclass by default. AdminRole.STAFF's name is 'STAFF', so
--    the new label must be 'STAFF' (uppercase) to match, or every row
--    written via the ORM as AdminRole.STAFF would fail to match this value.
ALTER TYPE admin_role_enum ADD VALUE IF NOT EXISTS 'STAFF';

-- 2. New tables (no-ops if SQLAlchemy create_all already ran)
CREATE TABLE IF NOT EXISTS roles (
    role_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(100) NOT NULL,
    is_system           BOOLEAN NOT NULL DEFAULT FALSE,
    system_key          VARCHAR(30) UNIQUE,
    created_by_admin_id UUID REFERENCES admins(admin_id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS permissions (
    permission_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resource      VARCHAR(50) NOT NULL,
    action        VARCHAR(20) NOT NULL,
    CONSTRAINT uq_permissions_resource_action UNIQUE (resource, action)
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_id       UUID NOT NULL REFERENCES roles(role_id) ON DELETE CASCADE,
    permission_id UUID NOT NULL REFERENCES permissions(permission_id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_id)
);

-- Per-creator name uniqueness (two different creators may each name a role
-- "Support Staff"); system role names stay globally unique via system_key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_roles_creator_name
    ON roles (created_by_admin_id, LOWER(name)) WHERE created_by_admin_id IS NOT NULL;

-- 3. admins alterations
ALTER TABLE admins ADD COLUMN IF NOT EXISTS role_id UUID REFERENCES roles(role_id) ON DELETE SET NULL;
ALTER TABLE admins ADD COLUMN IF NOT EXISTS parent_admin_id UUID REFERENCES admins(admin_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_admins_role_id ON admins (role_id);
CREATE INDEX IF NOT EXISTS idx_admins_parent_admin_id ON admins (parent_admin_id);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_admin_no_self_parent'
    ) THEN
        ALTER TABLE admins ADD CONSTRAINT chk_admin_no_self_parent
            CHECK (parent_admin_id IS NULL OR parent_admin_id <> admin_id);
    END IF;
END $$;

-- 4. Seed the 5 system roles (idempotent upsert on system_key)
INSERT INTO roles (name, is_system, system_key)
VALUES
    ('Super Admin', TRUE, 'super_admin'),
    ('Branch Manager', TRUE, 'branch_manager'),
    ('Marketing Manager', TRUE, 'marketing_manager'),
    ('Inventory Manager', TRUE, 'inventory_manager'),
    ('Customer Support', TRUE, 'customer_support')
ON CONFLICT (system_key) DO NOTHING;

-- 5. Seed the delegatable permission catalog — every (resource, action) pair
--    a role can ever be granted. Mirrors
--    app/core/permission_catalog.py::DELEGATABLE_PERMISSIONS exactly.
--    'users'/'branches'/'partners'/'settings' are deliberately absent —
--    those stay Super-Admin-exclusive, never delegatable to staff.
INSERT INTO permissions (resource, action) VALUES
    ('dashboard','read'),
    ('products','create'), ('products','read'), ('products','update'), ('products','delete'),
    ('orders','read'), ('orders','update'),
    ('customers','read'), ('customers','update'),
    ('inventory','read'), ('inventory','update'),
    ('analytics','read'),
    ('watchlist','read'),
    ('reviews','read'), ('reviews','update'),
    ('categories','create'), ('categories','read'), ('categories','update'),
    ('transfers','create'), ('transfers','read'), ('transfers','update'),
    ('marketing','create'), ('marketing','read'), ('marketing','update'), ('marketing','delete'),
    ('banners','create'), ('banners','read'), ('banners','update'), ('banners','delete')
ON CONFLICT (resource, action) DO NOTHING;

-- 6. Wire every catalog permission to Super Admin (its "owns everything"
--    semantics — the frontend's {resource:'*', action:'*'} wildcard expanded
--    into concrete rows).
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.role_id, p.permission_id FROM roles r CROSS JOIN permissions p
WHERE r.system_key = 'super_admin'
ON CONFLICT DO NOTHING;

-- 7. Wire the other 4 system roles' current permission sets — ported 1:1
--    from admin/src/types/admin.types.ts ROLE_PERMISSIONS (wildcards
--    expanded), minus the excluded resources from step 5.
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.role_id, p.permission_id FROM roles r JOIN permissions p ON
    (r.system_key = 'branch_manager' AND (p.resource, p.action) IN
        (('dashboard','read'),('products','read'),('products','update'),('orders','read'),
         ('orders','update'),('customers','read'),('inventory','read'),('inventory','update'),
         ('analytics','read'),('watchlist','read'),('reviews','read'),('reviews','update'),
         ('transfers','create'),('transfers','read')))
    OR (r.system_key = 'marketing_manager' AND (p.resource, p.action) IN
        (('dashboard','read'),('products','read'),('customers','read'),('watchlist','read'),
         ('marketing','create'),('marketing','read'),('marketing','update'),('marketing','delete'),
         ('banners','create'),('banners','read'),('banners','update'),('banners','delete')))
    OR (r.system_key = 'customer_support' AND (p.resource, p.action) IN
        (('dashboard','read'),('products','read'),('orders','read'),('orders','update'),
         ('customers','read'),('customers','update'),('reviews','read'),('reviews','update')))
    OR (r.system_key = 'inventory_manager' AND (p.resource, p.action) IN
        (('dashboard','read'),('products','create'),('products','read'),('products','update'),
         ('inventory','read'),('inventory','update'),('categories','create'),('categories','read'),
         ('categories','update'),('transfers','create'),('transfers','read'),('transfers','update')))
WHERE r.system_key IN ('branch_manager', 'marketing_manager', 'customer_support', 'inventory_manager')
ON CONFLICT DO NOTHING;

-- 8. Backfill role_id for every existing admin from its enum role.
--    a.role::text yields the enum's stored NAME ('SUPER_ADMIN'), while
--    roles.system_key was seeded lowercase (matching AdminRole.*.value,
--    which is what application code compares system_key against) — hence
--    LOWER() here rather than a direct match.
UPDATE admins a SET role_id = r.role_id
FROM roles r WHERE r.system_key = LOWER(a.role::text) AND a.role_id IS NULL;

-- 9. Enforce NOT NULL now that every row (system-seeded + backfilled) has one
ALTER TABLE admins ALTER COLUMN role_id SET NOT NULL;
