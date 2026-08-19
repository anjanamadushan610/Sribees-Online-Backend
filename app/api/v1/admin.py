"""
Admin API Endpoints
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_admin
from app.models.branch import Branch
from app.models.category import Category
from app.models.order import Order, OrderStatus, PaymentStatus
from app.models.product import Product
from app.models.user import User

# Prefix "/admin" is applied by app/api/v1/router.py — do not repeat it here.
router = APIRouter(tags=["Admin"])


# ============================================================================
# Dashboard Endpoints
# ============================================================================

@router.get("/dashboard", response_model=dict)
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Get admin dashboard statistics.
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


@router.get("/analytics/revenue", response_model=dict)
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


@router.get("/analytics/orders", response_model=dict)
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
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Get all customer users with filtering and pagination.

    Note: this operates on the ``users`` (customer) table. Admin-account
    management lives at ``/admin/users`` (see admin_users.py).
    """
    try:
        query = select(User)

        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                User.email.ilike(search_pattern) |
                User.full_name.ilike(search_pattern) |
                User.phone.ilike(search_pattern)
            )

        if role:
            query = query.where(User.role == role)

        # Count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar()

        # Get users
        offset = (page - 1) * limit
        query = query.order_by(User.created_at.desc()).limit(limit).offset(offset)
        result = await db.execute(query)
        users = result.scalars().all()

        return {
            "success": True,
            "data": {
                "users": [
                    {
                        "user_id": str(u.user_id),
                        "email": u.email,
                        "full_name": u.full_name,
                        "phone": u.phone,
                        "role": u.role,
                        "is_active": u.is_active,
                        "is_verified": u.is_verified,
                        "created_at": u.created_at.isoformat() if u.created_at else None,
                        "last_login": u.last_login.isoformat() if u.last_login else None
                    }
                    for u in users
                ],
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


@router.put("/customers/{user_id}/role", response_model=dict)
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


@router.put("/customers/{user_id}/status", response_model=dict)
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
# Inventory Management
# ============================================================================

@router.get("/inventory/low-stock", response_model=dict)
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


@router.get("/recent-orders", response_model=dict)
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
