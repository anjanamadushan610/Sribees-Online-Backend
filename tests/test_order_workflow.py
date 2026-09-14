"""Tests for the order fulfilment state machine (app/services/order_workflow.py, QA spec B1)."""
import pytest

from app.schemas.order import OrderStatusEnum
from app.services import order_workflow as wf


def test_all_statuses_matches_schema_enum():
    assert set(wf.ALL_STATUSES) == {s.value for s in OrderStatusEnum}


@pytest.mark.parametrize(
    "current,target",
    [
        ("pending", "confirmed"),
        ("pending", "cancelled"),
        ("confirmed", "packing"),
        ("packing", "packed"),
        ("packed", "ready_for_pickup"),
        ("ready_for_pickup", "cancelled"),
        ("handed_to_courier", "shipped"),
        ("shipped", "out_for_delivery"),
        ("out_for_delivery", "delivered"),
        ("out_for_delivery", "delivery_failed"),
        ("delivery_failed", "out_for_delivery"),
        ("delivery_failed", "rto_initiated"),
        ("processing", "packed"),  # legacy alias still advances
    ],
)
def test_manager_allowed_transitions(current, target):
    for role in ("branch_manager", "super_admin"):
        assert wf.is_allowed(current, target, role)
        wf.validate_transition(current, target, role)  # must not raise


@pytest.mark.parametrize(
    "current,target",
    [
        ("pending", "delivered"),       # skips the workflow
        ("confirmed", "shipped"),       # skips packing
        ("packed", "delivered"),        # skips pickup + ship
        # The branch no longer hands over by hand: a packed parcel is booked
        # (ready_for_pickup) and the courier's rider collecting it is what
        # makes it handed_to_courier.
        ("packed", "handed_to_courier"),
        ("ready_for_pickup", "handed_to_courier"),
        ("ready_for_pickup", "delivered"),
        ("delivered", "packing"),       # backwards from terminal
        ("cancelled", "confirmed"),     # un-cancel is override-only
    ],
)
def test_manager_forbidden_transitions(current, target):
    assert not wf.is_allowed(current, target, "branch_manager")
    with pytest.raises(wf.InvalidTransitionError):
        wf.validate_transition(current, target, "branch_manager")


@pytest.mark.parametrize("role", ["customer_support", "staff", None, "inventory_manager"])
def test_non_fulfilment_roles_are_read_only(role):
    assert wf.allowed_actions("confirmed", role) == []
    assert wf.next_statuses("out_for_delivery", role) == []
    with pytest.raises(wf.InvalidTransitionError):
        wf.validate_transition("confirmed", "packing", role)


def test_actions_carry_labels_and_kind():
    actions = wf.allowed_actions("out_for_delivery", "branch_manager")
    assert {a["status"] for a in actions} == {"delivered", "delivery_failed"}
    assert all({"status", "label", "kind"} <= a.keys() for a in actions)
    assert any(a["kind"] == "danger" for a in actions)


def test_packed_offers_exactly_the_courier_pickup():
    [action] = wf.allowed_actions("packed", "branch_manager")
    assert action["status"] == "ready_for_pickup"
    assert action["kind"] == "primary"


def test_terminal_states_have_no_actions():
    for s in ("delivered", "cancelled", "refunded", "rto_initiated", "return_approved"):
        assert wf.allowed_actions(s, "super_admin") == []


def test_invalid_transition_error_is_a_valueerror():
    assert issubclass(wf.InvalidTransitionError, ValueError)
