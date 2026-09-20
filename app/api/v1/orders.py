"""
Order API Endpoints
"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis
from app.config.settings import settings
from app.core.dependencies import get_current_admin, get_current_user
from app.models.order import OrderStatus
from app.schemas.order import (
    CreateOrderRequest,
    OrderQuoteResponse,
    QuoteOrderRequest,
    ReturnRequest,
    UpdateOrderStatusRequest,
    UpdatePaymentStatusRequest,
    to_courier_payment_method,
)
from app.services import (
    branch_service,
    courier_sync_service,
    delivery_quote_service,
)
from app.services.app_settings_service import AppSettingsService
from app.services.cart_service import CartService
from app.services.invoice_service import InvoiceService
from app.services.order_service import OrderService
from app.services.pricing_service import PricingService
from app.services.product_review_service import ProductReviewService
from app.services.wallet_service import WalletService

# Prefix "/orders" is applied by app/api/v1/router.py — do not repeat it here.
router = APIRouter(tags=["Orders"])


# ============================================================================
# Helper Functions
# ============================================================================

# Normal forward progression of a fulfilled order. `processing` (the legacy
# pre-workflow state) is folded onto `packing` — see _effective_status.
_STATUS_FLOW = [
    ("pending", "Order Placed"),
    ("confirmed", "Confirmed"),
    ("packing", "Preparing Order"),
    ("packed", "Packed"),
    ("ready_for_pickup", "Waiting for Courier"),
    ("handed_to_courier", "Handed to Courier"),
    ("shipped", "Shipped"),
    ("out_for_delivery", "Out for Delivery"),
    ("delivered", "Delivered"),
]

# Known timestamp column per status (others are inferred, timestamp None).
_STATUS_TIMESTAMPS = {
    "pending": "created_at",
    "packed": "packed_at",
    "ready_for_pickup": "ready_for_pickup_at",
    "handed_to_courier": "handed_to_courier_at",
    "shipped": "shipped_at",
    "delivered": "delivered_at",
    "cancelled": "cancelled_at",
}

# Orders placed before the packing/packed/handed_to_courier states existed sit
# in `processing`; render them at the `packing` point of the timeline.
_LEGACY_STATUS_ALIASES = {"processing": "packing"}


def _effective_status(status: str) -> str:
    return _LEGACY_STATUS_ALIASES.get(status, status)


def build_status_timeline(order) -> list:
    """
    Build an ordered status timeline for the order.

    There is no dedicated status-history table, so the timeline is derived from
    the order's current status and the timestamp columns it does have
    (created_at, shipped_at, delivered_at, cancelled_at). Each step reports
    whether it is completed and which one is current.
    """
    def ts(field: str):
        value = getattr(order, field, None)
        return value.isoformat() if value else None

    # Terminal states short-circuit the normal flow.
    if order.status in ("cancelled", "refunded"):
        label = "Cancelled" if order.status == "cancelled" else "Refunded"
        return [
            {
                "status": "pending",
                "label": "Order Placed",
                "timestamp": ts("created_at"),
                "completed": True,
                "current": False,
            },
            {
                "status": order.status,
                "label": label,
                "timestamp": ts(_STATUS_TIMESTAMPS.get(order.status, "updated_at")),
                "completed": True,
                "current": True,
            },
        ]

    # Return flow: delivered → return requested/approved.
    if order.status in ("return_requested", "return_approved"):
        label = (
            "Return Requested"
            if order.status == "return_requested"
            else "Return Approved"
        )
        return [
            {
                "status": "pending",
                "label": "Order Placed",
                "timestamp": ts("created_at"),
                "completed": True,
                "current": False,
            },
            {
                "status": "delivered",
                "label": "Delivered",
                "timestamp": ts("delivered_at"),
                "completed": True,
                "current": False,
            },
            {
                "status": order.status,
                "label": label,
                "timestamp": ts("return_requested_at"),
                "completed": True,
                "current": True,
            },
        ]

    flow_keys = [s for s, _ in _STATUS_FLOW]
    effective = _effective_status(order.status)
    current_rank = flow_keys.index(effective) if effective in flow_keys else 0

    timeline = []
    for rank, (status_key, label) in enumerate(_STATUS_FLOW):
        timestamp_field = _STATUS_TIMESTAMPS.get(status_key)
        timeline.append({
            "status": status_key,
            "label": label,
            "timestamp": ts(timestamp_field) if timestamp_field else None,
            "completed": rank <= current_rank,
            "current": rank == current_rank,
        })
    return timeline


def format_order(
    order,
    include_items: bool = True,
    payment_redirect_url: Optional[str] = None,
    reviewed_product_ids: Optional[set] = None,
) -> dict:
    """Format order for API response.

    reviewed_product_ids (only meaningful/populated by the caller for a
    delivered order): product ids the requesting user already has a review
    for, so mobile can show "✓ Reviewed" per line item instead of a
    write-review action without a second round trip.
    """
    data = {
        "order_id": str(order.order_id),
        "order_number": order.order_number,
        "user_id": str(order.user_id),
        "subtotal": float(order.subtotal),
        "tax_amount": float(order.tax_amount),
        "shipping_amount": float(order.shipping_amount),
        "discount_amount": float(order.discount_amount),
        "wallet_deduction": float(order.wallet_deduction or 0),
        "cashback_earned": float(order.cashback_earned or 0),
        "total_amount": float(order.total_amount),
        "status": order.status,
        "payment_status": order.payment_status,
        "payment_method": order.payment_method,
        "payment_id": order.payment_id,
        # Marx IPG gateway — payment_redirect_url is response-only, never persisted.
        "payment_redirect_url": payment_redirect_url,
        "gateway_status": order.gateway_status,
        # Delivery handover verification code (non-COD orders only)
        "delivery_verification_code": order.delivery_verification_code,
        "delivery_verification_status": order.delivery_verification_status,
        # Courier tracking
        "courier_tracking_status": order.courier_tracking_status,
        "courier_waybill_number": order.courier_waybill_number,
        "delivery_slot_date": order.delivery_slot_date.isoformat() if order.delivery_slot_date else None,
        "delivery_slot_time": order.delivery_slot_time,
        "coupon_code": order.coupon_code,
        "notes": order.notes,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
        "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "cancelled_at": order.cancelled_at.isoformat() if order.cancelled_at else None,
        # Returns & refunds (Module 5.5)
        "return_reason": order.return_reason,
        "return_comments": order.return_comments,
        "return_requested_at": order.return_requested_at.isoformat() if order.return_requested_at else None,
        "refund_amount": float(order.refund_amount) if order.refund_amount is not None else None,
        # When the 12-hour window closes, so the app can show a live countdown
        # rather than re-deriving the rule — and so a customer learns they are
        # out of time *before* tapping, not by being rejected. Null means the
        # order was never delivered (or predates delivered_at being recorded),
        # in which case the app offers no return at all.
        "return_window_expires_at": (
            expires.isoformat()
            if (expires := OrderService.return_window_expires_at(order)) is not None
            else None
        ),
        # The reverse pickup, once a Branch Manager has approved one. The app
        # renders these as a second tracker under the delivered order.
        "return_waybill_number": order.return_waybill_number,
        "return_tracking_status": order.return_tracking_status,
        "return_collected_at": (
            order.return_collected_at.isoformat() if order.return_collected_at else None
        ),
        "return_received_at": (
            order.return_received_at.isoformat() if order.return_received_at else None
        ),
        "refunded_at": order.refunded_at.isoformat() if order.refunded_at else None,
        # Deliberately absent: `return_handover_code`. That is ours to read to
        # the rider at the counter, not the customer's to see — a customer who
        # can read it can close a return the goods never arrived for.
    }

    # Status timeline (derived — no dedicated history table)
    data["status_timeline"] = build_status_timeline(order)

    # Delivery address (matches the Address model + DeliveryAddressSchema)
    if order.delivery_address:
        addr = order.delivery_address
        data["delivery_address"] = {
            "address_id": str(addr.address_id),
            "address_line1": addr.address_line1,
            "address_line2": addr.address_line2,
            "postal_city": addr.postal_city,
            "post_office": addr.postal_city,  # legacy key: installed app builds read it
            "district": addr.district,
            "province": addr.province,
            "postal_code": addr.postal_code,
        }
    else:
        data["delivery_address"] = None

    # Items
    if include_items and order.items:
        reviewed = reviewed_product_ids or set()
        data["items"] = [
            {
                "order_item_id": str(item.order_item_id),
                "product_id": str(item.product_id),
                "variant_id": str(item.variant_id) if item.variant_id else None,
                "product_name": item.product_name,
                "product_sku": item.product_sku,
                "product_image": item.product_image,
                "quantity": item.quantity,
                "unit_price": float(item.unit_price),
                "subtotal": float(item.subtotal),
                "tax_amount": float(item.tax_amount) if item.tax_amount else 0,
                "is_reviewed": item.product_id in reviewed,
            }
            for item in order.items
        ]
        data["item_count"] = sum(item.quantity for item in order.items)
    else:
        data["items"] = []
        data["item_count"] = 0

    return data


def format_order_summary(order) -> dict:
    """Format order summary for list views.

    Includes a compact per-item preview (image/name/qty/price) so the order
    history list can show product photos and let a customer tell orders
    apart without opening each one — order.items is already eager-loaded by
    OrderService.get_user_orders (selectinload), so this is free, no extra
    query per order.
    """
    return {
        "order_id": str(order.order_id),
        "order_number": order.order_number,
        "status": order.status,
        "payment_status": order.payment_status,
        "total_amount": float(order.total_amount),
        "cashback_earned": float(order.cashback_earned or 0),
        "item_count": sum(item.quantity for item in order.items) if order.items else 0,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        # The handover code the rider will ask for, so the Orders list can put
        # it in front of the customer at the moment it matters (a rider at the
        # door) instead of making them open the order and hunt for it.
        #
        # Only while it is still live: once verification is confirmed or
        # failed, the code has done its job and repeating it in every list
        # response is exposure that buys nothing. A COD order never has one.
        # This list is already scoped to the requesting user by
        # OrderService.get_user_orders, so this is the same trust boundary the
        # detail endpoint already sits behind, not a wider one.
        "delivery_verification_code": (
            order.delivery_verification_code
            if order.delivery_verification_status == "pending"
            else None
        ),
        "delivery_verification_status": order.delivery_verification_status,
        "courier_tracking_status": order.courier_tracking_status,
        "items": [
            {
                "product_id": str(item.product_id),
                "product_name": item.product_name,
                "product_image": item.product_image,
                "quantity": item.quantity,
                "unit_price": float(item.unit_price),
                "subtotal": float(item.subtotal),
            }
            for item in order.items
        ] if order.items else [],
    }


# ============================================================================
# User Endpoints
# ============================================================================

@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_order(
    data: CreateOrderRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Create a new order from cart.
    """
    try:
        order, payment_redirect_url = await OrderService.create_from_cart(
            db,
            user_id=current_user.user_id,
            data=data
        )

        # Reload with relationships
        order = await OrderService.get_by_id(db, order.order_id)

        message = (
            "Redirect the customer to payment_redirect_url to complete payment"
            if payment_redirect_url
            else "Order placed successfully"
        )
        return {
            "success": True,
            "data": format_order(order, payment_redirect_url=payment_redirect_url),
            "message": message
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error creating order: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create order"
        )


@router.post("/quote", response_model=OrderQuoteResponse)
async def quote_order(
    data: QuoteOrderRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user = Depends(get_current_user)
):
    """
    Preview the exact, server-authoritative order totals **without** creating
    an order or mutating any state.

    Uses the same `PricingService` as order creation, and the same SribeesExpress
    delivery quote the cart shows, so the returned figures match what the
    customer will be charged on checkout. ``delivery_available: false`` (with
    ``delivery_status`` and ``delivery_message``) means checkout will be refused
    until the reason is fixed — there is no flat delivery fee behind it.
    """
    try:
        cart = await CartService.get_cart(str(current_user.user_id))
        wallet_balance = await WalletService.get_balance(db, current_user.user_id)
        pricing_config = await AppSettingsService.get_pricing_config(db)
        payment_method = to_courier_payment_method(data.payment_method)

        quote_items = (
            data.items
            if (data.items is not None and len(data.items) > 0)
            else cart.get("items", [])
        )
        from app.services.coupon_service import CouponService
        quote_coupon = None
        coupon_code_to_check = (
            data.coupon_code.strip() if data.coupon_code else None
        )
        if not coupon_code_to_check and not data.is_buy_now:
            cart_coupon = cart.get("coupon")
            if cart_coupon and cart_coupon.get("code"):
                coupon_code_to_check = cart_coupon.get("code")

        if coupon_code_to_check:
            coupon_row = await CouponService.get_by_code(db, coupon_code_to_check)
            if coupon_row:
                quote_coupon = {
                    "code": coupon_row.code,
                    "discount_type": coupon_row.discount_type,
                    "discount_value": float(coupon_row.discount_value),
                    "min_order_value": float(coupon_row.min_order_value or 0),
                    "max_discount_amount": float(coupon_row.max_discount_amount) if coupon_row.max_discount_amount is not None else None,
                }

        # The delivery quote and the fulfilling branch are resolved the SAME
        # way create_from_cart resolves them: from the chosen delivery
        # address's postal city (or the shopper's default address before one
        # is chosen), priced in that branch's courier account.
        delivery, destination = await delivery_quote_service.quote_for_cart(
            db,
            redis,
            current_user.user_id,
            quote_items,
            address_id=data.delivery_address_id,
            payment_method=payment_method,
        )

        branch_id = destination.branch_id
        if branch_id is None:
            context = await branch_service.get_branch_context(
                redis, str(current_user.user_id)
            )
            if context is not None:
                try:
                    branch_id = UUID(context["branch_id"])
                except (KeyError, ValueError, TypeError):
                    branch_id = None

        items = await OrderService._attach_cashback_rates(
            db, quote_items, branch_id
        )

        if quote_coupon and coupon_row:
            enriched_for_coupon = await CouponService.enrich_items(db, items, branch_id)
            quote_coupon["eligible_subtotal"] = float(
                CouponService.eligible_subtotal(coupon_row, enriched_for_coupon)
            )

        breakdown = await PricingService.quote(
            items=items,
            coupon=quote_coupon,
            use_wallet=data.use_wallet,
            wallet_balance=wallet_balance,
            tax_rate=pricing_config["tax_rate"],
            payment_method=payment_method,
            wallet_amount=data.wallet_amount,
            delivery=delivery,
        )

        item_count = sum(i.get("quantity", 0) for i in quote_items)

        # Blended rate for display: with per-product cashback there is no single
        # platform rate to quote, so report what this cart actually earns.
        effective_rate = (
            float(breakdown.cashback_earned / breakdown.subtotal)
            if breakdown.subtotal > 0
            else settings.cashback_rate
        )

        return OrderQuoteResponse(
            data={
                **breakdown.as_dict(),
                "currency": settings.wallet_currency,
                "cashback_rate": effective_rate,
                "wallet_balance": float(wallet_balance),
                "wallet_applied": breakdown.wallet_deduction > 0,
                "item_count": item_count,
                "delivery": delivery_quote_service.delivery_payload(delivery, destination),
            }
        )
    except Exception as e:
        logger.error(f"Error building order quote: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build order quote"
        )


@router.get("", response_model=dict)
async def get_my_orders(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get current user's orders.
    """
    try:
        offset = (page - 1) * limit
        orders, total = await OrderService.get_user_orders(
            db,
            user_id=current_user.user_id,
            limit=limit,
            offset=offset,
            status=status
        )

        return {
            "success": True,
            "data": {
                "orders": [format_order_summary(o) for o in orders],
                "pagination": {
                    "total": total,
                    "page": page,
                    "limit": limit,
                    "pages": (total + limit - 1) // limit
                }
            }
        }
    except Exception as e:
        logger.error(f"Error fetching orders: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch orders"
        )


@router.get("/{order_id}", response_model=dict)
async def get_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get order details.
    """
    try:
        # Try by ID first, then by order number
        try:
            order_uuid = UUID(order_id)
            order = await OrderService.get_by_id(db, order_uuid, current_user.user_id)
        except ValueError:
            order = await OrderService.get_by_order_number(db, order_id, current_user.user_id)

        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found"
            )

        # Only a delivered order can show write-review actions at all, so
        # only bother looking up existing reviews for one.
        reviewed_product_ids = set()
        if order.status == OrderStatus.DELIVERED.value and order.items:
            reviewed_product_ids = await ProductReviewService.get_reviewed_product_ids(
                db, current_user.user_id, [item.product_id for item in order.items]
            )

        return {
            "success": True,
            "data": format_order(order, reviewed_product_ids=reviewed_product_ids)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching order: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch order"
        )


@router.get("/{order_id}/tracking", response_model=dict)
async def get_order_tracking(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Live courier tracking status (docs/SRIBEESEXPRESS_ECOMMERCE_API.md section 8).

    Throttled to at most once per 60s per order — returns the cached
    ``courier_tracking_status`` if it was refreshed within that window instead
    of hammering the courier on every screen open.
    """
    try:
        order_uuid = UUID(order_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid order ID")

    order = await OrderService.get_by_id(db, order_uuid, current_user.user_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    if not order.courier_shipment_id:
        return {
            "success": True,
            "data": {"courier_tracking_status": None, "courier_waybill_number": None},
        }

    stale = (
        order.courier_tracking_updated_at is None
        or (
            datetime.now(timezone.utc) - order.courier_tracking_updated_at.astimezone(timezone.utc)
        ).total_seconds()
        > 60
    )
    shipment = None
    if stale:
        # Refresh through courier_sync_service so a tracking read applies the
        # same handover/cod/failure_note mapping the webhook does -- otherwise
        # a lost `delivered` push would show the right status here while
        # leaving courier_cod_collected wrong. Returns None on an outage; we
        # then serve whatever we already have.
        shipment = await courier_sync_service.refresh_order_tracking(db, order)

    return {
        "success": True,
        "data": {
            "courier_tracking_status": order.courier_tracking_status,
            "courier_waybill_number": order.courier_waybill_number,
            "courier_tracking_url": order.courier_tracking_url,
            "cod_collected": order.courier_cod_collected,
            "handover_verified": order.courier_handover_verified,
            "failure_note": order.courier_failure_note,
            "history": (shipment or {}).get("history", []),
        },
    }


@router.post("/{order_id}/cancel", response_model=dict)
async def cancel_order(
    order_id: str,
    reason: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Cancel an order.
    """
    try:
        order_uuid = UUID(order_id)
        order = await OrderService.get_by_id(db, order_uuid, current_user.user_id)

        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found"
            )

        order = await OrderService.cancel_order(db, order, reason)

        return {
            "success": True,
            "data": format_order(order),
            "message": "Order cancelled successfully"
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cancelling order: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cancel order"
        )


@router.post("/{order_id}/return", response_model=dict)
async def request_order_return(
    order_id: str,
    data: ReturnRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Request a return for a delivered order (Module 5.5).

    Only the order's owner can request it, and only while it is DELIVERED.
    Moves the order to RETURN_REQUESTED for an admin to approve/reject.
    """
    try:
        order_uuid = UUID(order_id)
        order = await OrderService.get_by_id(db, order_uuid, current_user.user_id)

        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found"
            )

        order = await OrderService.request_return(
            db, order, data.reason, data.comments, data.items
        )

        return {
            "success": True,
            "data": format_order(order),
            "message": "Return request submitted",
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error requesting return: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to request return"
        )


@router.get("/{order_id}/invoice")
async def download_invoice(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Download the PDF invoice for one of the caller's own orders (Module 5.6).
    """
    try:
        order_uuid = UUID(order_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid order ID")

    order = await OrderService.get_by_id(db, order_uuid, current_user.user_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    try:
        pdf = InvoiceService.generate_invoice_pdf(order, customer=current_user)
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


@router.post("/{order_id}/reorder", response_model=dict)
async def reorder(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Re-add a past order's items to the current user's cart at current prices.

    Unavailable items (missing / inactive / out of stock) are skipped and
    reported back. Accepts an order id or order number.
    """
    try:
        # Resolve id or order number.
        try:
            order_uuid = UUID(order_id)
        except ValueError:
            existing = await OrderService.get_by_order_number(
                db, order_id, current_user.user_id
            )
            if not existing:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Order not found"
                )
            order_uuid = existing.order_id

        cart, unavailable = await OrderService.reorder(
            db, order_uuid, current_user.user_id
        )

        if unavailable:
            message = (
                f"Items added to cart. {len(unavailable)} item(s) were "
                f"unavailable and skipped."
            )
        else:
            message = "Items added to cart"

        return {
            "success": True,
            "data": {
                "cart": cart,
                "unavailable_items": unavailable,
            },
            "message": message,
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reordering: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reorder"
        )


# ============================================================================
# Admin Endpoints
# ============================================================================

@router.get("/admin/all", response_model=dict)
async def get_all_orders(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    payment_status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Get all orders (Admin only).
    """
    try:
        offset = (page - 1) * limit
        orders, total = await OrderService.get_all_orders(
            db,
            limit=limit,
            offset=offset,
            status=status,
            payment_status=payment_status
        )

        return {
            "success": True,
            "data": {
                "orders": [format_order(o, include_items=False) for o in orders],
                "pagination": {
                    "total": total,
                    "page": page,
                    "limit": limit,
                    "pages": (total + limit - 1) // limit
                }
            }
        }
    except Exception as e:
        logger.error(f"Error fetching all orders: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch orders"
        )


@router.put("/admin/{order_id}/status", response_model=dict)
async def update_order_status(
    order_id: str,
    data: UpdateOrderStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Update order status (Admin only).
    """
    try:
        order_uuid = UUID(order_id)
        order = await OrderService.get_by_id(db, order_uuid)

        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found"
            )

        order = await OrderService.update_status(db, order, data.status)

        return {
            "success": True,
            "data": format_order(order),
            "message": f"Order status updated to {data.status.value}"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating order status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update order status"
        )


@router.put("/admin/{order_id}/payment", response_model=dict)
async def update_payment_status(
    order_id: str,
    data: UpdatePaymentStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Update payment status (Admin only).
    """
    try:
        order_uuid = UUID(order_id)
        order = await OrderService.get_by_id(db, order_uuid)

        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found"
            )

        order = await OrderService.update_payment_status(
            db, order, data.payment_status, data.payment_id
        )

        return {
            "success": True,
            "data": format_order(order),
            "message": f"Payment status updated to {data.payment_status.value}"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating payment status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update payment status"
        )


@router.get("/admin/stats", response_model=dict)
async def get_order_stats(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Get order statistics (Admin only).
    """
    try:
        stats = await OrderService.get_revenue_stats(db)

        # Get counts by status
        pending_count = await OrderService.get_order_count(db, status="pending")
        processing_count = await OrderService.get_order_count(db, status="processing")
        shipped_count = await OrderService.get_order_count(db, status="shipped")
        delivered_count = await OrderService.get_order_count(db, status="delivered")

        return {
            "success": True,
            "data": {
                "revenue": stats,
                "status_counts": {
                    "pending": pending_count,
                    "processing": processing_count,
                    "shipped": shipped_count,
                    "delivered": delivered_count
                }
            }
        }
    except Exception as e:
        logger.error(f"Error getting order stats: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get order statistics"
        )
