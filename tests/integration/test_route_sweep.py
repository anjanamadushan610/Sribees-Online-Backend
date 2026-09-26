"""
Every list and dashboard route answers, for the realm it belongs to.

The security suite proves each route REFUSES the wrong caller. This proves
the other half: for the right caller, against a real database holding a
branch, a product, a customer and an order, no parameter-free GET route
fails with a 500. A 4xx is an answer (a missing query parameter, a feature
switched off); a 500 is a page that breaks in front of someone.

Routes are derived from the app, so a new page is covered the day it lands.
"""
from __future__ import annotations

import pytest

from tests.security.conftest import api_routes, route_realm

# Routes whose normal answer is a stream or a redirect, not a JSON page.
_SKIP = {
    "/api/v1/chat/stream",
}


def _gets(realm: str) -> list[str]:
    out = set()
    for r in api_routes():
        if route_realm(r) == realm and "GET" in r.methods and "{" not in r.path and r.path not in _SKIP:
            out.add(r.path)
    return sorted(out)


@pytest.fixture
async def seeded(world, make_product, make_customer, place_order):
    """One of everything the list pages read: a product, a customer, an order."""
    pid = await make_product()
    customer = await make_customer()
    order = await place_order(customer, pid)
    return customer, order


def _assert_answers(path: str, r) -> None:
    assert r.status_code < 500, f"{path} -> {r.status_code}: {r.text[:300]}"


async def test_every_admin_page_answers_the_super_admin(api, seeded, make_admin):
    boss = await make_admin("super_admin", api=api)
    paths = _gets("admin")
    assert len(paths) > 40, "the admin realm lost its routes -- did the route walk break?"
    for path in paths:
        _assert_answers(path, await api.get(path, headers=boss.headers))


@pytest.mark.parametrize("role", ["branch_manager", "marketing_manager", "inventory_manager", "customer_support"])
async def test_every_admin_page_answers_a_branch_role_without_a_500(api, seeded, make_admin, role):
    """A refused page is a 403, never a 500 on the way to deciding."""
    admin = await make_admin(role, api=api)
    for path in _gets("admin"):
        _assert_answers(path, await api.get(path, headers=admin.headers))


async def test_every_customer_page_answers_the_customer(api, seeded):
    customer, _ = seeded
    paths = _gets("customer")
    assert len(paths) > 15
    for path in paths:
        _assert_answers(path, await api.get(path, headers=customer.headers))


async def test_every_public_page_answers_a_guest_and_a_customer(api, seeded):
    customer, _ = seeded
    for path in _gets("public"):
        _assert_answers(path, await api.get(path))
        _assert_answers(path, await api.get(path, headers=customer.headers))
