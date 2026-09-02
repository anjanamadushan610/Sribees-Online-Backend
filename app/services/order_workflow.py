"""
Order fulfilment state machine (QA spec B1).

Defines the legal forward/secondary transitions per order status and the
contextual action buttons the admin Order Details panel renders. Consulted by:

  * ``PATCH /admin/orders/{id}/status``    — standard transitions, validated here
  * ``GET  /admin/orders/{id}/next-statuses`` — button metadata for the UI
  * ``POST /admin/orders/{id}/status/override`` — Super Admin ONLY, bypasses
    this machine entirely (mandatory reason, audit-logged)

RBAC (B1 §2): only ``super_admin`` and ``branch_manager`` perform standard
transitions (a Branch Manager only for orders in their own branch — enforced by
``inject_branch_filter`` / ``_load_scoped_order``, not here). ``customer_support``
and delegated ``staff`` are read-only for status and get an empty action list.

Every non-admin caller of ``OrderService.update_status`` (courier webhook/sync,
payment confirmation, cancellation helpers) passes ``enforce=False`` and is not
touched by this module.
"""
from __future__ import annotations

# Roles allowed to drive a standard status transition.
FULFILMENT_ROLES: frozenset[str] = frozenset({"super_admin", "branch_manager"})

# Legacy pre-workflow state — treated as an alias of `packing`.
LEGACY_ALIASES: dict[str, str] = {"processing": "packing"}

# Natural lifecycle order — kept in sync with app/schemas/order.py OrderStatusEnum.
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
    "delivery_failed",
    "rto_initiated",
    "cancelled",
    "return_requested",
    "return_approved",
    "refunded",
)

# Contextual actions per current status (B1 §3). "primary" is the happy-path
# next step, "secondary" is the alternate/exception path. Labels are the single
# source of truth — the admin UI renders exactly these.
_ACTIONS: dict[str, list[dict[str, str]]] = {
    "pending": [
        {"status": "confirmed", "label": "Confirm Order", "kind": "primary"},
        {"status": "cancelled", "label": "Cancel Order", "kind": "danger"},
    ],
    "confirmed": [
        {"status": "packing", "label": "Start Packing", "kind": "primary"},
        {"status": "cancelled", "label": "Cancel Order", "kind": "danger"},
    ],
    "processing": [
        {"status": "packed", "label": "Mark as Packed", "kind": "primary"},
        {"status": "cancelled", "label": "Cancel Order", "kind": "danger"},
    ],
    "packing": [
        {"status": "packed", "label": "Mark as Packed", "kind": "primary"},
    ],
    "packed": [
        {"status": "handed_to_courier", "label": "Handover to Courier", "kind": "primary"},
    ],
    "handed_to_courier": [
        {"status": "shipped", "label": "Mark as Shipped", "kind": "primary"},
    ],
    "shipped": [
        {"status": "out_for_delivery", "label": "Out for Delivery", "kind": "primary"},
    ],
    "out_for_delivery": [
        {"status": "delivered", "label": "Mark as Delivered", "kind": "primary"},
        {"status": "delivery_failed", "label": "Delivery Failed", "kind": "danger"},
    ],
    "delivery_failed": [
        {"status": "out_for_delivery", "label": "Reschedule Delivery", "kind": "primary"},
        {"status": "rto_initiated", "label": "Return to Hub / RTO", "kind": "danger"},
    ],
    # Terminal / owned by a dedicated flow — no state-machine buttons.
    "delivered": [],
    "rto_initiated": [],
    "cancelled": [],
    "return_requested": [],
    "return_approved": [],
    "refunded": [],
}


class InvalidTransitionError(ValueError):
    """A standard transition the state machine does not allow."""


def _norm(status: str) -> str:
    return LEGACY_ALIASES.get(status, status)


def allowed_actions(current: str, actor_role: str | None) -> list[dict[str, str]]:
    """Contextual action buttons the actor may use from *current*. Empty for any
    role outside FULFILMENT_ROLES (read-only) and for terminal states."""
    if actor_role not in FULFILMENT_ROLES:
        return []
    return list(_ACTIONS.get(_norm(current), []))


def next_statuses(current: str, actor_role: str | None) -> list[str]:
    return [a["status"] for a in allowed_actions(current, actor_role)]


def is_allowed(current: str, target: str, actor_role: str | None) -> bool:
    return target in next_statuses(current, actor_role)


def validate_transition(current: str, target: str, actor_role: str | None) -> None:
    """Raise :class:`InvalidTransitionError` unless the standard transition is
    permitted for the actor. (Super Admin override does not come through here.)"""
    if is_allowed(current, target, actor_role):
        return
    if actor_role not in FULFILMENT_ROLES:
        raise InvalidTransitionError(
            "Your role can view this order but not change its status."
        )
    if current == target:
        raise InvalidTransitionError(f"The order is already '{current.replace('_', ' ')}'.")
    allowed = ", ".join(s.replace("_", " ") for s in next_statuses(current, actor_role)) or "no further changes"
    raise InvalidTransitionError(
        f"Cannot move an order from '{current.replace('_', ' ')}' to "
        f"'{target.replace('_', ' ')}'. Allowed from here: {allowed}."
    )
