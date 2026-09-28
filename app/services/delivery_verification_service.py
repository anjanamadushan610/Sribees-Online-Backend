"""
Delivery Verification Code — handover anti-fraud check.

We generate a short numeric code, show it to the customer in the app, and hand
it to SribeesExpress at booking time (``handover_code`` on
``POST /ecommerce/shipments``). The rider must enter it before releasing the
parcel; SribeesExpress rejects a ``delivered`` transition on a code-required
shipment until it matches. So verification happens on their side now — we
generate, display, and record the outcome that comes back on the webhook.

Same generation shape as ``app/services/otp_service.py`` (crypto-random digits
via ``secrets.randbelow``), but persisted on the ``Order`` row rather than
Redis-TTL'd: the code must survive from "payment confirmed" — possibly hours
before dispatch — until actual handover, and a 3-minute OTP lifetime is wrong
for that.

**Prepaid orders only.** SribeesExpress accepts a handover code on prepaid
shipments and rejects one on a COD shipment (contract section 7: "never send
this for a cod order"), on the reasoning that a rider collecting cash in hand
is its own proof. We are not asking them to change that. A COD parcel is
handed over on the cash exchange alone, and ``cod.collected`` on the delivered
webhook is what records it.
"""
import secrets
from datetime import datetime, timezone
from typing import Optional, Tuple

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.order import Order
from app.schemas.order import PaymentMethodEnum

# SribeesExpress accepts 4-6 digits (contract section 7). Anything outside
# that is rejected at booking, so clamp rather than trusting the setting.
MIN_CODE_DIGITS = 4
MAX_CODE_DIGITS = 6


def is_code_eligible(order: Order) -> bool:
    """
    Whether this order should carry a handover code: prepaid yes, COD no.

    Not a preference -- SribeesExpress rejects a code on a COD shipment with a
    ``400``, so sending one would fail the whole booking (contract section 7).
    """
    return order.payment_method != PaymentMethodEnum.COD.value


# Past these the code has nothing left to do: the parcel is with the customer,
# on its way back, or the order is gone.
_CODE_DONE_STATUSES = frozenset(
    {"delivered", "cancelled", "refunded", "return_requested", "return_approved", "rto_initiated"}
)


def show_code_to_customer(order: Order) -> bool:
    """Whether the app shows the customer their handover code.

    From the moment a prepaid order is paid until the rider takes the code
    (owner, 2026-09-28): they paid in advance, so the code is theirs to keep
    ready -- not only once a rider is already at the door.
    """
    return (
        is_code_eligible(order)
        and bool(order.delivery_verification_code)
        and order.delivery_verification_status == "pending"
        and order.payment_status == "paid"
        and order.status not in _CODE_DONE_STATUSES
    )


class DeliveryVerificationService:
    """Generate the handover code and record the outcome SribeesExpress reports."""

    @staticmethod
    def _generate_code() -> str:
        """A cryptographically-random zero-padded code, length from settings."""
        digits = max(MIN_CODE_DIGITS, min(MAX_CODE_DIGITS, settings.delivery_verification_code_digits))
        return f"{secrets.randbelow(10 ** digits):0{digits}d}"

    @staticmethod
    async def generate_for_order(db: AsyncSession, order: Order) -> None:
        """
        Idempotent: no-op if a code already exists, or if this order is not
        eligible for one. Does not commit — the caller's transaction does
        (mirrors the "does not commit" convention ``OrderService``'s other
        attach helpers already use).
        """
        if not is_code_eligible(order):
            return
        if order.delivery_verification_code:
            return
        order.delivery_verification_code = DeliveryVerificationService._generate_code()
        order.delivery_verification_status = "pending"
        logger.info(f"Delivery verification code generated for order {order.order_number}")

    @staticmethod
    async def record_outcome(
        db: AsyncSession,
        order: Order,
        *,
        verified: bool,
        attempts: Optional[int] = None,
        at: Optional[datetime] = None,
    ) -> None:
        """
        Record what SribeesExpress reported on the ``handover`` block.

        Called from ``courier_sync_service``; kept here so the status
        vocabulary on the order row has one owner. Does not commit -- the
        caller's transaction does.

        Only advances a *pending* verification. A code that already confirmed
        is never walked back by a later, less specific message: SribeesExpress
        sends the handover block on more than one read, and an absent or stale
        `code_verified` must not undo a proven handover.
        """
        if order.delivery_verification_status != "pending":
            return
        order.delivery_verification_status = "confirmed" if verified else "failed"
        if attempts is not None:
            order.delivery_verification_attempts = attempts
        if verified:
            order.delivery_verification_confirmed_at = at or datetime.now(timezone.utc)

    @staticmethod
    async def confirm(db: AsyncSession, order: Order, submitted_code: str) -> Tuple[bool, str]:
        """
        Verify a code against the order — attempt-limited and constant-time.

        Retained for support/admin use only. In the normal flow SribeesExpress
        verifies the code against its own copy and reports the result on the
        webhook, so nothing calls into us to confirm it any more (the old
        ``/courier/webhooks/orders/{ref}/delivery-code/confirm`` endpoint is
        gone with the old contract). Commits its own change.
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
            order.delivery_verification_confirmed_at = datetime.now(timezone.utc)
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
