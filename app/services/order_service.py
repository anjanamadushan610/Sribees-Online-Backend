"""
Order Service - Business Logic
"""
import random
import string
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.order import Order, OrderItem, OrderStatus, PaymentStatus
from app.models.product import Product
from app.models.user import Address
from app.models.wallet import WalletTransactionType
from app.schemas.notification import NotificationTypeEnum
from app.schemas.order import (
    CreateOrderRequest,
    OrderStatusEnum,
    PaymentMethodEnum,
    PaymentStatusEnum,
)
from app.services.app_settings_service import AppSettingsService
from app.services.cart_service import CartService
from app.services.coupon_service import CouponService
from app.services.notification_service import NotificationService
from app.services.pricing_service import PricingService
from app.services.wallet_service import WalletService


class OrderService:
    """Service class for order operations."""

    @staticmethod
    async def _attach_cashback_rates(
        db: AsyncSession,
        items: List[dict],
        branch_id: Optional[UUID],
    ) -> List[dict]:
        """
        Return *items* with each line's branch-resolved cashback rate attached.

        Rates are read from the database, never from the Redis cart: a cart can
        sit for days, and the customer must earn the rate that is live at
        checkout, not the one that happened to apply when they added the item.

        Returns copies — the caller's cart dicts are left untouched so nothing
        stale gets written back to Redis.
        """
        if not items:
            return []

        from app.services.product_service import ProductService

        product_ids = []
        for item in items:
            raw = item.get("product_id")
            if not raw:
                continue
            try:
                product_ids.append(UUID(str(raw)))
            except (ValueError, TypeError):
                # A malformed id cannot be priced from the catalog; the pricing
                # service falls back to the platform rate for it.
                continue

        rates = await ProductService.get_cashback_map(db, product_ids, branch_id)

        priced = []
        for item in items:
            enriched = dict(item)
            rate = rates.get(str(item.get("product_id")))
            if rate is not None:
                enriched["cashback_percentage"] = rate
            priced.append(enriched)
        return priced

    @staticmethod
    def generate_order_number() -> str:
        """Generate unique order number."""
        timestamp = datetime.now().strftime("%Y%m%d")
        random_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        return f"FC-{timestamp}-{random_part}"

    @staticmethod
    async def create_from_cart(
        db: AsyncSession,
        user_id: UUID,
        data: CreateOrderRequest
    ) -> Order:
        """Create order from user's cart."""
        # Get cart
        cart = await CartService.get_cart(str(user_id))

        if not cart["items"]:
            raise ValueError("Cart is empty")

        # Verify address exists
        address_result = await db.execute(
            select(Address).where(
                and_(
                    Address.address_id == UUID(data.delivery_address_id),
                    Address.user_id == user_id
                )
            )
        )
        address = address_result.scalar_one_or_none()

        if not address:
            raise ValueError("Invalid delivery address")

        # Authoritative coupon: re-validate against the database at checkout and
        # use the DB discount values — never the discount stored in the Redis
        # cart (defence in depth against a stale or tampered cart entry).
        coupon_row = None
        coupon_for_pricing = None
        raw_coupon = cart.get("coupon")
        if raw_coupon and raw_coupon.get("code"):
            coupon_row = await CouponService.get_by_code(db, raw_coupon["code"])
            if coupon_row is None:
                raise ValueError("The applied coupon is no longer valid.")
            cart_subtotal = Decimal(
                str(sum(i["price"] * i["quantity"] for i in cart["items"]))
            )
            # Raises CouponValidationError (a ValueError) → 400 at the endpoint.
            CouponService.validate(coupon_row, cart_subtotal, datetime.now(timezone.utc))
            coupon_for_pricing = {
                "code": coupon_row.code,
                "discount_type": coupon_row.discount_type,
                "discount_value": float(coupon_row.discount_value),
            }

        # Authoritative totals — same PricingService used by POST /orders/quote,
        # so the charged amount always equals the previewed quote. Delivery fee
        # and tax rate come from the DB-backed platform settings.
        wallet_balance = Decimal("0")
        if data.use_wallet:
            wallet = await WalletService.get_or_create_wallet(db, user_id)
            wallet_balance = wallet.balance or Decimal("0")

        pricing_config = await AppSettingsService.get_pricing_config(db)

        # Attribute the order to the branch serving the delivery address's post
        # office, so branch-scoped admins can see and manage it. Best-effort:
        # an unmapped post office leaves branch_id NULL (does not block checkout).
        #
        # Resolved before pricing because cashback rates are branch-specific:
        # the same product can earn a different rate in a different branch.
        branch_id = None
        try:
            from app.services import branch_service

            mapping = await branch_service.resolve_branch_by_post_office(
                db, address.post_office
            )
            if mapping is not None:
                branch_id = mapping.branch_id
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not resolve branch for order: {exc}")

        priced_items = await OrderService._attach_cashback_rates(
            db, cart["items"], branch_id
        )

        breakdown = PricingService.quote(
            items=priced_items,
            coupon=coupon_for_pricing,
            use_wallet=data.use_wallet,
            wallet_balance=wallet_balance,
            delivery_fee=pricing_config["delivery_fee"],
            tax_rate=pricing_config["tax_rate"],
        )

        wallet_deduction = breakdown.wallet_deduction
        cashback_earned = breakdown.cashback_earned
        order_number = OrderService.generate_order_number()

        # Payment status. COD is collected on delivery, so it is always PENDING.
        # A WALLET order whose total is fully covered by the wallet balance is
        # already PAID; anything with a balance still to collect stays PENDING.
        if data.payment_method == PaymentMethodEnum.WALLET.value and breakdown.total <= 0:
            payment_status_value = PaymentStatus.PAID.value
        else:
            payment_status_value = PaymentStatus.PENDING.value

        # Create order
        order = Order(
            user_id=user_id,
            order_number=order_number,
            branch_id=branch_id,
            subtotal=breakdown.subtotal,
            tax_amount=breakdown.tax,
            shipping_amount=breakdown.delivery_fee,
            discount_amount=breakdown.discount,
            wallet_deduction=wallet_deduction,
            cashback_earned=cashback_earned,
            total_amount=breakdown.total,
            delivery_address_id=UUID(data.delivery_address_id),
            delivery_slot_date=data.delivery_slot_date,
            delivery_slot_time=data.delivery_slot_time,
            payment_method=data.payment_method,
            coupon_code=coupon_row.code if coupon_row else data.coupon_code,
            notes=data.notes,
            status=OrderStatus.PENDING.value,
            payment_status=payment_status_value
        )

        db.add(order)
        await db.flush()  # Get order_id

        # Redeem the coupon atomically within this transaction. The conditional
        # UPDATE guards used_count < usage_limit under a row lock, so two orders
        # racing for the final slot cannot both succeed. If it lost the race,
        # roll the whole order back and surface a 400.
        if coupon_row is not None:
            redeemed = await CouponService.atomic_increment_usage(db, coupon_row.coupon_id)
            if not redeemed:
                await db.rollback()
                raise ValueError("This coupon has reached its usage limit.")

        # Create order items
        for item in cart["items"]:
            order_item = OrderItem(
                order_id=order.order_id,
                product_id=UUID(item["product_id"]),
                variant_id=UUID(item["variant_id"]) if item.get("variant_id") else None,
                product_name=item["name"],
                product_sku=item.get("sku"),
                product_image=item.get("image"),
                quantity=item["quantity"],
                unit_price=Decimal(str(item["price"])),
                subtotal=Decimal(str(item["price"] * item["quantity"]))
            )
            db.add(order_item)

        # Record wallet movements atomically with the order.
        # Spend first so the earned cashback isn't immediately spendable on the
        # same order.
        if wallet_deduction > 0:
            await WalletService.apply_transaction(
                db,
                user_id=user_id,
                tx_type=WalletTransactionType.SPENT,
                amount=wallet_deduction,
                title="Wallet payment",
                order_id=order.order_id,
                order_number=order_number,
            )
        if cashback_earned > 0:
            await WalletService.apply_transaction(
                db,
                user_id=user_id,
                tx_type=WalletTransactionType.EARNED,
                amount=cashback_earned,
                title="Order cashback",
                order_id=order.order_id,
                order_number=order_number,
            )

        # Record what this order owes the buyer's referrer, if they have one.
        # Staged in this transaction as `pending`; it only becomes claimable
        # when the order is delivered.
        from app.services.referral_service import ReferralService

        await ReferralService.accrue_for_order(db, order)

        await db.commit()
        await db.refresh(order)

        # Clear cart after successful order
        await CartService.clear_cart(str(user_id))

        logger.info(
            f"Order created: {order.order_number} for user {user_id} "
            f"(wallet -{wallet_deduction}, cashback +{cashback_earned})"
        )

        # Notify the user their order was placed. Best-effort: a notification
        # failure must never roll back a successfully committed order.
        try:
            await NotificationService.create(
                db,
                user_id=user_id,
                type=NotificationTypeEnum.ORDER_STATUS,
                title="Order Confirmed",
                message=(
                    f"Your order #{order.order_number} has been placed "
                    f"successfully."
                ),
                reference_type="order",
                reference_id=order.order_id,
                data={
                    "order_number": order.order_number,
                    "status": order.status,
                },
                send_push=True,
            )
        except Exception as e:
            logger.warning(
                f"Order {order.order_number} placed but confirmation "
                f"notification failed: {e}"
            )

        return order

    @staticmethod
    async def reorder(
        db: AsyncSession,
        order_id: UUID,
        user_id: UUID,
    ) -> Tuple[dict, list]:
        """
        Re-add a past order's items to the user's cart at CURRENT prices.

        Products that are missing, inactive or out of stock are skipped; the
        requested quantity is capped at available stock. Returns the updated
        cart and the list of skipped (unavailable) product names.

        Raises ValueError if the order is invalid or nothing could be re-added.
        """
        order = await OrderService.get_by_id(db, order_id, user_id)
        if not order:
            raise ValueError("Order not found")
        if not order.items:
            raise ValueError("This order has no items to reorder")

        added_any = False
        unavailable: list = []

        for item in order.items:
            product = await db.get(Product, item.product_id)
            available_stock = product.stock_quantity if product else 0

            if not product or not product.is_active or available_stock <= 0:
                unavailable.append(item.product_name)
                continue

            quantity = min(item.quantity, available_stock)
            await CartService.add_item(
                user_id=str(user_id),
                product_id=str(item.product_id),
                quantity=quantity,
                price=float(product.price),
                name=product.name or item.product_name,
                image=item.product_image,
                sku=product.sku or item.product_sku,
                variant_id=str(item.variant_id) if item.variant_id else None,
            )
            added_any = True

        if not added_any:
            raise ValueError("None of the items are available to reorder")

        cart = await CartService.get_cart(str(user_id))
        return cart, unavailable

    @staticmethod
    async def get_by_id(
        db: AsyncSession,
        order_id: UUID,
        user_id: Optional[UUID] = None
    ) -> Optional[Order]:
        """Get order by ID."""
        query = (
            select(Order)
            .options(
                selectinload(Order.items),
                selectinload(Order.delivery_address)
            )
            .where(Order.order_id == order_id)
        )

        if user_id:
            query = query.where(Order.user_id == user_id)

        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_order_number(
        db: AsyncSession,
        order_number: str,
        user_id: Optional[UUID] = None
    ) -> Optional[Order]:
        """Get order by order number."""
        query = (
            select(Order)
            .options(
                selectinload(Order.items),
                selectinload(Order.delivery_address)
            )
            .where(Order.order_number == order_number)
        )

        if user_id:
            query = query.where(Order.user_id == user_id)

        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_user_orders(
        db: AsyncSession,
        user_id: UUID,
        limit: int = 20,
        offset: int = 0,
        status: Optional[str] = None
    ) -> Tuple[List[Order], int]:
        """Get user's orders."""
        query = (
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.user_id == user_id)
        )

        if status:
            query = query.where(Order.status == status)

        # Count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar()

        # Get orders
        query = query.order_by(Order.created_at.desc()).limit(limit).offset(offset)
        result = await db.execute(query)
        orders = result.scalars().all()

        return orders, total

    @staticmethod
    async def get_all_orders(
        db: AsyncSession,
        limit: int = 20,
        offset: int = 0,
        status: Optional[str] = None,
        payment_status: Optional[str] = None
    ) -> Tuple[List[Order], int]:
        """Get all orders (admin)."""
        query = select(Order).options(
            selectinload(Order.items),
            selectinload(Order.user)
        )

        if status:
            query = query.where(Order.status == status)

        if payment_status:
            query = query.where(Order.payment_status == payment_status)

        # Count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar()

        # Get orders
        query = query.order_by(Order.created_at.desc()).limit(limit).offset(offset)
        result = await db.execute(query)
        orders = result.scalars().all()

        return orders, total

    @staticmethod
    async def _release_order_coupon(db: AsyncSession, order: Order) -> None:
        """
        Release the coupon redemption held by this order (if any) back to the
        public pool. Best-effort and idempotent at the call sites: callers must
        only invoke this on the *transition* into a cancelled state so a slot is
        never released twice for the same order. Does not commit.
        """
        if not order.coupon_code:
            return
        coupon = await CouponService.get_by_code(db, order.coupon_code)
        if coupon is None:
            return
        released = await CouponService.atomic_decrement_usage(db, coupon.coupon_id)
        if released:
            logger.info(
                f"Coupon {coupon.code} redemption released by cancelled "
                f"order {order.order_number}"
            )

    @staticmethod
    async def _reverse_order_financials(db: AsyncSession, order: Order) -> None:
        """
        Reverse every financial effect of an order on cancellation:

          1. Release the coupon redemption slot.
          2. Refund the wallet balance the customer spent (``wallet_deduction``)
             via a REFUND credit.
          3. Claw back the cashback credited for this order (``cashback_earned``)
             via a SPENT debit.

        All movements are staged in the CALLER'S transaction (``apply_transaction``
        flushes but never commits), so they commit atomically with the coupon
        release and the order status change — or roll back together on any error.

        The clawback may drive the wallet balance negative if the customer has
        already spent that cashback elsewhere. That is intentional: it records
        the debt and keeps the append-only ledger double-entry accurate.
        ``Wallet.balance`` has no non-negative constraint, so this is safe.

        Must be called exactly once per cancellation (guarded by the callers on
        the transition INTO the cancelled state) so effects are never doubled.
        """
        # 1. Coupon slot back to the public pool.
        await OrderService._release_order_coupon(db, order)

        # 2. Refund what the customer paid from their wallet.
        wallet_deduction = order.wallet_deduction or Decimal("0")
        if wallet_deduction > 0:
            await WalletService.apply_transaction(
                db,
                user_id=order.user_id,
                tx_type=WalletTransactionType.REFUND,
                amount=wallet_deduction,
                title="Order cancellation refund",
                order_id=order.order_id,
                order_number=order.order_number,
                notes="Wallet balance refunded on order cancellation",
            )
            logger.info(
                f"Wallet refund {wallet_deduction} for cancelled order "
                f"{order.order_number} (user {order.user_id})"
            )

        # 3. Claw back the cashback earned on this now-cancelled order. May go
        #    negative if already spent (records the debt).
        cashback_earned = order.cashback_earned or Decimal("0")
        if cashback_earned > 0:
            await WalletService.apply_transaction(
                db,
                user_id=order.user_id,
                tx_type=WalletTransactionType.SPENT,
                amount=cashback_earned,
                title="Order cancellation cashback reversal",
                order_id=order.order_id,
                order_number=order.order_number,
                notes="Cashback clawed back on order cancellation (balance may go negative)",
            )
            logger.info(
                f"Cashback clawback {cashback_earned} for cancelled order "
                f"{order.order_number} (user {order.user_id})"
            )

        # 4. Void the referrer's commission on this order. If it was already
        #    paid out, it comes back out of their wallet — otherwise ordering,
        #    claiming and cancelling in a loop would be free money.
        from app.services.referral_service import ReferralService

        await ReferralService.reverse_for_order(
            db,
            order.order_id,
            reason=f"Order {order.order_number} cancelled",
        )

    @staticmethod
    async def update_status(
        db: AsyncSession,
        order: Order,
        status: OrderStatusEnum
    ) -> Order:
        """Update order status."""
        # Detect the transition INTO cancelled so we release the coupon exactly
        # once (a no-op re-cancel must not double-release).
        was_cancelled = order.status == OrderStatus.CANCELLED.value

        was_delivered = order.status == OrderStatus.DELIVERED.value

        order.status = status.value

        # Set timestamps based on status
        now = datetime.utcnow()
        if status == OrderStatusEnum.SHIPPED:
            order.shipped_at = now
        elif status == OrderStatusEnum.DELIVERED:
            order.delivered_at = now
            if not was_delivered:
                # Referral commission only becomes real once the goods arrive —
                # accruing on placement would let someone order, claim, cancel.
                from app.services.referral_service import ReferralService

                await ReferralService.mark_claimable_for_order(db, order.order_id)
        elif status == OrderStatusEnum.CANCELLED:
            order.cancelled_at = now
            if not was_cancelled:
                # Reverse coupon + wallet effects atomically within this txn.
                await OrderService._reverse_order_financials(db, order)

        await db.commit()
        await db.refresh(order)

        logger.info(f"Order {order.order_number} status updated to {status.value}")

        return order

    @staticmethod
    async def update_payment_status(
        db: AsyncSession,
        order: Order,
        payment_status: PaymentStatusEnum,
        payment_id: Optional[str] = None
    ) -> Order:
        """Update payment status."""
        order.payment_status = payment_status.value

        if payment_id:
            order.payment_id = payment_id

        # If payment successful, confirm order
        if payment_status == PaymentStatusEnum.PAID and order.status == OrderStatus.PENDING.value:
            order.status = OrderStatus.CONFIRMED.value

        await db.commit()
        await db.refresh(order)

        logger.info(f"Order {order.order_number} payment status updated to {payment_status.value}")

        return order

    @staticmethod
    async def cancel_order(
        db: AsyncSession,
        order: Order,
        reason: Optional[str] = None
    ) -> Order:
        """Cancel an order."""
        # Check if order can be cancelled
        non_cancellable = [
            OrderStatus.SHIPPED.value,
            OrderStatus.OUT_FOR_DELIVERY.value,
            OrderStatus.DELIVERED.value,
            OrderStatus.CANCELLED.value
        ]

        if order.status in non_cancellable:
            raise ValueError(f"Order cannot be cancelled in {order.status} status")

        order.status = OrderStatus.CANCELLED.value
        order.cancelled_at = datetime.utcnow()

        if reason:
            order.notes = f"{order.notes or ''}\nCancellation reason: {reason}".strip()

        # Reverse all financial effects (coupon slot, wallet refund, cashback
        # clawback) atomically within this transaction. The non-cancellable
        # guard above ensures this runs only on a real cancellation transition.
        await OrderService._reverse_order_financials(db, order)

        await db.commit()
        await db.refresh(order)

        logger.info(f"Order {order.order_number} cancelled")

        return order

    # ================================================================
    # Returns & Refunds (Module 5.5)
    # ================================================================

    @staticmethod
    def _compute_return_refund(order: Order) -> Decimal:
        """
        Value to refund for a return: the returned items' subtotals.

        With no ``return_items`` recorded it is a full return (sum of every
        item's subtotal). Otherwise it sums ``unit_price × min(requested_qty,
        ordered_qty)`` for each selected item.
        """
        items = order.items or []
        return_items = order.return_items or []

        if not return_items:
            total = sum((i.subtotal or Decimal("0")) for i in items)
            return Decimal(str(total)).quantize(Decimal("0.01"))

        by_id = {str(i.order_item_id): i for i in items}
        total = Decimal("0")
        for ri in return_items:
            item = by_id.get(str(ri.get("order_item_id")))
            if item is None:
                continue
            qty = min(int(ri.get("quantity", 0)), item.quantity)
            if qty > 0:
                total += (item.unit_price or Decimal("0")) * qty
        return total.quantize(Decimal("0.01"))

    @staticmethod
    async def request_return(
        db: AsyncSession,
        order: Order,
        reason: str,
        comments: Optional[str] = None,
        items: Optional[list] = None,
    ) -> Order:
        """
        Customer request to return a DELIVERED order. Moves it to
        RETURN_REQUESTED and records the reason/comments/items.
        """
        if order.status != OrderStatus.DELIVERED.value:
            raise ValueError("Only delivered orders can be returned.")

        order.status = OrderStatus.RETURN_REQUESTED.value
        order.return_reason = reason
        order.return_comments = comments
        order.return_items = (
            [{"order_item_id": str(i.order_item_id), "quantity": i.quantity} for i in items]
            if items
            else None
        )
        order.return_requested_at = datetime.utcnow()

        await db.commit()
        await db.refresh(order)
        logger.info(f"Return requested for order {order.order_number}: {reason}")
        return order

    @staticmethod
    async def approve_return(db: AsyncSession, order: Order) -> Order:
        """
        Approve a pending return: credit the returned value to the customer's
        wallet (REFUND) and move the order to REFUNDED — all in one transaction.
        """
        if order.status != OrderStatus.RETURN_REQUESTED.value:
            raise ValueError("This order has no pending return request.")

        refund = OrderService._compute_return_refund(order)

        if refund > 0:
            await WalletService.apply_transaction(
                db,
                user_id=order.user_id,
                tx_type=WalletTransactionType.REFUND,
                amount=refund,
                title="Return refund",
                order_id=order.order_id,
                order_number=order.order_number,
                notes="Wallet credited for approved return",
            )

        order.refund_amount = refund
        order.status = OrderStatus.REFUNDED.value

        await db.commit()
        await db.refresh(order)
        logger.info(f"Return approved for order {order.order_number}: refunded {refund}")

        # Best-effort customer notification (never blocks the refund).
        try:
            await NotificationService.notify_order_status(
                db, order.user_id, order.order_number, OrderStatus.REFUNDED.value
            )
        except Exception as exc:  # pragma: no cover - notification is best-effort
            logger.warning(f"Return-approved notification failed for {order.order_number}: {exc}")

        return order

    @staticmethod
    async def reject_return(db: AsyncSession, order: Order) -> Order:
        """Reject a pending return: revert the order back to DELIVERED."""
        if order.status != OrderStatus.RETURN_REQUESTED.value:
            raise ValueError("This order has no pending return request.")

        order.status = OrderStatus.DELIVERED.value
        await db.commit()
        await db.refresh(order)
        logger.info(f"Return rejected for order {order.order_number}")
        return order

    @staticmethod
    async def get_order_count(
        db: AsyncSession,
        user_id: Optional[UUID] = None,
        status: Optional[str] = None
    ) -> int:
        """Get order count."""
        query = select(func.count(Order.order_id))

        if user_id:
            query = query.where(Order.user_id == user_id)

        if status:
            query = query.where(Order.status == status)

        result = await db.execute(query)
        return result.scalar() or 0

    @staticmethod
    async def get_revenue_stats(
        db: AsyncSession,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> dict:
        """Get revenue statistics (admin)."""
        query = select(
            func.count(Order.order_id).label("total_orders"),
            func.sum(Order.total_amount).label("total_revenue"),
            func.avg(Order.total_amount).label("average_order_value")
        ).where(Order.payment_status == PaymentStatus.PAID.value)

        if start_date:
            query = query.where(Order.created_at >= start_date)

        if end_date:
            query = query.where(Order.created_at <= end_date)

        result = await db.execute(query)
        row = result.one()

        return {
            "total_orders": row.total_orders or 0,
            "total_revenue": float(row.total_revenue or 0),
            "average_order_value": float(row.average_order_value or 0)
        }
