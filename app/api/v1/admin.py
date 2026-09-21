"""
Admin API Endpoints
"""
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
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
from app.models.category import Category
from app.models.order import Order, OrderStatus, PaymentStatus
from app.models.product import Product
from app.models.user import Address, Session, User
from app.services import customer_insight_service

# Prefix "/admin" is applied by app/api/v1/router.py — do not repeat it here.
router = APIRouter(tags=["Admin"])


# ============================================================================
# Dashboard Endpoints
# ============================================================================

@router.get(
    "/dashboard",
    response_model=dict,
    dependencies=[Depends(require_roles("super_admin"))],
)
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Network-wide admin statistics (Super Admin only). Branch-scoped roles use
    ``GET /admin/dashboard/stats`` (admin_dashboard.py), which isolates every
    metric to the caller's branch and permission set.
    """
    try:
        # Total users
        users_result = await db.execute(select(func.count(User.user_id)))
        total_users = users_result.scalar() or 0

        # Total products
        products_result = await db.execute(
            select(func.count(Product.product_id)).where(Product.is_active == True)
        )
        total_products = products_result.scalar() or 0

        # Total categories
        categories_result = await db.execute(
            select(func.count(Category.category_id)).where(Category.is_active == True)
        )
        total_categories = categories_result.scalar() or 0

        # Total orders
        orders_result = await db.execute(select(func.count(Order.order_id)))
        total_orders = orders_result.scalar() or 0

        # Pending orders
        pending_orders_result = await db.execute(
            select(func.count(Order.order_id))
            .where(Order.status == OrderStatus.PENDING.value)
        )
        pending_orders = pending_orders_result.scalar() or 0

        # Total revenue (paid orders)
        revenue_result = await db.execute(
            select(func.sum(Order.total_amount))
            .where(Order.payment_status == PaymentStatus.PAID.value)
        )
        total_revenue = float(revenue_result.scalar() or 0)

        # Today's orders
        today = datetime.utcnow().date()
        today_orders_result = await db.execute(
            select(func.count(Order.order_id))
            .where(func.date(Order.created_at) == today)
        )
        today_orders = today_orders_result.scalar() or 0

        # Today's revenue
        today_revenue_result = await db.execute(
            select(func.sum(Order.total_amount))
            .where(
                func.date(Order.created_at) == today,
                Order.payment_status == PaymentStatus.PAID.value
            )
        )
        today_revenue = float(today_revenue_result.scalar() or 0)

        # Low stock products
        low_stock_result = await db.execute(
            select(func.count(Product.product_id))
            .where(
                Product.is_active == True,
                Product.stock_quantity <= Product.low_stock_threshold
            )
        )
        low_stock_products = low_stock_result.scalar() or 0

        return {
            "success": True,
            "data": {
                "users": {
                    "total": total_users
                },
                "products": {
                    "total": total_products,
                    "low_stock": low_stock_products
                },
                "categories": {
                    "total": total_categories
                },
                "orders": {
                    "total": total_orders,
                    "pending": pending_orders,
                    "today": today_orders
                },
                "revenue": {
                    "total": total_revenue,
                    "today": today_revenue
                }
            }
        }
    except Exception as e:
        logger.error(f"Error getting dashboard stats: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get dashboard statistics"
        )


@router.get(
    "/analytics/revenue",
    response_model=dict,
    dependencies=[Depends(require_roles("super_admin"))],
)
async def get_revenue_analytics(
    period: str = Query("7d", pattern="^(7d|30d|90d|1y)$"),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Get revenue analytics for the specified period.
    """
    try:
        # Calculate date range
        periods = {
            "7d": 7,
            "30d": 30,
            "90d": 90,
            "1y": 365
        }
        days = periods.get(period, 7)
        start_date = datetime.utcnow() - timedelta(days=days)

        # Get daily revenue
        result = await db.execute(
            select(
                func.date(Order.created_at).label("date"),
                func.sum(Order.total_amount).label("revenue"),
                func.count(Order.order_id).label("orders")
            )
            .where(
                Order.created_at >= start_date,
                Order.payment_status == PaymentStatus.PAID.value
            )
            .group_by(func.date(Order.created_at))
            .order_by(func.date(Order.created_at))
        )

        daily_data = [
            {
                "date": row.date.isoformat() if row.date else None,
                "revenue": float(row.revenue or 0),
                "orders": row.orders or 0
            }
            for row in result.all()
        ]

        # Calculate totals
        total_revenue = sum(d["revenue"] for d in daily_data)
        total_orders = sum(d["orders"] for d in daily_data)
        avg_order_value = total_revenue / total_orders if total_orders > 0 else 0

        return {
            "success": True,
            "data": {
                "period": period,
                "daily": daily_data,
                "summary": {
                    "total_revenue": round(total_revenue, 2),
                    "total_orders": total_orders,
                    "average_order_value": round(avg_order_value, 2)
                }
            }
        }
    except Exception as e:
        logger.error(f"Error getting revenue analytics: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get revenue analytics"
        )


@router.get(
    "/analytics/orders",
    response_model=dict,
    dependencies=[Depends(require_roles("super_admin"))],
)
async def get_order_analytics(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Get order status distribution.
    """
    try:
        # Get order counts by status
        result = await db.execute(
            select(
                Order.status,
                func.count(Order.order_id).label("count")
            )
            .group_by(Order.status)
        )

        status_counts = {row.status: row.count for row in result.all()}

        # Get payment status counts
        payment_result = await db.execute(
            select(
                Order.payment_status,
                func.count(Order.order_id).label("count")
            )
            .group_by(Order.payment_status)
        )

        payment_counts = {row.payment_status: row.count for row in payment_result.all()}

        return {
            "success": True,
            "data": {
                "by_status": status_counts,
                "by_payment_status": payment_counts
            }
        }
    except Exception as e:
        logger.error(f"Error getting order analytics: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get order analytics"
        )


# ============================================================================
# User Management
# ============================================================================

@router.get("/customers", response_model=dict)
async def get_customers(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    role: Optional[str] = None,
    segment: Optional[str] = Query(
        None,
        pattern="^(returning|new|at_risk)$",
        description=(
            "Behavioural segment. Derived from order history at read time "
            "(see CustomerInsightService), so it is never stale."
        ),
    ),
    is_blocked: Optional[bool] = Query(
        None, description="Filter on account status (the directory's Active/Blocked tabs)"
    ),
    db: AsyncSession = Depends(get_db),
    _perm = Depends(require_permission("customers", "read")),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Customer directory (the ``users`` table). Branch-isolated: a scoped admin
    (Branch Manager + their staff, Marketing/Inventory Manager) only sees
    customers who have an order fulfilled by their branch; Super Admin and
    Customer Support see everyone. Admin-account management is /admin/users.
    """
    try:
        query = select(User)

        if not scope.is_unscoped:
            branch_customer_ids = (
                select(Order.user_id).where(Order.branch_id == scope.branch_id).distinct()
            )
            query = query.where(User.user_id.in_(branch_customer_ids))

        if search:
            search_pattern = f"%{search.strip()}%"
            query = query.where(
                User.email.ilike(search_pattern) |
                User.full_name.ilike(search_pattern) |
                User.phone.ilike(search_pattern) |
                # The NIC is what support is given over the phone when
                # someone asks about their account, so it has to be
                # searchable here or it is not usable as an identity.
                User.nic.ilike(search_pattern) |
                User.alternate_phone.ilike(search_pattern)
            )

        if role:
            query = query.where(User.role == role)

        if is_blocked is not None:
            query = query.where(User.is_blocked.is_(is_blocked))

        # Count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar()

        # Get users
        offset = (page - 1) * limit
        query = query.order_by(User.created_at.desc()).limit(limit).offset(offset)
        result = await db.execute(query)
        users = result.scalars().all()

        # Purchasing history for the whole page in one grouped query, scoped
        # to this admin's branch. Net spend counts DELIVERED orders only —
        # refunded money is not revenue from that customer, and this page used
        # to imply it was.
        stats_map = await customer_insight_service.stats_for_users(
            db,
            [u.user_id for u in users],
            None if scope.is_unscoped else scope.branch_id,
        )

        rows = []
        for u in users:
            stats = stats_map.get(u.user_id) or customer_insight_service.CustomerStats(
                user_id=u.user_id
            )
            rows.append(
                {
                    "user_id": str(u.user_id),
                    "email": u.email,
                    "full_name": u.full_name,
                    "phone": u.phone,
                    "nic": u.nic,
                    "alternate_phone": u.alternate_phone,
                    "role": u.role,
                    "is_active": u.is_active,
                    "is_verified": u.is_verified,
                    "is_blocked": u.is_blocked,
                    "blocked_reason": u.blocked_reason,
                    "created_at": u.created_at.isoformat() if u.created_at else None,
                    "last_login": u.last_login.isoformat() if u.last_login else None,
                    **stats.as_dict(u.created_at),
                }
            )

        # Segment is derived from order history, not stored, so it cannot be a
        # SQL predicate without reimplementing the rules in two places. Applied
        # to the page in Python instead; the page-size cap bounds the cost, and
        # there is exactly one definition of what "at risk" means.
        if segment:
            rows = [r for r in rows if r["segment"] == segment]

        return {
            "success": True,
            "data": {
                "users": rows,
                "pagination": {
                    "total": total,
                    "page": page,
                    "limit": limit,
                    "pages": (total + limit - 1) // limit
                }
            }
        }
    except Exception as e:
        logger.error(f"Error getting users: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get users"
        )


@router.put(
    "/customers/{user_id}/role",
    response_model=dict,
    dependencies=[Depends(require_roles("super_admin"))],
)
async def update_customer_role(
    user_id: str,
    role: str = Query(..., pattern="^(customer|admin|manager)$"),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Update a customer user's role.
    """
    try:
        from uuid import UUID
        user_uuid = UUID(user_id)

        result = await db.execute(
            select(User).where(User.user_id == user_uuid)
        )
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        user.role = role
        await db.commit()

        return {
            "success": True,
            "message": f"User role updated to {role}"
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user ID"
        )
    except Exception as e:
        logger.error(f"Error updating user role: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user role"
        )


@router.put(
    "/customers/{user_id}/status",
    response_model=dict,
    dependencies=[Depends(require_roles("super_admin", "customer_support"))],
)
async def toggle_customer_status(
    user_id: str,
    is_active: bool,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Enable or disable a customer user account.
    """
    try:
        from uuid import UUID
        user_uuid = UUID(user_id)

        result = await db.execute(
            select(User).where(User.user_id == user_uuid)
        )
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        user.is_active = is_active
        await db.commit()

        status_text = "enabled" if is_active else "disabled"
        return {
            "success": True,
            "message": f"User account {status_text}"
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user ID"
        )
    except Exception as e:
        logger.error(f"Error toggling user status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user status"
        )


# ============================================================================
# Customer Action Schemas & Routes
# ============================================================================


class UpdateCustomerPayload(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=100)
    email: Optional[str] = None
    phone: Optional[str] = None
    # A customer cannot rewrite their own NIC (see
    # AuthService.update_profile). This is the escape hatch that guard
    # assumes exists: support correcting a mistyped one.
    nic: Optional[str] = None
    alternate_phone: Optional[str] = None

    @field_validator("nic")
    @classmethod
    def validate_nic(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        from app.utils.identity import normalize_nic

        return normalize_nic(v)

    @field_validator("alternate_phone")
    @classmethod
    def validate_alternate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        from app.utils.identity import normalize_mobile

        return normalize_mobile(v)

class BlockCustomerPayload(BaseModel):
    reason: str = Field(..., min_length=1)


@router.get("/customers/{user_id}/profile", response_model=dict)
async def get_customer_profile(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _perm = Depends(require_permission("customers", "read")),
    scope: BranchScope = Depends(inject_branch_filter),
):
    try:
        user_uuid = UUID(user_id)

        # Get user with addresses
        result = await db.execute(
            select(User)
            .options(selectinload(User.addresses))
            .where(User.user_id == user_uuid)
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Branch isolation: a scoped admin may only see a customer who has an
        # order in their branch, and gets branch-scoped lifetime numbers.
        if not scope.is_unscoped:
            in_branch = await db.scalar(
                select(func.count(Order.order_id)).where(
                    Order.user_id == user_uuid, Order.branch_id == scope.branch_id
                )
            )
            if not in_branch:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
                )

        # Lifetime value, from the same service the directory list uses so the
        # two screens can never disagree.
        #
        # This previously summed every order that was not literally 'cancelled',
        # which counted REFUNDED and returned orders as money the customer had
        # spent. A customer whose only order was refunded showed as a paying
        # customer, and any retention decision made from that figure was made
        # from a fiction.
        stats = await customer_insight_service.stats_for_user(
            db, user_uuid, None if scope.is_unscoped else scope.branch_id
        )
        total_orders = stats.completed_orders
        total_spent = stats.net_spent

        return {
            "success": True,
            "data": {
                "user_id": str(user.user_id),
                "email": user.email,
                "full_name": user.full_name,
                "phone": user.phone,
                "nic": user.nic,
                "alternate_phone": user.alternate_phone,
                "is_active": user.is_active,
                "is_blocked": user.is_blocked,
                "blocked_reason": user.blocked_reason,
                "is_verified": user.is_verified,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "last_login": user.last_login.isoformat() if user.last_login else None,
                "addresses": [
                    {
                        "address_id": str(addr.address_id),
                        "address_line1": addr.address_line1,
                        "address_line2": addr.address_line2,
                        "postal_city": addr.postal_city,
                        "district": addr.district,
                        "postal_code": addr.postal_code,
                        "province": addr.province,
                        "is_default": addr.is_default,
                    }
                    for addr in user.addresses
                ],
                "stats": {
                    # Kept under the original names for existing callers;
                    # both now mean DELIVERED-only.
                    "total_orders": total_orders,
                    "total_spent": total_spent,
                    **stats.as_dict(user.created_at),
                }
            }
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID")
    except Exception as e:
        logger.error(f"Error getting customer profile: {e}")
        raise HTTPException(status_code=500, detail="Failed to get profile")


@router.get("/customers/{user_id}/orders", response_model=dict)
async def get_customer_orders(
    user_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    _perm = Depends(require_permission("customers", "read")),
    scope: BranchScope = Depends(inject_branch_filter),
):
    try:
        user_uuid = UUID(user_id)

        # Branch isolation: a scoped admin only sees this customer's orders that
        # their own branch fulfilled.
        base_filter = [Order.user_id == user_uuid]
        if not scope.is_unscoped:
            base_filter.append(Order.branch_id == scope.branch_id)

        # Get orders count
        count_result = await db.execute(
            select(func.count(Order.order_id)).where(*base_filter)
        )
        total = count_result.scalar() or 0

        # Get orders
        offset = (page - 1) * limit
        orders_result = await db.execute(
            select(Order)
            .where(*base_filter)
            .order_by(Order.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        orders = orders_result.scalars().all()

        return {
            "success": True,
            "data": {
                "orders": [
                    {
                        "order_id": str(o.order_id),
                        "order_number": o.order_number,
                        "total_amount": float(o.total_amount),
                        "status": o.status,
                        "payment_status": o.payment_status,
                        "created_at": o.created_at.isoformat() if o.created_at else None
                    }
                    for o in orders
                ],
                "pagination": {
                    "total": total,
                    "page": page,
                    "limit": limit,
                    "pages": (total + limit - 1) // limit
                }
            }
        }
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID")
    except Exception as e:
        logger.error(f"Error getting customer orders: {e}")
        raise HTTPException(status_code=500, detail="Failed to get customer orders")


@router.put(
    "/customers/{user_id}",
    response_model=dict,
    dependencies=[Depends(require_roles("super_admin", "customer_support"))],
)
async def update_customer(
    user_id: str,
    payload: UpdateCustomerPayload,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    try:
        from uuid import UUID
        user_uuid = UUID(user_id)

        result = await db.execute(
            select(User).where(User.user_id == user_uuid)
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        if payload.email:
            email_check = await db.execute(
                select(User).where(User.email == payload.email, User.user_id != user_uuid)
            )
            if email_check.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email address is already in use by another customer"
                )
            user.email = payload.email

        if payload.phone:
            phone_check = await db.execute(
                select(User).where(User.phone == payload.phone, User.user_id != user_uuid)
            )
            if phone_check.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Phone number is already in use by another customer"
                )
            user.phone = payload.phone

        if payload.nic and payload.nic != user.nic:
            nic_check = await db.execute(
                select(User).where(
                    User.nic == payload.nic, User.user_id != user_uuid
                )
            )
            if nic_check.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="This NIC is already registered to another customer",
                )
            user.nic = payload.nic

        if payload.alternate_phone is not None:
            if payload.alternate_phone == user.phone:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="The second number must differ from the main number",
                )
            user.alternate_phone = payload.alternate_phone

        user.full_name = payload.full_name
        await db.commit()

        return {
            "success": True,
            "message": "Customer profile updated successfully",
            "data": {
                "user_id": str(user.user_id),
                "email": user.email,
                "full_name": user.full_name,
                "phone": user.phone,
                "nic": user.nic,
                "alternate_phone": user.alternate_phone,
            }
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user ID"
        )
    except Exception as e:
        logger.error(f"Error updating customer: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update customer"
        )


@router.post(
    "/customers/{user_id}/block",
    response_model=dict,
    dependencies=[Depends(require_roles("super_admin", "customer_support"))],
)
async def block_customer(
    user_id: str,
    payload: BlockCustomerPayload,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    try:
        from uuid import UUID
        user_uuid = UUID(user_id)

        result = await db.execute(
            select(User).where(User.user_id == user_uuid)
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        user.is_blocked = True
        user.blocked_reason = payload.reason
        user.is_active = False  # Deactivate as well for safety

        # Terminate active sessions
        await db.execute(
            delete(Session).where(Session.user_id == user_uuid)
        )

        await db.commit()

        return {
            "success": True,
            "message": "Customer account blocked successfully."
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user ID"
        )
    except Exception as e:
        logger.error(f"Error blocking customer: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to block customer"
        )


@router.post(
    "/customers/{user_id}/unblock",
    response_model=dict,
    dependencies=[Depends(require_roles("super_admin", "customer_support"))],
)
async def unblock_customer(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    try:
        user_uuid = UUID(user_id)

        result = await db.execute(
            select(User).where(User.user_id == user_uuid)
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        user.is_blocked = False
        user.blocked_reason = None
        user.is_active = True  # Reactivate
        await db.commit()

        return {
            "success": True,
            "message": "Customer account unblocked successfully."
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user ID"
        )
    except Exception as e:
        logger.error(f"Error unblocking customer: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to unblock customer"
        )


@router.delete(
    "/customers/{user_id}",
    response_model=dict,
    dependencies=[Depends(require_roles("super_admin"))],
)
async def delete_customer(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    try:
        user_uuid = UUID(user_id)

        # Check if user exists
        result = await db.execute(
            select(User).where(User.user_id == user_uuid)
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Check if user has orders
        order_check = await db.execute(
            select(func.count(Order.order_id)).where(Order.user_id == user_uuid)
        )
        has_orders = (order_check.scalar() or 0) > 0

        if not has_orders:
            # Hard delete
            await db.delete(user)
            await db.commit()
            return {
                "success": True,
                "message": "User account and associated data permanently deleted."
            }
        else:
            # Anonymize (Soft delete)
            user.full_name = "Deleted Account"
            user.email = f"deleted_user_{user_id}@sribeesonline.lk"
            user.phone = None
            user.password_hash = None
            user.profile_picture_url = None
            user.is_active = False
            user.is_blocked = True
            user.blocked_reason = "Account Anonymized / Deleted"

            # Delete addresses
            await db.execute(
                delete(Address).where(Address.user_id == user_uuid)
            )

            # Delete sessions
            await db.execute(
                delete(Session).where(Session.user_id == user_uuid)
            )

            await db.commit()
            return {
                "success": True,
                "message": "User account anonymized successfully. Order history preserved for accounting."
            }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user ID"
        )
    except Exception as e:
        logger.error(f"Error deleting customer: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete customer account"
        )


# ============================================================================
# Inventory Management
# ============================================================================

@router.get(
    "/inventory/low-stock",
    response_model=dict,
    dependencies=[Depends(require_roles("super_admin"))],
)
async def get_low_stock_products(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Get products with low stock.
    """
    try:
        result = await db.execute(
            select(Product)
            .where(
                Product.is_active == True,
                Product.stock_quantity <= Product.low_stock_threshold
            )
            .order_by(Product.stock_quantity.asc())
            .limit(limit)
        )
        products = result.scalars().all()

        return {
            "success": True,
            "data": {
                "products": [
                    {
                        "product_id": str(p.product_id),
                        "name": p.name,
                        "sku": p.sku,
                        "stock_quantity": p.stock_quantity,
                        "low_stock_threshold": p.low_stock_threshold
                    }
                    for p in products
                ],
                "count": len(products)
            }
        }
    except Exception as e:
        logger.error(f"Error getting low stock products: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get low stock products"
        )


@router.get(
    "/recent-orders",
    response_model=dict,
    dependencies=[Depends(require_roles("super_admin"))],
)
async def get_recent_orders(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Get recent orders for admin dashboard.
    """
    try:
        from sqlalchemy.orm import selectinload

        result = await db.execute(
            select(Order)
            .options(selectinload(Order.user))
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
        orders = result.scalars().all()

        return {
            "success": True,
            "data": {
                "orders": [
                    {
                        "order_id": str(o.order_id),
                        "order_number": o.order_number,
                        "customer": {
                            "user_id": str(o.user.user_id),
                            "name": f"{o.user.first_name} {o.user.last_name}".strip(),
                            "email": o.user.email
                        } if o.user else None,
                        "total_amount": float(o.total_amount),
                        "status": o.status,
                        "payment_status": o.payment_status,
                        "created_at": o.created_at.isoformat() if o.created_at else None
                    }
                    for o in orders
                ]
            }
        }
    except Exception as e:
        logger.error(f"Error getting recent orders: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get recent orders"
        )


@router.get("/branch-options", response_model=dict)
async def list_branch_options(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin),
):
    """
    Every active branch's id/name/code — for any admin-facing branch picker
    (stock transfer From/To, the Orders branch filter, etc). This router has
    no role restriction beyond being a signed-in admin, unlike `/admin/branches`
    (Super Admin only, full branch management) — nothing returned here is
    sensitive that isn't already shown elsewhere in the UI to every role.
    """
    rows = (
        await db.execute(
            select(Branch.branch_id, Branch.name, Branch.code)
            .where(Branch.is_active.is_(True))
            .order_by(Branch.name)
        )
    ).all()
    return {
        "success": True,
        "data": {
            "branches": [{"branch_id": str(r.branch_id), "name": r.name, "code": r.code} for r in rows]
        },
    }
