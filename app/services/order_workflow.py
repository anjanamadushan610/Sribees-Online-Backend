"""
Order fulfilment state machine.

The customer-facing lifecycle of a fulfilled order is:

    pending -> confirmed -> packing -> packed -> handed_to_courier
            -> shipped -> out_for_delivery -> delivered

with `cancelled` reachable from every pre-dispatch state and the returns flow
(`return_requested` -> `return_approved`/`delivered` -> `refunded`) handled by
its own dedicated endpoints rather than this map.

This module is consulted ONLY when an admin drives a status change through
`PATCH /admin/orders/{id}/status` with ``enforce=True``. Every other caller of
``OrderService.update_status`` — the SribeesExpress webhook/sync, payment
confirmation, cancellation helpers — passes ``enforce=False`` and is
unaffected, because those transitions are system-authoritative (a courier
genuinely can report `delivered` straight after `handed_to_courier`).

`super_admin` bypasses the machine entirely: an override that can un-cancel an
order or jump it to any state is a deliberate Super-Admin power.
"""
from __future__ import annotations

# Legacy pre-workflow state. Orders placed before the packing/packed/
# handed_to_courier states existed sit in `processing`; treat it as an alias
# of `packing` so those orders can still be advanced by branch staff.
LEGACY_PACKING_ALIAS = "processing"

# Allowed target states per current state for a branch-level actor
# (branch_manager, customer_support, or a delegated staff account holding
# orders:update). Forward progression plus same-branch corrections
# (packed -> packing) and pre-dispatch cancellation.
ADMIN_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"confirmed", "packing", "cancelled"},
    "confirmed": {"packing", "cancelled"},
    "processing": {"packing", "packed", "cancelled"},
    "packing": {"packed", "confirmed", "cancelled"},
    "packed": {"handed_to_courier", "packing", "cancelled"},
    "handed_to_courier": {"shipped", "packed"},
    "shipped": {"out_for_delivery", "delivered"},
    "out_for_delivery": {"delivered", "shipped"},
    # Terminal / owned by a dedicated flow — no free-form status edits.
    "delivered": set(),
    "cancelled": set(),
    "return_requested": set(),
    "return_approved": set(),
    "refunded": set(),
}

# The subset of states that represent branch-floor fulfilment work. Used by the
# admin UI to surface quick-action buttons and by dashboards to build a
# "to pack / to hand over" queue.
FULFILMENT_STATES: tuple[str, ...] = ("confirmed", "processing", "packing", "packed")


def next_statuses(current: str, actor_role: str | None) -> list[str]:
    """
    Ordered list of states an actor may move an order in ``current`` state to.

    ``super_admin`` gets every known state except the current one (full
    override). Everyone else gets the ADMIN_TRANSITIONS entry for the state.
    """
    if actor_role == "super_admin":
        return [s for s in ALL_STATUSES if s != current]
    return [s for s in ALL_STATUSES if s in ADMIN_TRANSITIONS.get(current, set())]


def is_allowed(current: str, target: str, actor_role: str | None) -> bool:
    if current == target:
        return False
    if actor_role == "super_admin":
        return target in ALL_STATUSES
    return target in ADMIN_TRANSITIONS.get(current, set())


class InvalidTransitionError(ValueError):
    """Raised when an admin attempts a status change the workflow forbids."""


def validate_transition(current: str, target: str, actor_role: str | None) -> None:
    """Raise :class:`InvalidTransitionError` unless the transition is allowed."""
    if is_allowed(current, target, actor_role):
        return
    if current == target:
        raise InvalidTransitionError(
            f"Order is already '{current.replace('_', ' ')}'."
        )
    allowed = next_statuses(current, actor_role)
    pretty = ", ".join(s.replace("_", " ") for s in allowed) or "no further changes"
    raise InvalidTransitionError(
        f"Cannot move an order from '{current.replace('_', ' ')}' to "
        f"'{target.replace('_', ' ')}'. Allowed from here: {pretty}."
    )


# Every status the machine knows about, in natural lifecycle order. Kept in
# sync with app/schemas/order.py::OrderStatusEnum.
ALL_STATUSES: tuple[str, ...] = (
    "pending",
    "confirmed",
    "processing",
    "packing",
    "packed",
    "handed_to_courier",
    "shipped",
    "out_for_delivery",
    "delivered",
    "cancelled",
    "return_requested",
    "return_approved",
    "refunded",
)
