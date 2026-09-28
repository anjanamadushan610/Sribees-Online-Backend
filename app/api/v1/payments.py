"""
Payment API Endpoints
"""
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.orders import format_order
from app.config.database import get_db
from app.core.dependencies import (
    get_current_user,
    require_super_admin,
)
from app.core.request_language import current_language
from app.models.order import Order
from app.payments import catalog as payment_catalog
from app.payments import service as online_payments
from app.payments.gateways.base import Outcome
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
from app.services.order_service import OrderService
from app.services.payment_service import PaymentService

# Prefix "/payments" is applied by app/api/v1/router.py — do not repeat it here.
router = APIRouter(tags=["Payments"])

# ============================================================================
# Payment methods -- mounted separately at "/payment-methods".
#
# Every method lives in the catalog (app/payments/catalog.py): its name and
# the line under it in the caller's language, a generic icon, and how the
# money moves. The app draws this list and nothing else.
# ============================================================================
methods_router = APIRouter(tags=["Payment Methods"])


@methods_router.get("", response_model=dict, summary="List supported payment methods")
async def list_payment_methods(
    current_user=Depends(get_current_user),
):
    """The methods checkout offers right now, in the caller's language."""
    return {"success": True, "data": {"methods": payment_catalog.offered(current_language())}}


# ============================================================================
# Online payments, any gateway (app/payments/service.py)
# ============================================================================


class CompletePaymentRequest(BaseModel):
    # The URL the app landed on when the gateway sent the customer back.
    # Only its query is read, and only to find WHICH try to ask the gateway
    # about -- it passed through the customer's browser and proves nothing.
    return_url: str = Field(..., min_length=1, max_length=2048)


class PayAgainRequest(BaseModel):
    method: Optional[str] = Field(None, max_length=40)


async def _own_order(db: AsyncSession, order_id: str, user) -> "Order":
    try:
        order_uuid = UUID(order_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid order ID")
    order = await OrderService.get_by_id(db, order_uuid, user.user_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order


def _outcome_body(result: online_payments.CompleteResult) -> dict:
    return {
        "success": result.outcome == Outcome.PAID,
        "data": {**format_order(result.order), "outcome": result.outcome.value},
        "message": result.message,
    }


@router.post("/orders/{order_id}/complete", response_model=dict)
async def complete_payment(
    order_id: str,
    data: CompletePaymentRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """The customer is back from the gateway: settle that try with the
    gateway and answer with the order as it now stands."""
    order = await _own_order(db, order_id, current_user)
    try:
        result = await online_payments.complete(
            db, order, online_payments.return_params(data.return_url), current_language()
        )
    except online_payments.PaymentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    order = await OrderService.get_by_id(db, order.order_id, current_user.user_id)
    return _outcome_body(online_payments.CompleteResult(result.outcome, order, result.message))


@router.post("/orders/{order_id}/pay", response_model=dict)
async def pay_order(
    order_id: str,
    data: PayAgainRequest = Body(default_factory=PayAgainRequest),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Pay (again) for an order still waiting for its money, by the method it
    was placed with or another online one. Answers with where to go."""
    order = await _own_order(db, order_id, current_user)
    try:
        action = await online_payments.pay_again(
            db, order, current_user, method_code=data.method, lang=current_language()
        )
    except online_payments.PaymentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {"success": True, "data": {"payment_action": action}}


# ============================================================================
# Marx IPG -- the confirm call apps before the payments layer make. Same
# rules, same answers; the work is done by the service above.
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
    redirected back with trId/merchantRID. Kept for apps that predate
    POST /payments/orders/{id}/complete.
    """
    order = await _own_order(db, data.order_id, current_user)

    # Idempotent: a second confirm on a paid order must not call Marx again.
    if order.payment_status == PaymentStatusEnum.PAID.value:
        return {"success": True, "data": format_order(order), "message": "Already confirmed"}

    params = {"trId": data.tr_id, "merchantRID": data.merchant_rid}
    try:
        result = await online_payments.complete(db, order, params, current_language())
    except online_payments.PaymentError as exc:
        if exc.status_code == status.HTTP_502_BAD_GATEWAY:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment reference mismatch")

    order = await OrderService.get_by_id(db, order.order_id, current_user.user_id)
    if result.outcome == Outcome.PAID:
        return {"success": True, "data": format_order(order), "message": "Payment successful"}
    if result.outcome == Outcome.FAILED:
        return {"success": False, "data": format_order(order), "message": "Payment was declined"}
    return {"success": False, "data": format_order(order), "message": "Payment is still being confirmed"}


@router.get("/marx/return", response_class=HTMLResponse, include_in_schema=False)
async def marx_return_landing():
    """
    Unauthenticated static landing page. ``settings.marx_ipg_return_url``
    points here -- a fallback for the rare case where the app does not catch
    the redirect (the shopper opened the pay link in an external browser).
    In the normal flow the app intercepts navigation to this URL.
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
    current_user = Depends(require_super_admin)
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
