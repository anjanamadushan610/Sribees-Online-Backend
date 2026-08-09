"""
Payment API Endpoints
"""
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.orders import format_order
from app.config.database import get_db
from app.config.settings import settings
from app.core.dependencies import get_current_admin, get_current_user
from app.schemas.order import PaymentStatusEnum
from app.schemas.payment import (
    ConfirmPaymentRequest,
    CreatePaymentIntentRequest,
    PaymentHistoryResponse,
    RefundRequest,
    SaveCardRequest,
    SavedCardResponse,
    SavedCardsListResponse,
)
from app.services.marx_ipg_service import MarxIPGError, MarxIPGService
from app.services.order_service import OrderService
from app.services.payment_service import PaymentService

# Prefix "/payments" is applied by app/api/v1/router.py — do not repeat it here.
router = APIRouter(tags=["Payments"])

# ============================================================================
# Payment Methods (Module 13.1) — mounted separately at "/payment-methods".
#
# Cash on Delivery + internal Wallet, plus the three Marx IPG hosted-gateway
# methods (LKR only — see MARX_PAYMENT_METHOD_MAP). Kept as a constant so it
# can later be swapped for a DB-backed PaymentMethod table without changing
# the endpoint contract.
# ============================================================================
methods_router = APIRouter(tags=["Payment Methods"])

_SUPPORTED_PAYMENT_METHODS = [
    {
        "id": "cod",
        "code": "COD",
        "name": "Cash on Delivery",
        "type": "OFFLINE",
        "is_active": True,
    },
    {
        "id": "wallet",
        "code": "WALLET",
        "name": "SRIBEES Wallet",
        "type": "WALLET",
        "is_active": True,
    },
    {
        "id": "marx_other",
        "code": "MARX_OTHER",
        "name": "Visa / Mastercard / UnionPay",
        "type": "GATEWAY",
        # Gated by settings.marx_gateway_methods_enabled, NOT hardcoded --
        # see list_payment_methods(). The backend can create/confirm Marx
        # orders as soon as it's deployed, but the *mobile app* is what
        # redirects the shopper to pay (handles payment_redirect_url). A
        # backend deploy ships instantly; a Flutter app release does not --
        # exposing these tiles before the updated app has rolled out would
        # let a customer on the OLD app "place" a gateway order that's
        # actually still unpaid, with no redirect ever happening. Flip the
        # setting on once the app update carrying MarxPaymentWebViewScreen is
        # live.
        "is_active": True,
    },
    {
        "id": "marx_amex",
        "code": "MARX_AMEX",
        "name": "Amex / Diners / Discover",
        "type": "GATEWAY",
        "is_active": True,
    },
    {
        "id": "marx_bank",
        "code": "MARX_BANK",
        "name": "Bank Transfer",
        "type": "GATEWAY",
        "is_active": True,
    },
]


@methods_router.get("", response_model=dict, summary="List supported payment methods")
async def list_payment_methods(
    current_user=Depends(get_current_user),
):
    """
    Return the active payment methods available at checkout: Cash on
    Delivery, the internal SRIBEES Wallet, and (once
    ``settings.marx_gateway_methods_enabled`` is turned on) the Marx IPG
    gateway methods.
    """
    active = [
        m
        for m in _SUPPORTED_PAYMENT_METHODS
        if m["is_active"] and (m["type"] != "GATEWAY" or settings.marx_gateway_methods_enabled)
    ]
    return {"success": True, "data": {"methods": active}}


# ============================================================================
# Marx IPG Gateway
# ============================================================================

class MarxConfirmRequest(BaseModel):
    order_id: str
    tr_id: str
    merchant_rid: str


@router.post("/marx/confirm", response_model=dict)
async def confirm_marx_payment(
    data: MarxConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Confirm a Marx IPG hosted-page payment after the shopper's browser is
    redirected back with trId/merchantRID (doc sections 5.2-5.4). Called by
    the mobile app's WebView interception of the return URL, not by Marx
    itself (Marx redirects the shopper's browser; it does not call our API).
    """
    try:
        order_uuid = UUID(data.order_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid order ID")

    order = await OrderService.get_by_id(db, order_uuid, current_user.user_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    # 5.3: validate BEFORE calling 5.4 -- never trust redirect query
    # parameters blindly, they pass through the shopper's browser.
    if order.gateway_tr_id != data.tr_id or order.order_number != data.merchant_rid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Payment reference mismatch"
        )

    # Idempotent: a second confirm call on an already-PAID order (e.g. the
    # WebView retrying) must not re-call Marx.
    if order.payment_status == PaymentStatusEnum.PAID.value:
        return {"success": True, "data": format_order(order), "message": "Already confirmed"}

    try:
        result = await MarxIPGService.initiate_payment(tr_id=data.tr_id, merchant_rid=data.merchant_rid)
    except MarxIPGError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not confirm payment with the gateway: {exc}",
        )

    order.gateway_status = result["summary_result"]
    order.gateway_response_code = result.get("gateway_code")
    order.gateway_raw_response = result["raw"]
    order.gateway_confirmed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(order)

    if result["summary_result"] == "SUCCESS" and result.get("order_status") == "CAPTURED":
        order = await OrderService.update_payment_status(
            db, order, PaymentStatusEnum.PAID, payment_id=data.tr_id
        )
        order = await OrderService.get_by_id(db, order_uuid, current_user.user_id)
        return {"success": True, "data": format_order(order), "message": "Payment successful"}

    if result["summary_result"] == "FAILURE":
        order.payment_status = PaymentStatusEnum.FAILED.value
        await db.commit()
        await db.refresh(order)
        return {"success": False, "data": format_order(order), "message": "Payment was declined"}

    # PENDING / UNKNOWN -- leave payment_status as-is; the reconciliation job
    # (OrderService.reconcile_pending_gateway_payments) resolves it later.
    return {
        "success": False,
        "data": format_order(order),
        "message": "Payment is still being confirmed",
    }


@router.get("/marx/return", response_class=HTMLResponse, include_in_schema=False)
async def marx_return_landing():
    """
    Unauthenticated static landing page. ``settings.marx_ipg_return_url``
    points here — a fallback for the rare case where the mobile WebView
    doesn't intercept the redirect (e.g. the shopper opened the pay link in an
    external browser). In the normal flow this page never actually renders;
    ``MarxPaymentWebViewScreen`` intercepts navigation to this URL client-side.
    """
    return (
        "<html><body style='font-family:sans-serif;text-align:center;padding-top:4rem'>"
        "<h2>Payment complete</h2>"
        "<p>Return to the SRIBEESonline app to see your order status.</p>"
        "</body></html>"
    )


# ============================================================================
# Payment Endpoints
# ============================================================================

@router.post("/create-intent", response_model=dict)
async def create_payment_intent(
    data: CreatePaymentIntentRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Create a payment intent for an order.
    """
    try:
        # Get order
        order = await OrderService.get_by_id(
            db,
            UUID(data.order_id),
            current_user.user_id
        )

        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found"
            )

        if order.payment_status == "paid":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Order is already paid"
            )

        # Create payment intent
        intent = await PaymentService.create_payment_intent(
            amount=order.total_amount,
            currency="inr",
            order_id=str(order.order_id)
        )

        return {
            "success": True,
            "data": intent
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating payment intent: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create payment intent"
        )


@router.post("/confirm", response_model=dict)
async def confirm_payment(
    data: ConfirmPaymentRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Confirm a payment.
    """
    try:
        # Confirm with payment provider
        result = await PaymentService.confirm_payment(
            data.payment_intent_id,
            data.payment_method_id
        )

        if result["status"] == "succeeded":
            # Update order payment status
            # Note: In production, get order_id from payment intent metadata
            return {
                "success": True,
                "data": result,
                "message": "Payment successful"
            }
        else:
            return {
                "success": False,
                "data": result,
                "message": "Payment requires additional action"
            }
    except Exception as e:
        logger.error(f"Error confirming payment: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to confirm payment"
        )


@router.get("/status/{payment_intent_id}", response_model=dict)
async def get_payment_status(
    payment_intent_id: str,
    current_user = Depends(get_current_user)
):
    """
    Get payment intent status.
    """
    try:
        result = await PaymentService.retrieve_payment_intent(payment_intent_id)

        return {
            "success": True,
            "data": result
        }
    except Exception as e:
        logger.error(f"Error getting payment status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get payment status"
        )


# ============================================================================
# Admin Endpoints
# ============================================================================

@router.post("/refund", response_model=dict)
async def create_refund(
    data: RefundRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Create a refund for an order. (Admin only)
    """
    try:
        # Get order
        order = await OrderService.get_by_id(db, UUID(data.order_id))

        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found"
            )

        if order.payment_status != "paid":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Order has not been paid"
            )

        if not order.payment_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No payment ID found for this order"
            )

        # Create refund
        refund_amount = Decimal(str(data.amount)) if data.amount else order.total_amount

        result = await PaymentService.create_refund(
            payment_intent_id=order.payment_id,
            amount=refund_amount,
            reason=data.reason
        )

        # Update order status
        await OrderService.update_payment_status(
            db, order, PaymentStatusEnum.REFUNDED
        )

        return {
            "success": True,
            "data": {
                "refund_id": result["refund_id"],
                "amount": float(refund_amount),
                "status": result["status"]
            },
            "message": "Refund processed successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating refund: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create refund"
        )


# ============================================================================
# Saved Cards Endpoints
# ============================================================================

@router.get("/cards", response_model=SavedCardsListResponse)
async def get_saved_cards(
    current_user = Depends(get_current_user)
):
    """
    Get user's saved payment methods (cards).
    """
    try:
        cards = await PaymentService.get_saved_cards(str(current_user.user_id))

        return SavedCardsListResponse(
            success=True,
            cards=[SavedCardResponse(**card) for card in cards]
        )
    except Exception as e:
        logger.error(f"Error getting saved cards: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get saved cards"
        )


@router.post("/cards", response_model=dict)
async def save_card(
    data: SaveCardRequest,
    current_user = Depends(get_current_user)
):
    """
    Save a new payment method (card).
    """
    try:
        card = await PaymentService.save_card(
            user_id=str(current_user.user_id),
            payment_method_id=data.payment_method_id,
            set_default=data.set_default
        )

        return {
            "success": True,
            "data": card,
            "message": "Card saved successfully"
        }
    except Exception as e:
        logger.error(f"Error saving card: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save card"
        )


@router.delete("/cards/{card_id}", response_model=dict)
async def delete_saved_card(
    card_id: str,
    current_user = Depends(get_current_user)
):
    """
    Delete a saved payment method.
    """
    try:
        await PaymentService.delete_saved_card(
            user_id=str(current_user.user_id),
            card_id=card_id
        )

        return {
            "success": True,
            "message": "Card deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting card: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete card"
        )


@router.get("/history", response_model=PaymentHistoryResponse)
async def get_payment_history(
    current_user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get user's payment history.
    """
    try:
        payments = await PaymentService.get_payment_history(
            str(current_user.user_id), db
        )

        return PaymentHistoryResponse(
            success=True,
            payments=payments,
            total=len(payments)
        )
    except Exception as e:
        logger.error(f"Error getting payment history: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get payment history"
        )


# ============================================================================
# Webhook Endpoint
# ============================================================================

@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Handle Stripe webhook events.
    """
    try:
        payload = await request.body()
        signature = request.headers.get("stripe-signature", "")

        result = await PaymentService.handle_webhook(payload, signature)

        # Handle different event types
        event_type = result.get("event_type")

        if event_type == "payment_intent.succeeded":
            # Update order payment status
            logger.info("Payment succeeded webhook received")
            # Extract order_id from event metadata and update
            pass

        elif event_type == "payment_intent.payment_failed":
            logger.info("Payment failed webhook received")
            pass

        elif event_type == "charge.refunded":
            logger.info("Refund webhook received")
            pass

        return {"received": True}

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook processing failed"
        )
