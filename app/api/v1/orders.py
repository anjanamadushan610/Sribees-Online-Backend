"""
Order API Endpoints
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis
from app.config.settings import settings
from app.core.dependencies import (
    get_current_user,
    require_super_admin,
)
from app.core.media import media_url
from app.core.request_language import current_language
from app.models.order import OrderStatus
from app.payments import service as online_payments
from app.schemas.order import (
    CreateOrderRequest,
    OrderQuoteResponse,
    QuoteOrderRequest,
    ReorderRequest,
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
from app.services.delivery_verification_service import show_code_to_customer
from app.services.invoice_service import InvoiceService
from app.services.loyalty_service import LoyaltyService
from app.services.order_reward_service import OrderRewardService
from app.services.order_service import OrderService
from app.services.pricing_service import PricingService
from app.services.product_review_service import ProductReviewService
from app.services.storage import StorageError, get_storage
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

# Known timestamp column per status (others are inferred or fetched from status history).
_STATUS_TIMESTAMPS = {
    "pending": "created_at",
    "confirmed": "gateway_confirmed_at",
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

    Combines order status history records (from internal actions & SribeesExpress courier sync)
    and order timestamp columns (created_at, packed_at, ready_for_pickup_at, etc.).
    Each step reports whether it is completed and which one is current.
    """
    def ts(field: Optional[str]):
        if not field:
            return None
        value = getattr(order, field, None)
        return value.isoformat() if value else None

    # Status history map from order.status_history (records exact transition time)
    history_map = {}
    if hasattr(order, "status_history") and order.status_history:
        for h in order.status_history:
            if getattr(h, "created_at", None) and getattr(h, "new_status", None):
                status_name = _effective_status(h.new_status)
                if status_name not in history_map:
                    history_map[status_name] = h.created_at.isoformat()

    def _get_step_timestamp(status_key: str) -> Optional[str]:
        # 1. Exact timestamp from status change history
        st = history_map.get(status_key)
        if st:
            return st

        # 2. Known column timestamp
        timestamp_field = _STATUS_TIMESTAMPS.get(status_key)
        if timestamp_field:
            col_ts = ts(timestamp_field)
            if col_ts:
                return col_ts

        # 3. Contextual / transitional fallbacks
        if status_key == "pending":
            return ts("created_at")
        elif status_key == "confirmed":
            return (
                ts("gateway_confirmed_at")
                or ts("courier_quote_confirmed_at")
                or (history_map.get("pending") if getattr(order, "payment_status", None) == "PAID" else None)
            )
        elif status_key == "packing":
            return (
                history_map.get("packing")
                or history_map.get("processing")
                or ts("packed_at")
            )
        elif status_key == "ready_for_pickup":
            return ts("ready_for_pickup_at")
        elif status_key == "handed_to_courier":
            return ts("handed_to_courier_at")
        elif status_key == "shipped":
            return (
                ts("shipped_at")
                or history_map.get("shipped")
                or ts("courier_tracking_updated_at")
            )
        elif status_key == "out_for_delivery":
            return (
                history_map.get("out_for_delivery")
                or ts("courier_tracking_updated_at")
            )
        elif status_key == "delivered":
            return history_map.get("delivered") or ts("delivered_at")
        return None

    # Return lifecycle: delivered → return_requested → return_approved → rto_initiated → rto_delivered → refunded
    # An order that reached return states has already completed the full forward delivery workflow.
    if order.status in ("return_requested", "return_approved", "rto_initiated", "rto_delivered") or (
        order.status == "refunded" and (getattr(order, "return_requested_at", None) or getattr(order, "delivered_at", None) or history_map.get("delivered"))
    ):
        timeline = []
        # 1. Preserve all 9 fulfilled forward workflow steps
        for status_key, step_label in _STATUS_FLOW:
            step_ts = _get_step_timestamp(status_key) or history_map.get(status_key)
            timeline.append({
                "status": status_key,
                "label": step_label,
                "timestamp": step_ts or ts("created_at"),
                "completed": True,
                "current": False,
            })

        # 2. Append Return Requested milestone
        req_ts = ts("return_requested_at") or history_map.get("return_requested") or ts("updated_at")
        is_req_current = order.status == "return_requested"
        timeline.append({
            "status": "return_requested",
            "label": "Return Requested",
            "timestamp": req_ts,
            "completed": True,
            "current": is_req_current,
        })

        # 3. Append Return Approved milestone if reached
        if (
            order.status in ("return_approved", "rto_initiated", "rto_delivered")
            or getattr(order, "return_approved_at", None)
            or history_map.get("return_approved")
            or order.status == "refunded"
        ):
            appr_ts = ts("return_approved_at") or history_map.get("return_approved") or req_ts
            is_appr_current = order.status == "return_approved"
            timeline.append({
                "status": "return_approved",
                "label": "Return Approved",
                "timestamp": appr_ts,
                "completed": True,
                "current": is_appr_current,
            })

        # 4. Append Courier Reverse Pickup (RTO) milestones if present
        if history_map.get("rto_initiated") or order.status in ("rto_initiated", "rto_delivered"):
            rto_init_ts = history_map.get("rto_initiated") or ts("updated_at")
            is_rto_init_current = order.status == "rto_initiated"
            timeline.append({
                "status": "rto_initiated",
                "label": "Return Pickup Initiated",
                "timestamp": rto_init_ts,
                "completed": True,
                "current": is_rto_init_current,
            })

        if history_map.get("rto_delivered") or order.status == "rto_delivered":
            rto_del_ts = history_map.get("rto_delivered") or ts("updated_at")
            is_rto_del_current = order.status == "rto_delivered"
            timeline.append({
                "status": "rto_delivered",
                "label": "Returned to Branch",
                "timestamp": rto_del_ts,
                "completed": True,
                "current": is_rto_del_current,
            })

        # 5. Append Refunded milestone if reached
        if order.status == "refunded":
            ref_ts = ts("refunded_at") or history_map.get("refunded") or ts("updated_at")
            timeline.append({
                "status": "refunded",
                "label": "Refund Completed",
                "timestamp": ref_ts,
                "completed": True,
                "current": True,
            })

        return timeline

    # Pre-delivery terminal states (cancelled, or refunded before delivery):
    if order.status in ("cancelled", "refunded"):
        label = "Cancelled" if order.status == "cancelled" else "Refunded"
        cancelled_ts = (
            history_map.get(order.status)
            or ts(_STATUS_TIMESTAMPS.get(order.status, "cancelled_at"))
            or ts("updated_at")
        )

        timeline = []
        for status_key, step_label in _STATUS_FLOW:
            step_ts = _get_step_timestamp(status_key)
            # Include 'pending' always, and any intermediate step that has a recorded timestamp / history
            if status_key == "pending" or step_ts is not None:
                timeline.append({
                    "status": status_key,
                    "label": step_label,
                    "timestamp": step_ts or ts("created_at"),
                    "completed": True,
                    "current": False,
                })

        # Append terminal status as the current step
        timeline.append({
            "status": order.status,
            "label": label,
            "timestamp": cancelled_ts,
            "completed": True,
            "current": True,
        })
        return timeline

    flow_keys = [s for s, _ in _STATUS_FLOW]
    effective = _effective_status(order.status)
    current_rank = flow_keys.index(effective) if effective in flow_keys else 0

    timeline = []
    for rank, (status_key, label) in enumerate(_STATUS_FLOW):
        step_completed = rank <= current_rank
        step_current = rank == current_rank
        step_timestamp = _get_step_timestamp(status_key) if step_completed else None

        timeline.append({
            "status": status_key,
            "label": label,
            "timestamp": step_timestamp,
            "completed": step_completed,
            "current": step_current,
        })
    return timeline


def format_order(
    order,
    include_items: bool = True,
    payment_redirect_url: Optional[str] = None,
    reviewed_product_ids: Optional[set] = None,
    wallet_balance: Optional[Decimal] = None,
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
        # Where that cashback is: "pending" until the return window closes
        # after delivery, then "credited"; "reversed" if cancelled. Decided
        # here so the app shows a state, not a rule it would have to copy.
        **OrderRewardService.order_cashback_state(order),
        "total_amount": float(order.total_amount),
        "status": order.status,
        # What the app shows for the status and the payment, in the caller's
        # language -- so a status it has never heard of still reads right.
        "status_label": online_payments.status_label(order, current_language()),
        "payment": online_payments.payment_view(order, current_language()),
        "payment_status": order.payment_status,
        "payment_method": order.payment_method,
        "payment_id": order.payment_id,
        # Marx IPG gateway — payment_redirect_url is response-only, never persisted.
        "payment_redirect_url": payment_redirect_url,
        "gateway_status": order.gateway_status,
        # Delivery handover verification code (non-COD orders only)
        "delivery_verification_code": order.delivery_verification_code,
        "delivery_verification_status": order.delivery_verification_status,
        # Whether to put that code in front of the customer; decided here.
        "show_delivery_code": show_code_to_customer(order),
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
        "return_images": [media_url(p) for p in (order.return_images or [])],
        # Everything the app needs to draw the Cancel button, decided HERE.
        # The rules involve the courier booking and the payment mix, which the
        # app does not have -- and a second copy on the phone cannot be fixed
        # for a fortnight once the store has the build.
        #
        #   free    -> plain button, everything paid comes back
        #   fee     -> RED button, and the confirmation must say the parcel is
        #              already booked and the delivery charge is kept back
        #   blocked -> no button
        "can_cancel": OrderService.customer_can_cancel(order),
        "cancel_state": OrderService.customer_cancel_state(order),
        # The real figures, so the confirmation names them instead of saying
        # "your money".
        "cancel_refund_amount": float(OrderService.cancellation_refund_preview(order)),
        "cancel_courier_fee": float(
            OrderService.cancellation_charge(order, wallet_balance or Decimal("0"))
        ),
        # The part of that charge that would come out of the customer's
        # existing wallet balance rather than out of this order's payment --
        # all of it on a COD order. Only known when the caller looked up the
        # balance (the order detail screen, which is where Cancel lives).
        "cancel_wallet_charge": float(
            OrderService.cancellation_wallet_charge(order, wallet_balance or Decimal("0"))
        ),
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
                "is_returnable": bool(getattr(item, "is_returnable", True)),
                # Snapshotted at checkout (see migration 065) — null for
                # orders placed before this was tracked, in which case the
                # app shows no per-item cashback rather than guessing one.
                "cashback_percentage": (
                    float(item.cashback_percentage)
                    if getattr(item, "cashback_percentage", None) is not None
                    else None
                ),
                "cashback_earned": (
                    float(
                        (item.unit_price * item.quantity * item.cashback_percentage / 100)
                        .quantize(Decimal("0.01"))
                    )
                    if getattr(item, "cashback_percentage", None) is not None
                    else None
                ),
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
        "status_label": online_payments.status_label(order, current_language()),
        "payment": online_payments.payment_view(order, current_language()),
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
        "show_delivery_code": show_code_to_customer(order),
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
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Create a new order from cart.
    """
    from app.services import coupon_presenter
    from app.services.coupon_service import CouponValidationError
    from app.utils.i18n import resolve_language

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
            "data": {
                **format_order(order, payment_redirect_url=payment_redirect_url),
                # Where to send the customer to pay, and how to come back. The
                # app follows this; it knows no gateway.
                "payment_action": (
                    online_payments.action_for(order, payment_redirect_url)
                    if payment_redirect_url
                    else None
                ),
            },
            "message": message
        }
    except CouponValidationError as e:
        # A coupon refusal in the customer's language, like every other.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=coupon_presenter.reason(
                e, resolve_language(request.headers.get("accept-language"))
            ),
        )
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
    request: Request,
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
        from app.services import coupon_presenter
        from app.services.coupon_service import CouponService, CouponValidationError
        from app.utils.i18n import resolve_language

        lang = resolve_language(request.headers.get("accept-language"))
        quote_coupon = None
        coupon_refusal: Optional[str] = None
        coupon_row = None
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
            else:
                coupon_refusal = coupon_presenter.text(lang, "reason.not_found")

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

        # The same catalogue prices checkout will charge; see
        # OrderService._attach_server_prices.
        quote_items = await OrderService._attach_server_prices(db, quote_items, branch_id)
        items = await OrderService._attach_cashback_rates(
            db, quote_items, branch_id
        )

        if quote_coupon and coupon_row:
            enriched_for_coupon = await CouponService.enrich_items(db, items, branch_id)
            eligible_sub = CouponService.eligible_subtotal(coupon_row, enriched_for_coupon)
            quote_coupon["eligible_subtotal"] = float(eligible_sub)

            target_branch = coupon_row.branch_id
            had_completed = (
                await CouponService.has_completed_order(
                    db, current_user.user_id, target_branch
                )
                if coupon_row.first_order_only
                else None
            )
            user_redemptions = await CouponService.redemptions_by_user(
                db, coupon_row.code, current_user.user_id
            )
            try:
                CouponService.validate(
                    coupon_row,
                    eligible_sub,
                    datetime.now(timezone.utc),
                    branch_id=branch_id,
                    user_id=current_user.user_id,
                    user_redemptions=user_redemptions,
                    user_has_completed_order=had_completed,
                    user_points=await LoyaltyService.get_points(db, current_user.user_id),
                    user_phone=current_user.phone,
                )
            except CouponValidationError as exc:
                quote_coupon = None
                coupon_refusal = coupon_presenter.reason(
                    exc, lang, restricted=coupon_presenter.is_restricted(coupon_row)
                )
            except Exception:
                quote_coupon = None

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
                "coupon": (
                    {
                        "code": coupon_row.code if coupon_row else coupon_code_to_check,
                        "label": coupon_presenter.label(coupon_row, lang) if coupon_row else None,
                        "applied": quote_coupon is not None and breakdown.discount > 0,
                        "discount": float(breakdown.discount),
                        "reason": coupon_refusal,
                    }
                    if coupon_code_to_check
                    else None
                ),
            }
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
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
    # Named apart from fastapi.status, which the error path below needs.
    status_filter: Optional[str] = Query(None, alias="status"),
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
            status=status_filter
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

        # Auto-refresh courier tracking if actively handled by courier
        if order.courier_shipment_id and order.status in (
            OrderStatus.READY_FOR_PICKUP.value,
            OrderStatus.HANDED_TO_COURIER.value,
            OrderStatus.SHIPPED.value,
            OrderStatus.OUT_FOR_DELIVERY.value,
        ):
            try:
                if await courier_sync_service.refresh_order_tracking(db, order) is not None:
                    order = await OrderService.get_by_id(db, order.order_id, current_user.user_id) or order
            except Exception as sync_err:
                logger.warning(f"Auto courier sync failed for order {order.order_number}: {sync_err}")

        # Only a delivered order can show write-review actions at all, so
        # only bother looking up existing reviews for one.
        reviewed_product_ids = set()
        if order.status == OrderStatus.DELIVERED.value and order.items:
            reviewed_product_ids = await ProductReviewService.get_reviewed_product_ids(
                db, current_user.user_id, [item.product_id for item in order.items]
            )

        # The Cancel confirmation names what would come out of the wallet (a
        # COD order cancelled after booking), and that depends on the balance.
        wallet_balance = None
        if OrderService.customer_can_cancel(order):
            wallet_balance = await WalletService.get_balance(db, current_user.user_id)

        return {
            "success": True,
            "data": format_order(
                order,
                reviewed_product_ids=reviewed_product_ids,
                wallet_balance=wallet_balance,
            ),
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


def _client_ip(request: Request) -> Optional[str]:
    """
    The caller's address, as close to the truth as we can get.

    Behind nginx (production) and Caddy (development) the socket peer is the
    proxy, so the real client is the FIRST entry of X-Forwarded-For -- the
    proxy appends, so later entries are hops we added ourselves. It is a
    client-settable header and a determined abuser can put anything in it;
    that is fine for what this is used for. It is a signal on an internal
    review screen, not an authorisation decision.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()[:64]
    return request.client.host if request.client else None


# The decorator sits directly on the handler. It was once separated from it by
# _client_ip above, which silently made THAT the route: every cancel from the
# app ran a function returning an IP address and failed, and cancel_order was
# never registered at all. tests/test_order_cancellation.py pins it now.
@router.post("/{order_id}/cancel", response_model=dict)
async def cancel_order(
    order_id: str,
    request: Request,
    reason: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Cancel an order the customer still owns the decision on.

    Allowed until the parcel reaches the customer -- see
    ``OrderService.customer_cancel_state``. After a courier booking the
    delivery charge is kept (from the wallet on a COD order). Anything paid comes back to the
    Sribees Wallet in the same transaction, and every cancellation is recorded
    with where it came from (``order_cancellations``).
    """
    try:
        order_uuid = UUID(order_id)
        order = await OrderService.get_by_id(db, order_uuid, current_user.user_id)

        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found"
            )

        # Blocked means it reached the customer or is already settled: past
        # the doorstep it is a return, not a cancellation. A parcel a rider is
        # still carrying CAN be cancelled (the rider is asked to bring it back).
        if not OrderService.customer_can_cancel(order):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "This order can no longer be cancelled. If it has been "
                    "delivered, you can request a return from the order screen."
                ),
            )

        order = await OrderService.cancel_order(
            db,
            order,
            reason,
            cancelled_by="customer",
            context={
                "ip_address": _client_ip(request),
                "user_agent": request.headers.get("user-agent"),
                "device_id": request.headers.get("x-device-id"),
                "client_platform": request.headers.get("x-client-platform"),
            },
        )

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


# Kept deliberately small. A phone camera photo of a dented tin does not need
# to arrive at full resolution, and the app already downscales to quality 80
# before sending; this is the ceiling, not the target.
RETURN_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
RETURN_IMAGE_MAX_MB = 5
RETURN_IMAGE_MAX_COUNT = 4


def _return_image_folder(order_id: UUID) -> str:
    """One folder per order, which is also what makes the path checkable."""
    return f"returns/{order_id}"


@router.post("/{order_id}/return/images", response_model=dict)
async def upload_return_image(
    order_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Store one photo for a return the customer is about to request.

    Uploaded before the request itself, so the app can show a thumbnail the
    moment a photo is picked and so a failed upload is reported there rather
    than swallowing the whole submission at the end.

    Scoped to the order and the caller: the file lands under
    ``returns/<order_id>/``, and :func:`request_order_return` accepts only
    paths under that prefix. Without that, ``images`` would be a free-text
    field the client fills in, and anything readable on the media host could
    be attached to a claim.
    """
    order_uuid = UUID(order_id)
    order = await OrderService.get_by_id(db, order_uuid, current_user.user_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Order not found"
        )

    # Same gate as the return itself. Uploading against an order that can no
    # longer be returned would only fill the disk with files nothing can claim.
    if order.status != OrderStatus.DELIVERED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only delivered orders can be returned.",
        )

    if file.content_type not in RETURN_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid file type '{file.content_type}'. "
                f"Allowed: {', '.join(sorted(RETURN_IMAGE_TYPES))}"
            ),
        )

    contents = await file.read()
    if len(contents) > RETURN_IMAGE_MAX_MB * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Photo is {len(contents) / 1024 / 1024:.1f}MB. "
                f"Please use one under {RETURN_IMAGE_MAX_MB}MB."
            ),
        )

    import io

    try:
        path = get_storage().save(
            fileobj=io.BytesIO(contents),
            filename=file.filename or "return.jpg",
            folder=_return_image_folder(order_uuid),
            content_type=file.content_type,
        )
    except StorageError as exc:
        # 500 rather than 502: the storage backend is in-process, so a write
        # failure is ours, and calling it a gateway error sends the next person
        # to look at nginx.
        logger.error(f"Return image upload failed for order {order_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not store the photo. Please try again.",
        )

    # The path is what the client sends back with the request; the URL is only
    # so it can render the thumbnail it just uploaded.
    return {"success": True, "data": {"path": path, "url": media_url(path)}}


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

        # Every path must be one WE wrote for THIS order. `images` arrives
        # from the client, so without this it is a free-text field: any
        # readable path on the media host could be pinned to a claim, and a
        # path belonging to somebody else's order could be used to read it.
        valid_prefixes = (
            f"/{settings.media_url_prefix.strip('/')}/returns/{order_uuid}/",
            f"/{settings.media_url_prefix.strip('/')}/returns_{order_uuid}/",
        )
        for path in data.images:
            if not any(path.startswith(prefix) for prefix in valid_prefixes):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Return photos must be uploaded to this order through "
                        "/return/images before the request is submitted."
                    ),
                )

        order = await OrderService.request_return(
            db, order, data.reason, data.comments, data.items, data.images
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


async def _own_order_uuid(db: AsyncSession, order_id: str, user_id) -> UUID:
    """An order id or order number, resolved to the caller's order id."""
    try:
        return UUID(order_id)
    except ValueError:
        existing = await OrderService.get_by_order_number(db, order_id, user_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found"
            )
        return existing.order_id


@router.get("/{order_id}/reorder", response_model=dict)
async def reorder_preview(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    The reorder page: every line of a past order with today's price, how
    many are in stock, and the ordered quantity capped at that. Nothing is
    added to the cart here.
    """
    order_uuid = await _own_order_uuid(db, order_id, current_user.user_id)
    try:
        items = await OrderService.reorder_preview(db, order_uuid, current_user.user_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    for item in items:
        item["image"] = media_url(item["image"])
    return {"success": True, "data": {"items": items}}


@router.post("/{order_id}/reorder", response_model=dict)
async def reorder(
    order_id: str,
    body: Optional[ReorderRequest] = Body(None),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Re-add a past order's items to the current user's cart at current prices.

    With a body, only the listed lines are added, in the listed quantities
    (the reorder page); without one, the whole order. Unavailable items
    (missing / inactive / out of stock) are skipped and reported back.
    Accepts an order id or order number.
    """
    try:
        order_uuid = await _own_order_uuid(db, order_id, current_user.user_id)
        selection = (
            {line.order_item_id: line.quantity for line in body.items}
            if body is not None
            else None
        )

        cart, unavailable = await OrderService.reorder(
            db, order_uuid, current_user.user_id, selection
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
# Legacy: the admin panel uses /admin/orders, which is permission-checked and
# branch-scoped. These four had neither -- any admin account, a delegated staff
# login included, could read every branch's orders and mark any order paid --
# so they are Super Admin only (tests/security/test_admin_authorization.py).

@router.get("/admin/all", response_model=dict)
async def get_all_orders(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    payment_status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(require_super_admin)
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
            status=status_filter,
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
    current_user = Depends(require_super_admin)
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
    current_user = Depends(require_super_admin)
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
    current_user = Depends(require_super_admin)
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
