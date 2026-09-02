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
from app.models.branch import Branch
from app.models.order import Order
from app.models.user import User
from app.schemas.order import (
    OrderStatusEnum,
    OverrideOrderStatusRequest,
    UpdateOrderStatusRequest,
)
from app.services import order_workflow
from app.services.invoice_service import InvoiceService
from app.services.notification_service import NotificationService
from app.services.order_service import OrderService

# Allow base-role admins and staff accounts that hold orders:read.
# Write routes additionally require orders:update (declared per-route).
router = APIRouter(
    dependencies=[Depends(require_permission("orders", "read"))],
    tags=["Admin Orders"],
)

# Statuses whose transition triggers a customer notification.
_NOTIFY_STATUSES = {
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
        # Returns & refunds (Module 5.5)
        "return_reason": order.return_reason,
        "return_comments": order.return_comments,
        "return_items": order.return_items,
        "return_requested_at": order.return_requested_at.isoformat() if order.return_requested_at else None,
        "refund_amount": float(order.refund_amount) if order.refund_amount is not None else None,
        "customer": {
            "user_id": str(customer.user_id),
            "full_name": customer.full_name,
            "email": customer.email,
            "phone": customer.phone,
        } if customer else None,
        "delivery_address": {
            "address_line1": addr.address_line1,
            "address_line2": addr.address_line2,
            "post_office": addr.post_office,
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


@router.get("", response_model=dict)
async def list_orders(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    order_status: Optional[OrderStatusEnum] = Query(None, description="Filter by order status"),
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

    query = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.user))
        .join(User, Order.user_id == User.user_id)
    )

    if effective_branch is not None:
        query = query.where(Order.branch_id == effective_branch)
    if order_status is not None:
        query = query.where(Order.status == order_status.value)
    if search:
        pattern = f"%{search}%"
        query = query.where(
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
            query = query.where(Order.created_at >= from_dt)
        except ValueError:
            pass
    if to_date:
        try:
            to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
            query = query.where(Order.created_at < to_dt)
        except ValueError:
            pass

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
        "Branch", "Post Office", "Items Count", "Subtotal (LKR)", "Shipping (LKR)",
        "Discount (LKR)", "Total (LKR)", "Payment Method", "Payment Status", "Order Status",
    ])

    for o in orders:
        cust = o.user
        cust_name = cust.full_name if cust else "Customer"
        cust_email = cust.email if cust else ""
        cust_phone = cust.phone if cust else ""
        branch_name = branch_map.get(o.branch_id, "Unassigned")
        addr = o.delivery_address
        po = addr.post_office if addr else ""

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
    """Full order details (items, pricing, customer, delivery address)."""
    order = await _load_scoped_order(db, order_id, scope)
    branch_name = await _branch_name(db, order.branch_id)
    return {"success": True, "data": _format_order_detail(order, branch_name)}


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


@router.post("/{order_id}/return/approve", response_model=dict,
             dependencies=[Depends(require_permission("orders", "update"))])
async def approve_order_return(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Approve a pending return: credit the returned value to the customer's wallet
    and move the order to REFUNDED (atomic; branch-scoped).
    """
    order = await _load_scoped_order(db, order_id, scope)
    try:
        await OrderService.approve_return(db, order)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    branch_name = await _branch_name(db, order.branch_id)
    logger.info(f"[admin] Return approved for order {order.order_number}")
    return {
        "success": True,
        "data": _format_order_detail(order, branch_name),
        "message": "Return approved and customer wallet credited",
    }


@router.post("/{order_id}/return/reject", response_model=dict,
             dependencies=[Depends(require_permission("orders", "update"))])
async def reject_order_return(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Reject a pending return: revert the order to DELIVERED (branch-scoped)."""
    order = await _load_scoped_order(db, order_id, scope)
    try:
        await OrderService.reject_return(db, order)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    branch_name = await _branch_name(db, order.branch_id)
    logger.info(f"[admin] Return rejected for order {order.order_number}")
    return {
        "success": True,
        "data": _format_order_detail(order, branch_name),
        "message": "Return rejected",
    }


@router.post(
    "/reconcile-payments",
    response_model=dict,
    dependencies=[Depends(require_permission("orders", "update"))],
)
async def reconcile_marx_payments(db: AsyncSession = Depends(get_db)):
    """
    Manually trigger the Marx IPG reconciliation sweep (docs/COURIER_API.md's
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
