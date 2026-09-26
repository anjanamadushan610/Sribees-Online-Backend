"""
Payment Service - Stripe Integration
"""
from decimal import Decimal
from typing import Optional

from loguru import logger

from app.config.settings import settings


class PaymentService:
    """
    Payment service for handling Stripe payments.

    Note: In production, implement actual Stripe SDK integration.
    This is a stub implementation for the migration.
    """

    @staticmethod
    async def create_payment_intent(
        amount: Decimal,
        currency: str = "inr",
        order_id: str = None,
        customer_id: Optional[str] = None
    ) -> dict:
        """
        Create a Stripe payment intent.

        In production, use:
        ```python
        import stripe
        stripe.api_key = settings.stripe_secret_key

        intent = stripe.PaymentIntent.create(
            amount=int(amount * 100),  # Convert to paise
            currency=currency,
            metadata={"order_id": order_id}
        )
        ```
        """
        # Stub implementation
        import uuid

        payment_intent_id = f"pi_{uuid.uuid4().hex[:24]}"
        client_secret = f"{payment_intent_id}_secret_{uuid.uuid4().hex[:24]}"

        logger.info(f"Created payment intent: {payment_intent_id} for order {order_id}")

        return {
            "payment_intent_id": payment_intent_id,
            "client_secret": client_secret,
            "amount": float(amount),
            "currency": currency,
            "status": "requires_payment_method"
        }

    @staticmethod
    async def confirm_payment(
        payment_intent_id: str,
        payment_method_id: Optional[str] = None
    ) -> dict:
        """
        Confirm a payment intent.

        In production, use:
        ```python
        intent = stripe.PaymentIntent.confirm(
            payment_intent_id,
            payment_method=payment_method_id
        )
        ```
        """
        # Stub implementation
        logger.info(f"Confirmed payment: {payment_intent_id}")

        return {
            "payment_intent_id": payment_intent_id,
            "status": "succeeded",
            "amount_received": 0  # Would be actual amount
        }

    @staticmethod
    async def retrieve_payment_intent(payment_intent_id: str) -> dict:
        """
        Retrieve payment intent status.
        """
        # Stub implementation
        return {
            "payment_intent_id": payment_intent_id,
            "status": "succeeded"
        }

    @staticmethod
    async def create_refund(
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None
    ) -> dict:
        """
        Create a refund for a payment.

        In production, use:
        ```python
        refund = stripe.Refund.create(
            payment_intent=payment_intent_id,
            amount=int(amount * 100) if amount else None,
            reason=reason
        )
        ```
        """
        import uuid
        from datetime import datetime

        refund_id = f"re_{uuid.uuid4().hex[:24]}"

        logger.info(f"Created refund: {refund_id} for payment {payment_intent_id}")

        return {
            "refund_id": refund_id,
            "payment_intent_id": payment_intent_id,
            "amount": float(amount) if amount else 0,
            "status": "succeeded",
            "reason": reason,
            "created_at": datetime.utcnow()
        }

    @staticmethod
    async def handle_webhook(payload: bytes, signature: str) -> dict:
        """
        Handle Stripe webhook events with signature verification.
        Raises ValueError if the signature is missing, invalid, or the
        webhook secret is not configured — preventing fake event injection.
        """
        import stripe as stripe_lib

        webhook_secret = settings.stripe_webhook_secret
        if not webhook_secret or webhook_secret.startswith("whsec_your"):
            logger.error("Stripe webhook secret not configured — rejecting event")
            raise ValueError("Webhook secret not configured")

        try:
            event = stripe_lib.Webhook.construct_event(
                payload, signature, webhook_secret
            )
        except stripe_lib.error.SignatureVerificationError as e:
            logger.warning(f"Invalid Stripe webhook signature: {e}")
            raise ValueError("Invalid webhook signature")
        except Exception as e:
            logger.error(f"Webhook parse error: {e}")
            raise ValueError("Invalid webhook payload")

        event_type = event["type"]
        logger.info(f"Verified Stripe webhook: {event_type}")

        return {
            "received": True,
            "event_type": event_type,
            "event_id": event["id"],
        }

    # ========================================================================
    # Saved Cards Methods
    # ========================================================================

    @staticmethod
    async def get_saved_cards(user_id: str) -> list:
        """
        Get user's saved payment methods.

        In production, use:
        ```python
        payment_methods = stripe.PaymentMethod.list(
            customer=customer_id,
            type="card"
        )
        ```
        """

        # Stub implementation - return empty list or mock data
        # In production, query Stripe for customer's saved cards
        return []

    @staticmethod
    async def save_card(
        user_id: str,
        payment_method_id: str,
        set_default: bool = False
    ) -> dict:
        """
        Attach a payment method to customer and optionally set as default.

        In production:
        1. Get or create Stripe customer
        2. Attach payment method to customer
        3. If set_default, update customer's default payment method
        """
        import uuid
        from datetime import datetime

        # Stub implementation
        card_id = f"pm_{uuid.uuid4().hex[:24]}"

        logger.info(f"Saved card {card_id} for user {user_id}")

        return {
            "card_id": card_id,
            "brand": "visa",
            "last4": "4242",
            "exp_month": 12,
            "exp_year": 2028,
            "is_default": set_default,
            "created_at": datetime.utcnow()
        }

    @staticmethod
    async def delete_saved_card(user_id: str, card_id: str) -> bool:
        """
        Detach a payment method from customer.

        In production:
        ```python
        stripe.PaymentMethod.detach(card_id)
        ```
        """
        logger.info(f"Deleted card {card_id} for user {user_id}")
        return True

    @staticmethod
    async def get_payment_history(user_id: str, db) -> list:
        """
        Get user's payment history from orders.
        """
        from uuid import UUID

        from sqlalchemy import select

        from app.models.order import Order

        result = await db.execute(
            select(Order)
            .where(Order.user_id == UUID(user_id))
            .where(Order.payment_status == "paid")
            .order_by(Order.created_at.desc())
            .limit(50)
        )
        orders = result.scalars().all()

        payments = []
        for order in orders:
            payments.append({
                "payment_id": order.payment_id or str(order.order_id),
                "order_id": str(order.order_id),
                "amount": float(order.total_amount),
                "currency": "inr",
                "status": order.payment_status,
                "payment_method": order.payment_method or "card",
                "created_at": order.created_at
            })

        return payments
