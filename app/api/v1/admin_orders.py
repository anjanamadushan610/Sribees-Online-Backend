"""
Admin Order Management API (Module 7.3)

Branch-scoped order operations. Restricted to super_admin, branch_manager and
customer_support (enforced at the router level). Branch isolation is applied
via ``inject_branch_filter`` — a Branch Manager only sees/edits orders whose
``branch_id`` matches their assigned branch; Super Admin and Customer Support
are unscoped and see every branch (optionally narrowed by ``?branch_id=``) —
a support agent has to be able to look up any customer's order regardless of
which branch fulfilled it, not just the branch they happen to be listed
under.

Prefix "/admin/orders" is applied by app/api/v1/router.py.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config.database import get_db
from app.core.dependencies import (
    BranchScope,
    get_current_admin,
    inject_branch_filter,
    require_permission,
    require_roles,
)
from app.core.media import media_url
from app.models.admin import Admin
from app.models.branch import Branch
from app.models.escalation import EscalationStatus, OrderEscalation
from app.models.order import Order, OrderItem
from app.models.user import User
from app.schemas.escalation import EscalationCreate, EscalationUpdate
from app.schemas.order import (
    OrderStatusEnum,
    OverrideOrderStatusRequest,
    PaymentMethodEnum,
    RejectReturnRequest,
    ResolveReturnRequest,
    ReturnNoteRequest,
    UpdateOrderStatusRequest,
)
from app.services import courier_sync_service, order_workflow
from app.services.invoice_service import InvoiceService
from app.services.notification_service import NotificationService
from app.services.order_service import OrderService, resolve_recipient_name

# Allow base-role admins and staff accounts that hold orders:read.
# Write routes additionally require orders:update (declared per-route).
router = APIRouter(
    dependencies=[Depends(require_permission("orders", "read"))],
    tags=["Admin Orders"],
)

# Statuses whose transition triggers a customer notification.
_NOTIFY_STATUSES = {
    OrderStatusEnum.READY_FOR_PICKUP,
    OrderStatusEnum.HANDED_TO_COURIER,
    OrderStatusEnum.SHIPPED,
    OrderStatusEnum.OUT_FOR_DELIVERY,
    OrderStatusEnum.DELIVERED,
    OrderStatusEnum.DELIVERY_FAILED,
}


def _actor_role(admin) -> str:
    return admin.role.value if hasattr(admin.role, "value") else str(admin.role)


def _format_order_row(order: Order, customer: Optional[User], branch_name: Optional[str]) -> dict:
    """Slim order representation for the list view."""
    return {
        "order_id": str(order.order_id),
        "order_number": order.order_number,
        "user_id": str(order.user_id),
        "customer_name": (customer.full_name if customer else None),
        "customer_email": (customer.email if customer else None),
        "branch_id": str(order.branch_id) if order.branch_id else None,
        "branch_name": branch_name,
        "status": order.status,
        "payment_status": order.payment_status,
        "total_amount": float(order.total_amount or 0),
        "item_count": len(order.items) if order.items is not None else 0,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        # Enough courier state to spot a parcel that needs a human without
        # opening every order. A failed booking is otherwise invisible from
        # the list: the order sits at Packed looking perfectly normal while
        # no rider has ever been asked to collect it.
        "courier_booking_status": order.courier_booking_status,
        "courier_waybill": order.courier_waybill_number,
        "courier_tracking_status": order.courier_tracking_status,
    }


def _format_order_detail(order: Order, branch_name: Optional[str]) -> dict:
    """Full order representation: items, pricing breakdown, customer, address."""
    customer = order.user
    addr = order.delivery_address
    return {
        "order_id": str(order.order_id),
        "order_number": order.order_number,
        "status": order.status,
        "payment_status": order.payment_status,
        "payment_method": order.payment_method,
        "branch_id": str(order.branch_id) if order.branch_id else None,
        "branch_name": branch_name,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "packed_at": order.packed_at.isoformat() if order.packed_at else None,
        "ready_for_pickup_at": (
            order.ready_for_pickup_at.isoformat() if order.ready_for_pickup_at else None
        ),
        "handed_to_courier_at": (
            order.handed_to_courier_at.isoformat() if order.handed_to_courier_at else None
        ),
        "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "delivery_failed_at": (
            order.delivery_failed_at.isoformat() if order.delivery_failed_at else None
        ),
        "delivery_slot_date": order.delivery_slot_date.isoformat() if order.delivery_slot_date else None,
        "delivery_slot_time": order.delivery_slot_time,
        "notes": order.notes,
        # Returns & refunds (Module 5.5 / B4)
        "return_reason": order.return_reason,
        "return_comments": order.return_comments,
        "return_items": order.return_items,
        # Resolved to URLs here, not stored that way -- this is what the
        # Branch Manager, Super Admin or Customer Support actually looks at
        # before approving or refusing the claim.
        "return_images": [media_url(p) for p in (order.return_images or [])],
        "return_requested_at": order.return_requested_at.isoformat() if order.return_requested_at else None,
        "refund_amount": float(order.refund_amount) if order.refund_amount is not None else None,
        "return_resolution": getattr(order, "return_resolution", None),
        "return_resolution_note": getattr(order, "return_resolution_note", None),
        # The reverse pickup, for the panel a Branch Manager works the claim
        # from. `return_handover_code` is deliberately here and nowhere on the
        # customer-facing payload — it is read to the rider at our counter.
        "return_waybill_number": getattr(order, "return_waybill_number", None),
        "return_handover_code": getattr(order, "return_handover_code", None),
        "return_booking_status": getattr(order, "return_booking_status", None),
        "return_booking_error": getattr(order, "return_booking_error", None),
        "return_fee": (
            float(order.return_fee) if getattr(order, "return_fee", None) is not None else None
        ),
        "return_tracking_status": getattr(order, "return_tracking_status", None),
        "return_approved_at": (
            order.return_approved_at.isoformat()
            if getattr(order, "return_approved_at", None)
            else None
        ),
        "return_collected_at": (
            order.return_collected_at.isoformat()
            if getattr(order, "return_collected_at", None)
            else None
        ),
        "return_received_at": (
            order.return_received_at.isoformat()
            if getattr(order, "return_received_at", None)
            else None
        ),
        # How long the customer had, and whether they were inside it. A claim
        # support escalated from outside the window is otherwise
        # indistinguishable from a fresh one, and they are not the same
        # decision.
        "hours_since_delivery": _hours_since_delivery(order),
        "customer": {
            "user_id": str(customer.user_id),
            "full_name": customer.full_name,
            "email": customer.email,
            "phone": customer.phone,
        } if customer else None,
        "delivery_address": {
            "address_line1": addr.address_line1,
            "address_line2": addr.address_line2,
            "postal_city": addr.postal_city,
            "district": addr.district,
            "province": addr.province,
            "postal_code": addr.postal_code,
        } if addr else None,
        "items": [
            {
                "order_item_id": str(it.order_item_id),
                "product_id": str(it.product_id),
                "product_name": it.product_name,
                "product_sku": it.product_sku,
                "product_image": it.product_image,
                "quantity": it.quantity,
                "unit_price": float(it.unit_price or 0),
                "subtotal": float(it.subtotal or 0),
                "is_returnable": bool(getattr(it, "is_returnable", True)),
            }
            for it in (order.items or [])
        ],
        "pricing": {
            "subtotal": float(order.subtotal or 0),
            "tax_amount": float(order.tax_amount or 0),
            "shipping_amount": float(order.shipping_amount or 0),
            "discount_amount": float(order.discount_amount or 0),
            "wallet_deduction": float(order.wallet_deduction or 0),
            "cashback_earned": float(order.cashback_earned or 0),
            "total_amount": float(order.total_amount or 0),
        },
        "history": [
            {
                "history_id": str(h.history_id),
                "old_status": h.old_status,
                "new_status": h.new_status,
                "changed_by": h.changed_by or "System",
                "notes": h.notes,
                "created_at": h.created_at.isoformat() if h.created_at else None,
            }
            for h in (order.status_history or [])
        ] if order.status_history else [
            *(
                [{
                    "history_id": "placed",
                    "old_status": None,
                    "new_status": "pending",
                    "changed_by": "Customer",
                    "notes": "Order placed by customer",
                    "created_at": order.created_at.isoformat(),
                }] if order.created_at else []
            ),
            *(
                [{
                    "history_id": "shipped",
                    "old_status": "processing",
                    "new_status": "shipped",
                    "changed_by": "System (Fulfillment)",
                    "notes": "Dispatched to courier",
                    "created_at": order.shipped_at.isoformat(),
                }] if order.shipped_at else []
            ),
            *(
                [{
                    "history_id": "delivered",
                    "old_status": "shipped",
                    "new_status": "delivered",
                    "changed_by": "System (Delivery)",
                    "notes": "Delivered to customer",
                    "created_at": order.delivered_at.isoformat(),
                }] if order.delivered_at else []
            ),
            *(
                [{
                    "history_id": "cancelled",
                    "old_status": "pending",
                    "new_status": "cancelled",
                    "changed_by": "System",
                    "notes": "Order cancelled",
                    "created_at": order.cancelled_at.isoformat(),
                }] if order.cancelled_at else []
            ),
        ],
    }


async def _branch_name(db: AsyncSession, branch_id: Optional[UUID]) -> Optional[str]:
    if branch_id is None:
        return None
    return (
        await db.execute(select(Branch.name).where(Branch.branch_id == branch_id))
    ).scalar_one_or_none()


async def _branch_pickup_registered(db: AsyncSession, branch_id: Optional[UUID]) -> bool:
    """
    Whether SribeesExpress knows where to collect this branch's parcels.

    True when the branch books with its own key — it is its own SribeesExpress
    client, collected at the address it registered with them — or when it is
    registered as an outlet of the shared account (contract section 12).

    Neither is not a booking failure: SribeesExpress falls back to the shared
    account's own address, which for a multi-branch merchant is another
    branch's. The rider drives to the wrong shop and nothing in the booking
    response says so, so the flag is surfaced before the parcel is marked ready
    for pickup, not discovered afterwards.
    """
    if branch_id is None:
        return False
    from app.services import courier_credentials

    try:
        target = await courier_credentials.resolve(db, branch_id)
    except (courier_credentials.CourierNotConfiguredError, courier_credentials.DecryptionError):
        return False
    if target.key_source == "branch":
        return True
    return (
        await db.execute(select(Branch.courier_outlet_id).where(Branch.branch_id == branch_id))
    ).scalar_one_or_none() is not None


# Order states before the parcel is picked (no rider yet) — QA spec B4 Module A.
_RIDER_PENDING_STATES = {
    "pending", "confirmed", "processing", "packing", "packed", "ready_for_pickup",
}


async def _fulfilment_contacts(db: AsyncSession, order: Order) -> dict:
    """Branch + rider contact card for the Order Details drawer (B4 Module A)."""
    branch_block = None
    if order.branch_id is not None:
        branch = (
            await db.execute(select(Branch).where(Branch.branch_id == order.branch_id))
        ).scalar_one_or_none()
        if branch is not None:
            manager_name = manager_email = None
            if branch.manager_id is not None:
                mgr = (
                    await db.execute(
                        select(Admin.full_name, Admin.email).where(
                            Admin.admin_id == branch.manager_id
                        )
                    )
                ).first()
                if mgr is not None:
                    manager_name, manager_email = mgr
            branch_block = {
                "name": branch.name,
                "code": branch.code,
                "phone": branch.phone,
                "manager_name": manager_name,
                "manager_email": manager_email,
            }

    dispatched = order.status not in _RIDER_PENDING_STATES
    rider_block = {
        "state": "assigned" if dispatched else "pending",
        # Rider name / phone / vehicle come from the courier payload, which the
        # current schema does not carry — surface what we have.
        "name": None,
        "phone": None,
        "vehicle": None,
        "waybill": order.courier_waybill_number,
        "tracking_status": order.courier_tracking_status,
        "courier_booking_status": order.courier_booking_status,
    }
    return {"branch": branch_block, "rider": rider_block}


def _courier_block(order: Order, *, branch_pickup_registered: bool = False) -> dict:
    """
    Everything the Order Details courier panel needs, including the three flags
    that decide what the operator is allowed to do next.

    ``can_request_pickup`` is the button's enablement: there is no shipment
    yet, and the order is either packed (pressing it marks the parcel ready for
    pickup, which books it) or already marked ready by a path that did not
    book it (pressing it retries the booking). A booked order shows its waybill
    instead -- re-pressing must never create a second parcel.

    ``branch_pickup_registered`` is a warning, not a block: booking an
    unregistered branch's parcel succeeds and sends the rider to the account
    default address. Refusing the booking over it would be worse than letting
    it through with the address wrong, so the panel warns and still allows it.

    ``quote_amount`` is what SribeesExpress agreed to bill for delivery;
    ``shipping_amount`` on the order is what the customer paid. They differ only
    when a booking had to re-quote after the price hold lapsed.
    """
    booked = bool(order.courier_shipment_id)
    is_cod = (order.payment_method or "").upper() == PaymentMethodEnum.COD.value
    return {
        "shipment_id": order.courier_shipment_id,
        "waybill": order.courier_waybill_number,
        "payment_method": "cod" if is_cod else "prepaid",
        "cod_amount": float(order.total_amount or 0) if is_cod else 0.0,
        "quote_id": order.courier_quote_id,
        "quote_amount": (
            float(order.courier_quote_amount) if order.courier_quote_amount is not None else None
        ),
        "quote_expires_at": (
            order.courier_quote_expires_at.isoformat() if order.courier_quote_expires_at else None
        ),
        "quote_confirmed_at": (
            order.courier_quote_confirmed_at.isoformat()
            if order.courier_quote_confirmed_at
            else None
        ),
        "booking_status": order.courier_booking_status,
        "tracking_status": order.courier_tracking_status,
        "tracking_url": order.courier_tracking_url,
        "tracking_updated_at": (
            order.courier_tracking_updated_at.isoformat()
            if order.courier_tracking_updated_at
            else None
        ),
        "pickup_location_name": order.courier_pickup_location_name,
        "handover_required": order.courier_handover_required,
        "handover_verified": order.courier_handover_verified,
        # COD only: `delivered` alone does NOT mean the cash was collected.
        "cod_collected": order.courier_cod_collected,
        "cod_collected_amount": (
            float(order.courier_cod_collected_amount)
            if order.courier_cod_collected_amount is not None
            else None
        ),
        "remittance_id": order.courier_remittance_id,
        "failure_note": order.courier_failure_note,
        "will_retry": order.courier_will_retry,
        "can_request_pickup": (
            not booked
            and order.status
            in (OrderStatusEnum.PACKED.value, OrderStatusEnum.READY_FOR_PICKUP.value)
        ),
        "can_refresh_tracking": booked,
        "branch_pickup_registered": branch_pickup_registered,
    }


@router.get("", response_model=dict)
async def list_orders(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    order_status: Optional[OrderStatusEnum] = Query(None, description="Filter by a single order status"),
    order_statuses: Optional[str] = Query(
        None, description="Filter by any of these statuses (comma-separated) — used by the order tabs (B2)"
    ),
    search: Optional[str] = Query(None, description="Match order number, customer name or email"),
    branch_id: Optional[UUID] = Query(
        None, description="Super Admin / Customer Support only: narrow to a branch (ignored for scoped admins)"
    ),
    from_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    to_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Paginated order list — branch-scoped for Branch Manager, network-wide for Super Admin and Customer Support."""
    effective_branch = scope.resolve(branch_id)
    offset = (page - 1) * limit

    # Filters other than status — shared by the list and the per-status counts,
    # so the tab/pill badges (QA spec B2) reflect the same date/branch/search
    # context the admin is looking at.
    conds = []
    if effective_branch is not None:
        conds.append(Order.branch_id == effective_branch)
    if search:
        pattern = f"%{search.strip()}%"
        conds.append(
            or_(
                Order.order_number.ilike(pattern),
                User.full_name.ilike(pattern),
                User.email.ilike(pattern),
            )
        )
    if from_date:
        try:
            # tz-aware: Order.created_at is timestamptz, and a naive datetime
            # here would silently compare as UTC regardless of what the admin
            # meant (see Window in admin_analytics.py for the same pattern).
            from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            conds.append(Order.created_at >= from_dt)
        except ValueError:
            pass
    if to_date:
        try:
            to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
            conds.append(Order.created_at < to_dt)
        except ValueError:
            pass

    # Per-status counts for the tab/pill badges (status-agnostic).
    count_stmt = (
        select(Order.status, func.count(Order.order_id))
        .join(User, Order.user_id == User.user_id)
        .where(*conds)
        .group_by(Order.status)
    )
    status_counts = {
        row[0]: int(row[1]) for row in (await db.execute(count_stmt)).all()
    }

    # Resolve the status filter: an explicit multi-select (order_statuses) wins,
    # else the single order_status, else no status filter.
    status_values: list[str] = []
    if order_statuses:
        status_values = [s.strip() for s in order_statuses.split(",") if s.strip()]
    elif order_status is not None:
        status_values = [order_status.value]

    query = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.user))
        .join(User, Order.user_id == User.user_id)
        .where(*conds)
    )
    if status_values:
        query = query.where(Order.status.in_(status_values))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0

    query = query.order_by(Order.created_at.desc()).limit(limit).offset(offset)
    orders = (await db.execute(query)).scalars().unique().all()

    # Resolve branch names in one query for the page.
    branch_ids = {o.branch_id for o in orders if o.branch_id}
    names: dict[UUID, str] = {}
    if branch_ids:
        rows = (
            await db.execute(select(Branch.branch_id, Branch.name).where(Branch.branch_id.in_(branch_ids)))
        ).all()
        names = dict(rows)

    return {
        "success": True,
        "data": {
            "orders": [_format_order_row(o, o.user, names.get(o.branch_id)) for o in orders],
            "pagination": {
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": (total + limit - 1) // limit if limit else 1,
            },
            # {status: count} across the current date/branch/search context —
            # the admin order tabs (B2) derive every badge from this.
            "status_counts": status_counts,
            "scope": {
                "is_super_admin": scope.is_super_admin,
                "branch_id": str(effective_branch) if effective_branch else None,
            },
        },
    }


@router.get("/export/csv")
async def export_orders_csv(
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    order_status: Optional[OrderStatusEnum] = None,
    branch_id: Optional[UUID] = None,
    search: Optional[str] = Query(None, min_length=1),
    from_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    to_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    order_ids: Optional[str] = Query(None, description="Comma separated UUIDs"),
):
    """Export orders to CSV file matching active filters or selected order_ids."""
    import csv
    import io

    effective_branch = scope.resolve(branch_id)

    query = select(Order).options(
        selectinload(Order.user),
        selectinload(Order.items),
        selectinload(Order.delivery_address),
    )

    if effective_branch is not None:
        query = query.where(Order.branch_id == effective_branch)
    if order_status:
        query = query.where(Order.status == order_status.value)
    if search:
        term = f"%{search.strip()}%"
        query = query.outerjoin(User, Order.user_id == User.user_id).where(
            or_(
                Order.order_number.ilike(term),
                User.full_name.ilike(term),
                User.email.ilike(term),
            )
        )
    if from_date:
        try:
            # tz-aware: Order.created_at is timestamptz, and a naive datetime
            # here would silently compare as UTC regardless of what the admin
            # meant (see Window in admin_analytics.py for the same pattern).
            from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            query = query.where(Order.created_at >= from_dt)
        except ValueError:
            pass
    if to_date:
        try:
            to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
            query = query.where(Order.created_at < to_dt)
        except ValueError:
            pass
    if order_ids:
        raw_ids = [id_str.strip() for id_str in order_ids.split(",") if id_str.strip()]
        try:
            uuid_ids = [UUID(id_str) for id_str in raw_ids]
            query = query.where(Order.order_id.in_(uuid_ids))
        except ValueError:
            pass

    query = query.order_by(Order.created_at.desc())
    res = await db.execute(query)
    orders = res.scalars().unique().all()

    branches_res = await db.execute(select(Branch))
    branch_map = {b.branch_id: b.name for b in branches_res.scalars().all()}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Order Number", "Date", "Customer Name", "Customer Email", "Customer Phone",
        "Branch", "Postal City", "Items Count", "Subtotal (LKR)", "Shipping (LKR)",
        "Discount (LKR)", "Total (LKR)", "Payment Method", "Payment Status", "Order Status",
    ])

    for o in orders:
        cust = o.user
        cust_name = cust.full_name if cust else "Customer"
        cust_email = cust.email if cust else ""
        cust_phone = cust.phone if cust else ""
        branch_name = branch_map.get(o.branch_id, "Unassigned")
        addr = o.delivery_address
        po = addr.postal_city if addr else ""

        writer.writerow([
            o.order_number,
            o.created_at.strftime("%Y-%m-%d %H:%M") if o.created_at else "",
            cust_name,
            cust_email,
            cust_phone,
            branch_name,
            po,
            len(o.items or []),
            float(o.subtotal or 0),
            float(o.shipping_amount or 0),
            float(o.discount_amount or 0),
            float(o.total_amount or 0),
            o.payment_method or "",
            o.payment_status or "",
            o.status or "",
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=orders_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"},
    )


@router.get("/export/pdf")
async def export_orders_pdf(
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    order_status: Optional[OrderStatusEnum] = None,
    branch_id: Optional[UUID] = None,
    search: Optional[str] = Query(None, min_length=1),
    from_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    to_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    order_ids: Optional[str] = Query(None, description="Comma separated UUIDs"),
):
    """Export orders to PDF Dispatch Manifest."""
    import io

    effective_branch = scope.resolve(branch_id)

    query = select(Order).options(
        selectinload(Order.user),
        selectinload(Order.items),
        selectinload(Order.delivery_address),
    )

    if effective_branch is not None:
        query = query.where(Order.branch_id == effective_branch)
    if order_status:
        query = query.where(Order.status == order_status.value)
    if search:
        term = f"%{search.strip()}%"
        query = query.outerjoin(User, Order.user_id == User.user_id).where(
            or_(
                Order.order_number.ilike(term),
                User.full_name.ilike(term),
                User.email.ilike(term),
            )
        )
    if from_date:
        try:
            # tz-aware: Order.created_at is timestamptz, and a naive datetime
            # here would silently compare as UTC regardless of what the admin
            # meant (see Window in admin_analytics.py for the same pattern).
            from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            query = query.where(Order.created_at >= from_dt)
        except ValueError:
            pass
    if to_date:
        try:
            to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
            query = query.where(Order.created_at < to_dt)
        except ValueError:
            pass
    if order_ids:
        raw_ids = [id_str.strip() for id_str in order_ids.split(",") if id_str.strip()]
        try:
            uuid_ids = [UUID(id_str) for id_str in raw_ids]
            query = query.where(Order.order_id.in_(uuid_ids))
        except ValueError:
            pass

    query = query.order_by(Order.created_at.desc())
    res = await db.execute(query)
    orders = res.scalars().unique().all()

    branch_name = "All Branches"
    if effective_branch:
        b_res = await db.execute(select(Branch).where(Branch.branch_id == effective_branch))
        b_obj = b_res.scalar_one_or_none()
        if b_obj:
            branch_name = b_obj.name

    try:
        pdf_bytes = InvoiceService.generate_dispatch_manifest_pdf(orders, branch_name=branch_name)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"Error generating dispatch manifest PDF: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate dispatch manifest PDF",
        )

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=dispatch_manifest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"},
    )


def _hours_since_delivery(order: Order) -> Optional[float]:
    """Hours between delivery and now, to one decimal — or None if undelivered.

    Shown beside a return claim so a manager can see at a glance whether it
    came in inside the 12-hour window. The API refuses a late request, so an
    out-of-window claim only exists because support raised it by hand; that is
    a legitimate thing to do and an illegitimate thing to do invisibly.
    """
    delivered_at = getattr(order, "delivered_at", None)
    if delivered_at is None:
        return None
    if delivered_at.tzinfo is None:
        delivered_at = delivered_at.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - delivered_at).total_seconds() / 3600, 1)


async def _load_scoped_order(db: AsyncSession, order_id: UUID, scope: BranchScope) -> Order:
    """Fetch an order with relations, enforcing branch isolation."""
    order = (
        await db.execute(
            select(Order)
            .options(
                selectinload(Order.items),
                selectinload(Order.delivery_address),
                selectinload(Order.user),
                selectinload(Order.status_history),
            )
            .where(Order.order_id == order_id)
        )
    ).scalar_one_or_none()

    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    # A scoped admin can only touch orders in their own branch.
    if not scope.is_unscoped and order.branch_id != scope.branch_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This order belongs to another branch.",
        )
    return order


@router.get("/{order_id}", response_model=dict)
async def get_order(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Full order details (items, pricing, customer, delivery address,
    fulfilment contacts, escalation tickets — B4 — and the SribeesExpress
    shipment)."""
    order = await _load_scoped_order(db, order_id, scope)
    branch_name = await _branch_name(db, order.branch_id)
    data = _format_order_detail(order, branch_name)
    data["fulfilment_contacts"] = await _fulfilment_contacts(db, order)
    data["escalations"] = await _list_escalations(db, order_id)
    data["courier"] = _courier_block(
        order, branch_pickup_registered=await _branch_pickup_registered(db, order.branch_id)
    )
    return {"success": True, "data": data}


@router.get("/{order_id}/invoice")
async def download_order_invoice(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Download the PDF invoice for an order (branch-scoped, Module 5.6).
    """
    order = await _load_scoped_order(db, order_id, scope)
    try:
        pdf = InvoiceService.generate_invoice_pdf(order, customer=order.user)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"Error generating invoice for order {order_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate invoice",
        )

    filename = InvoiceService.filename_for(order)
    return StreamingResponse(
        iter([pdf]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _notify_customer(order: Order, new_status: OrderStatusEnum, db: AsyncSession) -> None:
    if new_status not in _NOTIFY_STATUSES:
        return
    try:
        await NotificationService.notify_order_status(
            db,
            user_id=order.user_id,
            order_number=order.order_number,
            status=new_status.value,
            order_id=order.order_id,
        )
    except Exception as exc:  # pragma: no cover - notification is best-effort
        logger.warning(f"Order status notification failed for {order.order_number}: {exc}")


@router.get("/{order_id}/next-statuses", response_model=dict)
async def get_order_next_statuses(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    admin=Depends(get_current_admin),
):
    """Contextual status actions this admin may take on this order from its
    current state (B1 §3). Empty for read-only roles — the UI then shows a
    plain status badge. Super Admin additionally gets the override link."""
    order = await _load_scoped_order(db, order_id, scope)
    role = _actor_role(admin)
    return {
        "success": True,
        "data": {
            "status": order.status,
            "actions": order_workflow.allowed_actions(order.status, role),
            "can_override": role == "super_admin",
            "all_statuses": list(order_workflow.ALL_STATUSES),
        },
    }


@router.patch("/{order_id}/status", response_model=dict)
async def update_order_status(
    order_id: UUID,
    body: UpdateOrderStatusRequest,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    admin=Depends(require_permission("orders", "update")),
):
    """
    Standard status transition, held to the fulfilment state machine
    (app/services/order_workflow.py). Only ``super_admin`` and ``branch_manager``
    may drive it (a Branch Manager only within their own branch); every other
    role gets a 400. Notifies the customer on the milestone transitions.
    """
    order = await _load_scoped_order(db, order_id, scope)

    admin_name = getattr(admin, "full_name", None) or getattr(admin, "email", "Admin")
    try:
        await OrderService.update_status(
            db,
            order,
            body.status,
            changed_by=f"Admin ({admin_name})",
            enforce=True,
            actor_role=_actor_role(admin),
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await _notify_customer(order, body.status, db)
    branch_name = await _branch_name(db, order.branch_id)
    logger.info(f"[admin] Order {order.order_number} status -> {body.status.value}")
    return {
        "success": True,
        "data": _format_order_detail(order, branch_name),
        "message": f"Order status updated to {body.status.value}",
    }


@router.post("/{order_id}/courier/request-pickup", response_model=dict)
async def request_courier_pickup(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    admin=Depends(require_permission("orders", "update")),
):
    """
    Ask SribeesExpress to collect this order — the dashboard's Request Pickup.

    On a **packed** order this is the "ready for pickup" transition itself:
    it books the parcel with the delivery price the customer agreed to at
    checkout and how they pay (COD with the amount to collect, or prepaid),
    then moves the order to ``ready_for_pickup``. On an order already marked
    ready whose booking did not stick, it retries the booking. Branch-scoped
    like every other write here — a Branch Manager can only dispatch their own
    branch's parcels.

    Already booked returns 200 with the existing waybill, not an error:
    pressing twice is a double-click, not a fault, and must not produce a
    second parcel. A refusal — payment not in, courier said no — is a 409
    carrying the reason, because the operator is the one who can act on it.

    Held to FULFILMENT_ROLES for the same reason the status machine is
    (order_workflow §RBAC): dispatching a parcel is a branch-floor act, and
    Customer Support holds orders:update so they can annotate and escalate,
    not so they can send a rider. Their path to getting one sent is an
    escalation to the branch.
    """
    order = await _load_scoped_order(db, order_id, scope)
    if _actor_role(admin) not in order_workflow.FULFILMENT_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a Branch Manager or Super Admin can dispatch a parcel.",
        )

    admin_name = getattr(admin, "full_name", None) or getattr(admin, "email", "Admin")
    if order.status == OrderStatusEnum.PACKED.value:
        try:
            await OrderService.update_status(
                db,
                order,
                OrderStatusEnum.READY_FOR_PICKUP,
                changed_by=f"Admin ({admin_name})",
                notes="Packed and booked for courier pickup",
                enforce=True,
                actor_role=_actor_role(admin),
            )
        except ValueError as exc:
            logger.warning(
                f"[admin] Pickup request for {order.order_number} by {admin_name} refused: {exc}"
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        await _notify_customer(order, OrderStatusEnum.READY_FOR_PICKUP, db)
        ok, message = True, f"Ready for pickup. SribeesExpress waybill {order.courier_shipment_id}."
    elif order.status == OrderStatusEnum.READY_FOR_PICKUP.value or order.courier_shipment_id:
        ok, message = await OrderService.book_courier_shipment_now(db, order)
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A parcel can be sent for once it is packed; this order is "
                f"'{order.status.replace('_', ' ')}'."
            ),
        )

    if not ok:
        logger.warning(
            f"[admin] Pickup request for {order.order_number} by {admin_name} failed: {message}"
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)

    logger.info(f"[admin] Pickup requested for {order.order_number} by {admin_name}: {message}")
    branch_name = await _branch_name(db, order.branch_id)
    return {
        "success": True,
        "data": {
            "order": _format_order_detail(order, branch_name),
            "courier": _courier_block(
                order,
                branch_pickup_registered=await _branch_pickup_registered(db, order.branch_id),
            ),
        },
        "message": message,
    }


@router.get("/{order_id}/courier/label", response_model=dict)
async def get_courier_label(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Everything a shipping label needs for this parcel.

    SribeesExpress does have a label builder, but it lives on their staff API
    behind a staff login (`POST /api/v1/shipments/labels`, OAuth2), not on the
    merchant `/ecommerce` namespace our API key reaches — so a merchant cannot
    call it. What it returns is not a rendered label either: it is exactly
    these fields, with their own docs saying "the barcode is drawn client-side
    from `waybill_id`, which keeps paper size, printer model and layout out of
    the API".

    So we render it ourselves, from data we already hold, and deliberately
    mirror their `ShippingLabelOut` field-for-field. If they later expose the
    builder to merchants, the printer component does not change — only where
    the rows come from.

    404 until the parcel is booked: a label without a waybill is a sticker
    that routes nothing, and printing one is worse than printing none.
    """
    order = await _load_scoped_order(db, order_id, scope)
    if not order.courier_shipment_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No parcel booked yet — request a pickup before printing a label.",
        )

    address = order.delivery_address
    branch = (
        await db.execute(select(Branch).where(Branch.branch_id == order.branch_id))
    ).scalar_one_or_none() if order.branch_id else None

    # Billed weight, resolved the same way the rate quote resolves it, so the
    # number on the sticker is the number the parcel was priced at.
    item_rows = (
        await db.execute(
            select(OrderItem.product_id, OrderItem.quantity).where(
                OrderItem.order_id == order.order_id
            )
        )
    ).all()
    priced = await OrderService._attach_weights(
        db,
        [{"product_id": str(pid), "quantity": qty} for pid, qty in item_rows],
    )
    weight_kg = sum(
        Decimal(str(i.get("weight_kg") or 0)) * Decimal(str(i["quantity"])) for i in priced
    )

    is_cod = (order.payment_method or "").upper() == PaymentMethodEnum.COD.value
    address_parts = [
        address.address_line1,
        address.address_line2,
        address.postal_city,
        address.district,
    ] if address else []

    return {
        "success": True,
        "data": {
            "waybill_id": order.courier_shipment_id,
            "order_number": order.order_number,
            "booked_at": order.created_at.isoformat() if order.created_at else None,
            "sender_name": branch.name if branch else None,
            "sender_phone": branch.phone if branch else None,
            "origin_branch": branch.name if branch else None,
            "recipient_name": await resolve_recipient_name(db, order, address),
            "recipient_phone": (address.phone if address else None) or "",
            "recipient_address": ", ".join([p for p in address_parts if p]),
            "destination_city": address.postal_city if address else None,
            "destination_district": address.district if address else None,
            "postal_code": address.postal_code if address else None,
            "weight_kg": str(weight_kg),
            # The rider collects the ORDER total at the door, not the goods
            # value -- delivery is part of what the customer owes.
            "cod_amount": str(order.total_amount if is_cod else Decimal("0")),
            "is_cod": is_cod,
            "delivery_charge": str(order.shipping_amount or Decimal("0")),
            "item_count": len(item_rows),
            "status_name": order.courier_tracking_status,
            "requires_handover_code": bool(order.courier_handover_required),
            "tracking_url": order.courier_tracking_url,
        },
    }


@router.post("/{order_id}/courier/refresh", response_model=dict)
async def refresh_courier_tracking(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    admin=Depends(require_permission("orders", "update")),
):
    """
    Re-read this one shipment from SribeesExpress and apply what it says.

    Webhooks are not retried on their side, so a single missed push leaves an
    order stuck at a stale status with nothing to correct it until the next
    reconciliation sweep. This is that sweep for one order, on demand — what
    support reaches for when a customer says the parcel arrived and the
    dashboard disagrees.
    """
    order = await _load_scoped_order(db, order_id, scope)
    if not order.courier_shipment_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This order has no SribeesExpress shipment to refresh.",
        )

    # refresh_order_tracking swallows courier errors and returns None -- right
    # for a background sweep, wrong for a button someone just pressed.
    if await courier_sync_service.refresh_order_tracking(db, order) is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="SribeesExpress could not be reached. The status shown is the last one we had.",
        )
    # An open reverse pickup: the goods may be back already, which is what
    # releases the refund. Best-effort -- the forward refresh above worked.
    if order.status == OrderStatusEnum.RETURN_APPROVED.value and order.return_waybill_number:
        await courier_sync_service.refresh_return_tracking(db, order)

    branch_name = await _branch_name(db, order.branch_id)
    return {
        "success": True,
        "data": {
            "order": _format_order_detail(order, branch_name),
            "courier": _courier_block(
                order,
                branch_pickup_registered=await _branch_pickup_registered(db, order.branch_id),
            ),
        },
        "message": f"Tracking refreshed — {order.courier_tracking_status or 'unknown'}.",
    }


@router.post("/{order_id}/status/override", response_model=dict)
async def override_order_status(
    order_id: UUID,
    body: OverrideOrderStatusRequest,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    admin=Depends(require_roles("super_admin")),
):
    """
    Super-Admin emergency manual override (B1 §4). Bypasses the state machine,
    requires a >=15-char justification, and is permanently marked
    ``[EMERGENCY OVERRIDE]`` in the order's audit trail.
    """
    order = await _load_scoped_order(db, order_id, scope)
    admin_name = getattr(admin, "full_name", None) or getattr(admin, "email", "Super Admin")
    await OrderService.update_status(
        db,
        order,
        body.status,
        changed_by=f"SUPER ADMIN OVERRIDE ({admin_name})",
        notes=body.reason.strip(),
        is_override=True,
    )
    await _notify_customer(order, body.status, db)
    branch_name = await _branch_name(db, order.branch_id)
    logger.warning(
        f"[admin] OVERRIDE Order {order.order_number} -> {body.status.value} "
        f"by {admin_name}: {body.reason.strip()[:120]}"
    )
    return {
        "success": True,
        "data": _format_order_detail(order, branch_name),
        "message": f"Order status overridden to {body.status.value}",
    }


# Return/refund resolution is a Branch-Manager / Super-Admin decision (B4 §2).
# Customer Support captures the customer's proofs but cannot authorise a
# refund, dispatch a reverse pickup, or reject a claim.
_RETURN_DECIDER = Depends(require_roles("super_admin", "branch_manager"))


@router.post("/{order_id}/return/approve", response_model=dict)
async def approve_order_return(
    order_id: UUID,
    body: ResolveReturnRequest = ResolveReturnRequest(),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    _decider = _RETURN_DECIDER,
):
    """
    Approve a pending return (Branch Manager / Super Admin): refund the returned
    value to the customer's wallet and move the order to REFUNDED.
    ``resolution`` records whether this is a returnless refund (spoilage — no
    pickup) or a reverse pickup (a rider will collect the goods).
    """
    order = await _load_scoped_order(db, order_id, scope)
    admin_name = getattr(_decider, "full_name", None) or getattr(_decider, "email", "Admin")
    try:
        await OrderService.approve_return(
            db,
            order,
            resolution=body.resolution.value,
            note=body.note,
            changed_by=f"Admin ({admin_name})",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    branch_name = await _branch_name(db, order.branch_id)
    logger.info(
        f"[admin] Return approved ({body.resolution.value}) for order {order.order_number} by {admin_name}"
    )
    return {
        "success": True,
        "data": _format_order_detail(order, branch_name),
        "message": "Return approved and customer wallet credited",
    }


@router.post("/{order_id}/return/reject", response_model=dict)
async def reject_order_return(
    order_id: UUID,
    body: RejectReturnRequest = RejectReturnRequest(),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    _decider = _RETURN_DECIDER,
):
    """Reject a pending return (Branch Manager / Super Admin): revert to DELIVERED."""
    order = await _load_scoped_order(db, order_id, scope)
    admin_name = getattr(_decider, "full_name", None) or getattr(_decider, "email", "Admin")
    try:
        await OrderService.reject_return(
            db,
            order,
            note=body.note,
            changed_by=f"Admin ({admin_name})",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    branch_name = await _branch_name(db, order.branch_id)
    logger.info(f"[admin] Return rejected for order {order.order_number} by {admin_name}")
    return {
        "success": True,
        "data": _format_order_detail(order, branch_name),
        "message": "Return rejected",
    }


@router.post("/{order_id}/return/regenerate-code", response_model=dict)
async def regenerate_return_handover_code(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    _decider = _RETURN_DECIDER,
):
    """Get a new handover code for this order's reverse pickup.

    The code is what our counter staff read to the rider to close the return.
    SribeesExpress stores only a hash of it, so "we lost it" has exactly one
    answer: issue a new one, which kills the old one immediately. Restricted to
    the same roles that decide the claim — anyone who can mint this code can
    release goods back to a rider.
    """
    order = await _load_scoped_order(db, order_id, scope)
    if not order.return_waybill_number:
        raise HTTPException(
            status_code=400, detail="This order has no reverse pickup booked."
        )

    from app.services import courier_credentials
    from app.services.courier_service import CourierError, CourierService

    # Inside the branch's credential scope, as every other courier call is:
    # without it the client refuses before sending anything, and this button
    # answered every press with a 500.
    try:
        async with courier_credentials.for_branch(db, order.branch_id):
            result = await CourierService.regenerate_return_code(order.return_waybill_number)
    except (courier_credentials.CourierNotConfiguredError, courier_credentials.DecryptionError) as exc:
        raise HTTPException(status_code=503, detail=f"SribeesExpress is not configured for this branch: {exc}") from exc
    except CourierError as exc:
        raise HTTPException(status_code=502, detail=f"SribeesExpress: {exc.message}") from exc

    order.return_handover_code = result.get("handover_code")
    await db.commit()
    await db.refresh(order)
    logger.info(f"[admin] Return handover code regenerated for {order.order_number}")
    branch_name = await _branch_name(db, order.branch_id)
    return {
        "success": True,
        "data": _format_order_detail(order, branch_name),
        "message": "New handover code issued — the old one no longer works",
    }


@router.post("/{order_id}/return/retry-booking", response_model=dict)
async def retry_reverse_pickup_booking(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    _decider = _RETURN_DECIDER,
):
    """Book the reverse pickup again after a courier outage.

    Approval does not fail when SribeesExpress is unreachable — a manager's
    decision stands and the claim is left with `return_booking_status =
    'failed'`. This is the button that clears it. Safe to press repeatedly:
    the booking is idempotent on the order number, so a retry after a response
    we never saw returns the existing leg rather than sending a second rider.
    """
    order = await _load_scoped_order(db, order_id, scope)
    if order.status != OrderStatusEnum.RETURN_APPROVED.value:
        raise HTTPException(
            status_code=400, detail="This order has no approved return awaiting pickup."
        )
    await OrderService._book_reverse_pickup(db, order)
    branch_name = await _branch_name(db, order.branch_id)
    if order.return_booking_status != "booked":
        raise HTTPException(
            status_code=502,
            detail=order.return_booking_error or "SribeesExpress could not be reached.",
        )
    return {
        "success": True,
        "data": _format_order_detail(order, branch_name),
        "message": "Reverse pickup booked",
    }


@router.post("/{order_id}/return/note", response_model=dict,
             dependencies=[Depends(require_permission("orders", "update"))])
async def add_return_note(
    order_id: UUID,
    body: ReturnNoteRequest,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    admin=Depends(get_current_admin),
):
    """Customer Support / anyone with orders:update appends a proof note to a
    pending return claim (B4 §2 'Submit Quality Claim — upload proofs')."""
    order = await _load_scoped_order(db, order_id, scope)
    if order.status not in (
        OrderStatusEnum.RETURN_REQUESTED.value,
        OrderStatusEnum.RETURN_APPROVED.value,
    ):
        raise HTTPException(status_code=400, detail="This order has no open return claim.")
    who = getattr(admin, "full_name", None) or getattr(admin, "email", "Support")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    line = f"[{stamp} · {who}] {body.note.strip()}"
    order.return_comments = f"{order.return_comments}\n{line}" if order.return_comments else line
    await db.commit()
    await db.refresh(order)
    branch_name = await _branch_name(db, order.branch_id)
    return {
        "success": True,
        "data": _format_order_detail(order, branch_name),
        "message": "Note added to the return claim",
    }


# ============================================================================
# Internal escalation tickets (B4 Module B)
# ============================================================================

def _esc_out(e: OrderEscalation) -> dict:
    return {
        "escalation_id": str(e.escalation_id),
        "order_id": str(e.order_id),
        "raised_by_admin_id": str(e.raised_by_admin_id) if e.raised_by_admin_id else None,
        "category": e.category,
        "message": e.message,
        "status": e.status,
        "resolution_note": e.resolution_note,
        "raised_by_name": e.raised_by_name,
        "handled_by_name": e.handled_by_name,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "acknowledged_at": e.acknowledged_at.isoformat() if e.acknowledged_at else None,
        "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
    }


async def _list_escalations(db: AsyncSession, order_id: UUID) -> list[dict]:
    rows = (
        await db.execute(
            select(OrderEscalation)
            .where(OrderEscalation.order_id == order_id)
            .order_by(OrderEscalation.created_at.desc())
        )
    ).scalars().all()
    return [_esc_out(e) for e in rows]


@router.get("/{order_id}/escalations", response_model=dict)
async def list_order_escalations(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Escalation tickets on this order (branch-scoped via the order load)."""
    await _load_scoped_order(db, order_id, scope)
    return {"success": True, "data": {"escalations": await _list_escalations(db, order_id)}}


@router.post("/{order_id}/escalations", response_model=dict,
             dependencies=[Depends(require_permission("orders", "update"))])
async def raise_order_escalation(
    order_id: UUID,
    body: EscalationCreate,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    admin=Depends(get_current_admin),
):
    """Raise an internal support -> branch ticket (Support / BM / SA)."""
    order = await _load_scoped_order(db, order_id, scope)
    who = getattr(admin, "full_name", None) or getattr(admin, "email", "Admin")
    esc = OrderEscalation(
        order_id=order.order_id,
        branch_id=order.branch_id,
        raised_by_admin_id=getattr(admin, "admin_id", None),
        raised_by_name=who,
        category=body.category.value,
        message=body.message.strip(),
        status=EscalationStatus.OPEN.value,
    )
    db.add(esc)
    await db.commit()
    await db.refresh(esc)
    logger.info(f"[admin] Escalation raised on order {order.order_number} by {who}: {body.category.value}")
    return {"success": True, "data": _esc_out(esc), "message": "Escalation raised"}


@router.patch("/{order_id}/escalations/{escalation_id}", response_model=dict)
async def update_order_escalation(
    order_id: UUID,
    escalation_id: UUID,
    body: EscalationUpdate,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    admin=Depends(require_roles("super_admin", "branch_manager")),
):
    """Acknowledge or resolve a ticket — Branch Manager (own branch) / Super Admin."""
    await _load_scoped_order(db, order_id, scope)
    esc = (
        await db.execute(
            select(OrderEscalation).where(
                OrderEscalation.escalation_id == escalation_id,
                OrderEscalation.order_id == order_id,
            )
        )
    ).scalar_one_or_none()
    if esc is None:
        raise HTTPException(status_code=404, detail="Escalation not found")

    who = getattr(admin, "full_name", None) or getattr(admin, "email", "Admin")
    now = datetime.now(timezone.utc)
    esc.status = body.status.value
    esc.handled_by_name = who
    if body.resolution_note:
        esc.resolution_note = body.resolution_note.strip()
    if body.status == EscalationStatus.ACKNOWLEDGED and esc.acknowledged_at is None:
        esc.acknowledged_at = now
    if body.status == EscalationStatus.RESOLVED:
        esc.resolved_at = now
        if esc.acknowledged_at is None:
            esc.acknowledged_at = now
    await db.commit()
    await db.refresh(esc)
    return {"success": True, "data": _esc_out(esc), "message": f"Escalation {body.status.value}"}


@router.post(
    "/reconcile-payments",
    response_model=dict,
    dependencies=[Depends(require_permission("orders", "update"))],
)
async def reconcile_marx_payments(db: AsyncSession = Depends(get_db)):
    """
    Manually trigger the Marx IPG reconciliation sweep (the courier contract's
    sibling recommendation in the Marx doc's own section 7): resolves gateway
    orders stuck PENDING against Marx's GET .../summary. Cross-branch, so
    restricted to super_admin regardless of the router's default roles. Shares
    the exact same logic as ``scripts/reconcile_marx_payments.py`` (the
    unattended/cron path) via ``OrderService.reconcile_pending_gateway_payments``.
    """
    result = await OrderService.reconcile_pending_gateway_payments(db)
    return {
        "success": True,
        "data": result,
        "message": f"Checked {result['checked']} pending gateway order(s)",
    }
