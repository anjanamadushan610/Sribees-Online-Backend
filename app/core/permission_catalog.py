"""
Permission Catalog — Python mirror of admin/src/types/admin.types.ts ROLE_PERMISSIONS.

Two independent copies exist by design (frontend display/authoring, backend
seed data + defensive fallback) and nothing enforces they stay in sync except
developer discipline — if you change one, change the other.

`users`, `branches`, `partners`, `settings` are deliberately absent from
DELEGATABLE_PERMISSIONS: those stay Super-Admin-exclusive powers that can
never be handed to a delegated staff role, no matter what the creator holds.
"""

# The full catalog of (resource, action) pairs that can ever be delegated to
# a staff role. Seeded into the `permissions` table by migration 037.
DELEGATABLE_PERMISSIONS: set[tuple[str, str]] = {
    ("dashboard", "read"),
    ("products", "create"), ("products", "read"), ("products", "update"), ("products", "delete"),
    ("orders", "read"), ("orders", "update"),
    ("customers", "read"), ("customers", "update"),
    ("inventory", "read"), ("inventory", "update"),
    ("analytics", "read"),
    ("watchlist", "read"),
    ("reviews", "read"), ("reviews", "update"),
    ("categories", "create"), ("categories", "read"), ("categories", "update"),
    ("transfers", "create"), ("transfers", "read"), ("transfers", "update"),
    ("marketing", "create"), ("marketing", "read"), ("marketing", "update"), ("marketing", "delete"),
    ("banners", "create"), ("banners", "read"), ("banners", "update"), ("banners", "delete"),
    ("support", "read"), ("support", "update"),
    # What a branch tells the AI assistant for a while (migration 072).
    ("briefings", "create"), ("briefings", "read"), ("briefings", "update"), ("briefings", "delete"),
}

# Resources that are never delegatable regardless of what a creator holds —
# excluded from DELEGATABLE_PERMISSIONS entirely, not just filtered at the UI.
EXCLUDED_RESOURCES: set[str] = {"users", "branches", "partners", "settings"}

# Effective permission set per system role — ported 1:1 from
# admin/src/types/admin.types.ts ROLE_PERMISSIONS (wildcards expanded).
# Used only as a defensive fallback in PermissionService when an admin's
# role_id is unexpectedly unset; the DB (role_permissions table, seeded by
# migration 037) is the source of truth at runtime.
SYSTEM_ROLE_PERMISSIONS: dict[str, set[tuple[str, str]]] = {
    "super_admin": set(DELEGATABLE_PERMISSIONS),
    "branch_manager": {
        ("dashboard", "read"),
        ("products", "read"), ("products", "update"),
        ("categories", "read"),
        ("orders", "read"), ("orders", "update"),
        ("customers", "read"),
        ("inventory", "read"), ("inventory", "update"),
        ("analytics", "read"),
        ("watchlist", "read"),
        ("reviews", "read"), ("reviews", "update"),
        ("transfers", "create"), ("transfers", "read"),
        ("support", "read"), ("support", "update"),
        ("briefings", "create"), ("briefings", "read"), ("briefings", "update"), ("briefings", "delete"),
    },
    "marketing_manager": {
        ("dashboard", "read"),
        ("products", "read"),
        ("customers", "read"),
        ("watchlist", "read"),
        ("marketing", "create"), ("marketing", "read"), ("marketing", "update"), ("marketing", "delete"),
        ("banners", "create"), ("banners", "read"), ("banners", "update"), ("banners", "delete"),
    },
    "customer_support": {
        ("dashboard", "read"),
        ("products", "read"),
        ("categories", "read"),  # view-only catalog reference while assisting customers (B5)
        ("orders", "read"), ("orders", "update"),
        ("customers", "read"), ("customers", "update"),
        ("reviews", "read"), ("reviews", "update"),
        ("support", "read"), ("support", "update"),
    },
    "inventory_manager": {
        ("dashboard", "read"),
        ("products", "create"), ("products", "read"), ("products", "update"),
        ("inventory", "read"), ("inventory", "update"),
        ("categories", "create"), ("categories", "read"), ("categories", "update"),
        ("transfers", "create"), ("transfers", "read"), ("transfers", "update"),
    },
    # 'staff' intentionally absent — an orphaned staff account (role_id lost
    # its role) falls through to an empty set via .get(..., set()), not a
    # KeyError. See Role's docstring in app/models/role.py.
}
