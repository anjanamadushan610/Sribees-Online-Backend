"""
Guards for the RBAC boundaries QA specs B5 / B6 depend on.

The dashboard endpoint gates every money figure on ``analytics:read`` (B6) and
unblocks Categories for support on ``categories:read`` (B5) — both rely on these
exact entries in SYSTEM_ROLE_PERMISSIONS.
"""
from app.core.permission_catalog import DELEGATABLE_PERMISSIONS, SYSTEM_ROLE_PERMISSIONS


def test_customer_support_cannot_see_revenue():
    """B6: support holds orders:read to work orders — it must NOT imply
    analytics:read, which is what unlocks revenue on the dashboard."""
    support = SYSTEM_ROLE_PERMISSIONS["customer_support"]
    assert ("orders", "read") in support
    assert ("analytics", "read") not in support


def test_customer_support_has_view_only_catalog():
    """B5: Products and Categories are readable, never writable, for support."""
    support = SYSTEM_ROLE_PERMISSIONS["customer_support"]
    assert ("products", "read") in support
    assert ("categories", "read") in support
    for write in ("create", "update", "delete"):
        assert ("products", write) not in support
        assert ("categories", write) not in support


def test_analytics_read_is_delegatable():
    """So a Branch Manager can explicitly grant a fulfilment-staff account the
    financial view when they genuinely need it (B6: 'if he set permisson')."""
    assert ("analytics", "read") in DELEGATABLE_PERMISSIONS


def test_only_super_admin_holds_every_delegatable_permission():
    assert SYSTEM_ROLE_PERMISSIONS["super_admin"] == set(DELEGATABLE_PERMISSIONS)
    for role, perms in SYSTEM_ROLE_PERMISSIONS.items():
        if role != "super_admin":
            assert perms < set(DELEGATABLE_PERMISSIONS)
