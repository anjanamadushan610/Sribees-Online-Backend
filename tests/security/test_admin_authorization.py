"""
Admin authorisation: being logged in as SOME admin is not enough.

There are six admin roles plus delegated staff accounts whose permissions are
a subset of their creator's. A route guarded only by get_current_admin admits
all of them -- a Customer Support login could edit the catalogue, a staff
account could broadcast a push to every customer. That was true of 17 routes
until 2026-09-25.

So every admin route must carry a role check, a permission check, or branch
scoping, unless it is on the reviewed list below of things every admin may do
to their own account.
"""
from __future__ import annotations

import pytest

from tests.security.conftest import (
    _dependency_names,
    api_routes,
    make_token,
    route_realm,
)

pytestmark = pytest.mark.security

_STRONG = (
    "role_checker",
    "permission_checker",
    "require_super_admin",
    "require_base_role_admin",
    "inject_branch_filter",
)

# Things any signed-in admin may do: about themselves, or read-only lookups
# the sidebar needs before it knows what the admin is allowed to see.
SELF_SERVICE: set[tuple[str, str]] = {
    ("GET", "/api/v1/admin/auth/profile"),
    ("PUT", "/api/v1/admin/auth/profile"),
    ("POST", "/api/v1/admin/auth/change-password"),
    ("POST", "/api/v1/admin/auth/logout"),
    ("GET", "/api/v1/admin/branch-options"),
    ("GET", "/api/v1/admin/roles/catalog"),
    ("GET", "/api/v1/admin/roles"),
}

# Money, the catalogue and mass messaging: Super Admin and nobody else.
SUPER_ADMIN_ONLY: list[tuple[str, str]] = [
    ("POST", "/api/v1/products"),
    ("PUT", "/api/v1/products/{product_id}"),
    ("DELETE", "/api/v1/products/{product_id}"),
    ("POST", "/api/v1/products/{product_id}/images"),
    ("DELETE", "/api/v1/products/{product_id}/images/{image_id}"),
    ("POST", "/api/v1/products/{product_id}/variants"),
    ("PATCH", "/api/v1/products/{product_id}/stock"),
    ("POST", "/api/v1/categories"),
    ("PUT", "/api/v1/categories/{category_id}"),
    ("DELETE", "/api/v1/categories/{category_id}"),
    ("GET", "/api/v1/orders/admin/all"),
    ("PUT", "/api/v1/orders/admin/{order_id}/status"),
    ("PUT", "/api/v1/orders/admin/{order_id}/payment"),
    ("GET", "/api/v1/orders/admin/stats"),
    ("POST", "/api/v1/payments/refund"),
    ("POST", "/api/v1/notifications/admin/send"),
    ("POST", "/api/v1/notifications/admin/broadcast"),
]


def _weakly_guarded() -> set[tuple[str, str]]:
    out = set()
    for r in api_routes():
        if route_realm(r) != "admin":
            continue
        names = _dependency_names(r.dependant, set())
        if not any(s in n for n in names for s in _STRONG):
            for m in r.methods - {"HEAD", "OPTIONS"}:
                out.add((m, r.path))
    return out


def test_every_admin_route_checks_more_than_being_an_admin():
    weak = _weakly_guarded()
    unreviewed = sorted(weak - SELF_SERVICE)
    assert not unreviewed, (
        "These admin routes admit ANY admin role, delegated staff included. "
        "Add require_permission()/require_roles()/inject_branch_filter, or -- "
        f"only if every admin really should -- list them in SELF_SERVICE: {unreviewed}"
    )


def test_super_admin_only_routes_exist():
    present = {(m, r.path) for r in api_routes() for m in r.methods}
    missing = [c for c in SUPER_ADMIN_ONLY if c not in present]
    assert not missing, missing


@pytest.mark.parametrize(
    "role",
    ["branch_manager", "marketing_manager", "inventory_manager", "customer_support", "staff"],
)
@pytest.mark.parametrize(("method", "path"), SUPER_ADMIN_ONLY, ids=[f"{m} {p}" for m, p in SUPER_ADMIN_ONLY])
async def test_other_roles_cannot_reach_super_admin_only_routes(
    client, storage_free_app, monkeypatch, role, method, path
):
    """
    The token is real and the admin exists -- get_current_admin is stubbed to
    return one with the given role -- so only the role check stands between
    the request and the handler.
    """
    from types import SimpleNamespace

    from app.core import dependencies
    from app.models.admin import AdminRole

    async def _admin_with_role():
        return SimpleNamespace(
            admin_id="00000000-0000-0000-0000-000000000009",
            role=AdminRole(role),
            branch_id=None,
            is_active=True,
        )

    storage_free_app.dependency_overrides[dependencies.get_current_admin] = _admin_with_role
    try:
        from tests.security.conftest import concrete

        r = await client.request(
            method,
            concrete(path),
            headers={"Authorization": f"Bearer {make_token(is_admin=True, role=role)}"},
            json={} if method in {"POST", "PUT", "PATCH"} else None,
        )
    finally:
        storage_free_app.dependency_overrides.pop(dependencies.get_current_admin, None)
    assert r.status_code == 403, f"{role}: {method} {path} -> {r.status_code}: {r.text[:200]}"
