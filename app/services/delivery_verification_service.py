"""
Delivery Verification Code — handover anti-fraud check for prepaid orders.

Same shape as ``app/services/otp_service.py`` (crypto-random digits via
``secrets.randbelow``, constant-time compare via ``secrets.compare_digest``),
but persisted on the ``Order`` row rather than Redis-TTL'd: the code must
survive from "payment confirmed" (possibly hours before dispatch) until actual
handover, and a 3-minute OTP-style TTL is the wrong lifetime for that.

See docs/COURIER_API.md section 15 for the contract the (future) courier's
rider confirms this code against.
"""
import secrets
from datetime import datetime
from typing import Tuple

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.order import Order
from app.schemas.order import PaymentMethodEnum


class DeliveryVerificationService:
    """Generate and confirm the 3-digit delivery handover code."""

    @staticmethod
    def _generate_code() -> str:
        """A cryptographically-random zero-padded code, length from settings."""
        digits = settings.delivery_verification_code_digits
        return f"{secrets.randbelow(10 ** digits):0{digits}d}"

    @staticmethod
    async def generate_for_order(db: AsyncSession, order: Order) -> None:
        """
        Idempotent: no-op for COD orders (they never get a code) or if a code
        already exists on this order. Does not commit — the caller's
        transaction does (mirrors the "does not commit" convention
        ``OrderService._attach_cashback_rates`` and friends already use).
        """
        if order.payment_method == PaymentMethodEnum.COD.value:
            return
        if order.delivery_verification_code:
            return
        order.delivery_verification_code = DeliveryVerificationService._generate_code()
        order.delivery_verification_status = "pending"
        logger.info(f"Delivery verification code generated for order {order.order_number}")

    @staticmethod
    async def confirm(db: AsyncSession, order: Order, submitted_code: str) -> Tuple[bool, str]:
        """
        Returns (matched, message). Attempt-limited
        (``settings.delivery_verification_max_attempts``) and constant-time,
        same pattern as ``OTPService.verify_otp``. Commits its own change.
        """
        if order.delivery_verification_status != "pending":
            return False, "This order has no pending verification code."

        max_attempts = settings.delivery_verification_max_attempts
        if (order.delivery_verification_attempts or 0) >= max_attempts:
            order.delivery_verification_status = "failed"
            await db.commit()
            return False, "Too many attempts."

        order.delivery_verification_attempts = (order.delivery_verification_attempts or 0) + 1
        matched = secrets.compare_digest(
            order.delivery_verification_code or "", (submitted_code or "").strip()
        )

        if matched:
            order.delivery_verification_status = "confirmed"
            order.delivery_verification_confirmed_at = datetime.utcnow()
            message = "Code confirmed."
        elif order.delivery_verification_attempts >= max_attempts:
            order.delivery_verification_status = "failed"
            message = "Too many attempts."
        else:
            remaining = max_attempts - order.delivery_verification_attempts
            message = f"Code did not match. {remaining} attempt(s) left."

        await db.commit()
        if not matched:
            logger.warning(
                f"Delivery verification code mismatch for order {order.order_number} "
                f"(attempt {order.delivery_verification_attempts}/{max_attempts})"
            )
        return matched, message
