"""Tests for the order fulfilment state machine (app/services/order_workflow.py)."""
import pytest

from app.schemas.order import OrderStatusEnum
from app.services import order_workflow as wf


def test_all_statuses_matches_schema_enum():
    assert set(wf.ALL_STATUSES) == {s.value for s in OrderStatusEnum}


@pytest.mark.parametrize(
    "current,target",
    [
        ("confirmed", "packing"),
        ("packing", "packed"),
        ("packed", "handed_to_courier"),
        ("handed_to_courier", "shipped"),
        ("packed", "packing"),        # correction
        ("confirmed", "cancelled"),   # pre-dispatch cancel
        ("processing", "packed"),     # legacy alias still advances
    ],
)
def test_branch_actor_allowed_transitions(current, target):
    assert wf.is_allowed(current, target, "branch_manager")
    wf.validate_transition(current, target, "staff")  # must not raise


@pytest.mark.parametrize(
    "current,target",
    [
        ("pending", "delivered"),          # skips the whole workflow
        ("confirmed", "shipped"),          # skips packing
        ("delivered", "packing"),          # backwards from terminal
        ("packed", "delivered"),           # skips handover + ship
        ("cancelled", "confirmed"),        # un-cancel is Super-Admin only
        ("handed_to_courier", "cancelled"),  # too late to cancel
    ],
)
def test_branch_actor_forbidden_transitions(current, target):
    assert not wf.is_allowed(current, target, "branch_manager")
    with pytest.raises(wf.InvalidTransitionError):
        wf.validate_transition(current, target, "customer_support")


def test_super_admin_overrides_any_transition():
    assert wf.is_allowed("delivered", "packing", "super_admin")
    assert wf.is_allowed("cancelled", "confirmed", "super_admin")
    wf.validate_transition("pending", "delivered", "super_admin")  # must not raise


def test_no_op_transition_is_rejected_for_everyone():
    assert not wf.is_allowed("packing", "packing", "super_admin")
    with pytest.raises(wf.InvalidTransitionError):
        wf.validate_transition("packing", "packing", "branch_manager")


def test_next_statuses_are_ordered_by_lifecycle():
    nxt = wf.next_statuses("packed", "branch_manager")
    assert nxt == ["packing", "handed_to_courier", "cancelled"]


def test_invalid_transition_error_is_a_valueerror():
    """admin_orders.py catches ValueError -> 400, so this subclassing matters."""
    assert issubclass(wf.InvalidTransitionError, ValueError)
