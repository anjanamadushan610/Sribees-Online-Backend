"""Branch cashback: only Branch Manager / Super Admin may set it, capped at 30 %."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.admin import AdminRole
from app.services import branch_cashback


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _DB:
    """Answers the one app_settings lookup the guard makes."""

    def __init__(self, value=None, active=True):
        self.row = None if value is None else SimpleNamespace(value=value, is_active=active)

    async def execute(self, _query):
        return _Result(self.row)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    ["marketing_manager", "inventory_manager", "staff", "customer_support", AdminRole.INVENTORY_MANAGER],
)
async def test_other_roles_cannot_touch_cashback(role):
    with pytest.raises(HTTPException) as err:
        await branch_cashback.guard(_DB(), role, {"cashback_percentage": 1.0})
    assert err.value.status_code == 403
    assert err.value.detail["code"] == "CASHBACK_NOT_ALLOWED"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["marketing_manager", "inventory_manager", "staff"])
async def test_other_roles_cannot_clear_cashback_either(role):
    with pytest.raises(HTTPException):
        await branch_cashback.guard(_DB(), role, {"cashback_percentage": None}, current=2.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["inventory_manager", "marketing_manager", "staff"])
async def test_writes_without_cashback_pass_for_everyone(role):
    await branch_cashback.guard(_DB(), role, {"stock_quantity": 5, "branch_price": 100})


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["branch_manager", "super_admin", AdminRole.BRANCH_MANAGER])
async def test_editors_may_set_up_to_the_cap(role):
    await branch_cashback.guard(_DB(), role, {"cashback_percentage": 30})
    await branch_cashback.guard(_DB(), role, {"cashback_percentage": 0})
    await branch_cashback.guard(_DB(), role, {"cashback_percentage": None})


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["branch_manager", "super_admin"])
async def test_above_the_default_cap_is_refused(role):
    with pytest.raises(HTTPException) as err:
        await branch_cashback.guard(_DB(), role, {"cashback_percentage": 30.01})
    assert err.value.status_code == 400
    assert err.value.detail["code"] == "CASHBACK_ABOVE_CAP"
    assert err.value.detail["cap"] == 30.0


@pytest.mark.asyncio
async def test_cap_comes_from_app_settings():
    db = _DB("12.5")
    assert await branch_cashback.branch_max_percent(db) == 12.5
    with pytest.raises(HTTPException):
        await branch_cashback.guard(db, "branch_manager", {"cashback_percentage": 13})


@pytest.mark.asyncio
@pytest.mark.parametrize("value,active", [("abc", True), ("150", True), ("5", False)])
async def test_bad_or_inactive_setting_falls_back_to_30(value, active):
    assert await branch_cashback.branch_max_percent(_DB(value, active)) == 30


def test_describe_tells_the_ui_who_may_edit():
    assert branch_cashback.describe("branch_manager", 30) == {
        "can_edit_cashback": True,
        "cashback_branch_max_percent": 30.0,
    }
    assert branch_cashback.describe("inventory_manager", 30)["can_edit_cashback"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["inventory_manager", "marketing_manager", "staff"])
@pytest.mark.parametrize("current,sent", [(2.0, 2), (None, None), (1.5, 1.50)])
async def test_a_save_that_keeps_the_rate_passes_for_anyone(role, current, sent):
    """The admin forms send every field; saving stock must not be refused."""
    await branch_cashback.guard(_DB(), role, {"cashback_percentage": sent, "stock_quantity": 3}, current=current)


@pytest.mark.asyncio
@pytest.mark.parametrize("current,sent", [(2.0, None), (None, 0), (0.5, 1)])
async def test_a_real_change_is_still_refused(current, sent):
    with pytest.raises(HTTPException):
        await branch_cashback.guard(_DB(), "inventory_manager", {"cashback_percentage": sent}, current=current)


@pytest.mark.asyncio
async def test_an_old_rate_above_a_lowered_cap_may_be_kept():
    await branch_cashback.guard(_DB("10"), "branch_manager", {"cashback_percentage": 25}, current=25)
