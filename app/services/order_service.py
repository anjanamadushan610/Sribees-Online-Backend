"""
Order Service - Business Logic
"""
import random
import string
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config.settings import settings
from app.models.branch import Branch
from app.models.order import (
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusHistory,
    PaymentStatus,
)
from app.models.product import Product
from app.models.user import Address, User
from app.models.wallet import WalletTransactionType
from app.schemas.notification import NotificationTypeEnum
from app.schemas.order import (
    GATEWAY_PAYMENT_METHODS,
    MARX_PAYMENT_METHOD_MAP,
    CreateOrderRequest,
    OrderStatusEnum,
    PaymentMethodEnum,
    PaymentStatusEnum,
    to_courier_payment_method,
)
from app.services.app_settings_service import AppSettingsService
from app.services.cart_service import CartService
from app.services.coupon_service import CouponService
from app.services.courier_service import (
    CourierQuoteExpiredError,
    CourierService,
    CourierUnavailableError,
)
from app.services.delivery_verification_service import DeliveryVerificationService
from app.services.marx_ipg_service import MarxIPGError, MarxIPGService
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
    async def _attach_weights(db: AsyncSession, items: List[dict]) -> List[dict]:
        """
        Return *items* with each line's DB-resolved, kg-normalized weight
        attached as ``weight_kg`` — for the courier's weight-based rate quote.

        Same shape as ``_attach_cashback_rates``: resolved fresh from the
        database, never from the Redis cart, and returns copies so nothing
        stale is written back.
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
                continue

        weights = await ProductService.get_weight_map(db, product_ids)

        weighted = []
        for item in items:
            enriched = dict(item)
            weight = weights.get(str(item.get("product_id")))
            if weight is not None:
                enriched["weight_kg"] = weight
            weighted.append(enriched)
        return weighted

    @staticmethod
    def generate_order_number() -> str:
        """Generate unique order number using Asia/Colombo date."""
        colombo_tz = ZoneInfo("Asia/Colombo")
        timestamp = datetime.now(colombo_tz).strftime("%Y%m%d")
        random_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        return f"FC-{timestamp}-{random_part}"

    @staticmethod
    async def create_from_cart(
        db: AsyncSession,
        user_id: UUID,
        data: CreateOrderRequest
    ) -> Tuple[Order, Optional[str]]:
        """
        Create order from user's cart.

        Returns ``(order, payment_redirect_url)`` — the second element is only
        set for a Marx IPG gateway order the shopper must be redirected to pay
        (``None`` for COD/WALLET/wallet-fully-covered-gateway orders, which
        complete or stay pending exactly as before).
        """
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
                "max_discount_amount": (
                    float(coupon_row.max_discount_amount)
                    if coupon_row.max_discount_amount is not None
                    else None
                ),
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
        # office, so branch-scoped admins can see and manage it, and so there
        # is an origin to quote the courier with. Best-effort: an unmapped
        # post office leaves branch_id NULL (does not block checkout).
        #
        # Resolved before pricing because cashback rates (and the courier
        # quote) are branch-specific.
        branch_id = None
        branch = None
        try:
            from app.services import branch_service

            mapping = await branch_service.resolve_branch_by_post_office(
                db, address.post_office
            )
            if mapping is not None:
                branch_id = mapping.branch_id
                branch = mapping.branch
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not resolve branch for order: {exc}")

        priced_items = await OrderService._attach_cashback_rates(
            db, cart["items"], branch_id
        )
        priced_items = await OrderService._attach_weights(db, priced_items)

        origin = branch_service.to_courier_origin(branch) if branch is not None else None
        destination = {
            "recipient_name": address.recipient_name,
            "phone": address.phone,
            "address_line1": address.address_line1,
            "address_line2": address.address_line2,
            "post_office": address.post_office,
            "district": address.district,
            "province": address.province,
            "postal_code": address.postal_code,
        }

        breakdown = await PricingService.quote(
            items=priced_items,
            coupon=coupon_for_pricing,
            use_wallet=data.use_wallet,
            wallet_balance=wallet_balance,
            delivery_fee=pricing_config["delivery_fee"],
            tax_rate=pricing_config["tax_rate"],
            origin=origin,
            destination=destination,
            payment_method=to_courier_payment_method(data.payment_method),
        )

        wallet_deduction = breakdown.wallet_deduction
        cashback_earned = breakdown.cashback_earned
        order_number = OrderService.generate_order_number()

        # Marx IPG gateway orders: create the order on Marx's side BEFORE
        # touching our own database -- if it fails, nothing here needs
        # undoing. If the wallet balance alone already covers the total there
        # is nothing to charge the gateway for; fall through and treat it
        # exactly like a fully-covered WALLET order.
        is_gateway = data.payment_method in GATEWAY_PAYMENT_METHODS
        payment_redirect_url = None
        gateway_tr_id = None

        if is_gateway and breakdown.total > 0:
            user = await db.get(User, user_id)
            marx_method = MARX_PAYMENT_METHOD_MAP[data.payment_method]
            # Marx requires a real customerMail. Phone-only accounts (this
            # app's primary signup path) are asked for one right after OTP
            # verification (see CompleteProfileScreen), so this should only
            # fire for an account that predates that screen -- surface a
            # clear, actionable error rather than inventing an address no
            # one reads, which a payment processor should never be handed.
            if not user or not user.email:
                raise ValueError(
                    "Please add your email in Profile before paying by card or bank transfer."
                )
            try:
                gw = await MarxIPGService.create_order(
                    merchant_rid=order_number,
                    amount=breakdown.total,
                    return_url=settings.marx_ipg_return_url,
                    customer_mail=user.email,
                    customer_mobile=user.phone if user else None,
                    order_summary=f"SRIBEESonline order {order_number}",
                    customer_reference=(user.full_name if user else None) or order_number,
                    marx_payment_method=marx_method,
                )
            except MarxIPGError as exc:
                raise ValueError(f"Could not start payment: {exc}") from exc
            payment_redirect_url = gw.get("pay_url")
            gateway_tr_id = str(gw["tr_id"]) if gw.get("tr_id") is not None else None
        elif is_gateway:
            is_gateway = False

        # Payment status. COD is collected on delivery, so it is always
        # PENDING regardless of total. A WALLET order (or a gateway-method
        # order the wallet alone fully covers) whose total is fully covered is
        # already PAID; a real gateway order stays PENDING until Marx confirms
        # it (see _on_payment_confirmed / the /payments/marx/confirm endpoint).
        if breakdown.total <= 0 and data.payment_method in (
            PaymentMethodEnum.WALLET.value, *GATEWAY_PAYMENT_METHODS
        ):
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
            payment_status=payment_status_value,
            gateway_tr_id=gateway_tr_id,
            courier_quote_id=breakdown.courier_quote_id,
            courier_quote_expires_at=breakdown.courier_quote_expires_at,
        )

        db.add(order)
        await db.flush()  # Get order_id

        history = OrderStatusHistory(
            order_id=order.order_id,
            old_status=None,
            new_status=OrderStatus.PENDING.value,
            changed_by="Customer",
            notes="Order placed by customer",
            created_at=datetime.now(ZoneInfo("Asia/Colombo")),
        )
        db.add(history)

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
        # Cashback is credited to the wallet immediately for COD/WALLET (paid
        # synchronously or collected on delivery), but DEFERRED for gateway
        # methods until Marx actually confirms the payment succeeded --
        # crediting it here for a card payment that later fails or is
        # abandoned would be wrong. See OrderService._on_payment_confirmed.
        if cashback_earned > 0 and not is_gateway:
            await WalletService.apply_transaction(
                db,
                user_id=user_id,
                tx_type=WalletTransactionType.EARNED,
                amount=cashback_earned,
                title="Order cashback",
                order_id=order.order_id,
                order_number=order_number,
            )
            order.cashback_credited_at = datetime.utcnow()

        # Record what this order owes the buyer's referrer, if they have one.
        # Staged in this transaction as `pending`; it only becomes claimable
        # when the order is delivered.
        from app.services.referral_service import ReferralService

        await ReferralService.accrue_for_order(db, order)

        await db.commit()
        await db.refresh(order)

        # Clear cart after successful order -- except a real (pending) gateway
        # order, where the shopper hasn't paid yet and may abandon the hosted
        # page. That case clears the cart in _on_payment_confirmed instead.
        if not is_gateway:
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

        # Payment already confirmed (WALLET / wallet-fully-covered-gateway
        # orders): run the same "just got paid" side effects a Marx confirm or
        # the reconciliation job would trigger later (deferred cashback,
        # delivery code, courier booking). COD books its courier shipment
        # immediately since there's nothing to wait for; a real pending
        # gateway order does neither yet -- both happen once Marx confirms.
        if payment_status_value == PaymentStatus.PAID.value:
            await OrderService._on_payment_confirmed(db, order)
        elif data.payment_method == PaymentMethodEnum.COD.value:
            await OrderService._book_courier_shipment(db, order)

        return order, payment_redirect_url

    @staticmethod
    async def _on_payment_confirmed(db: AsyncSession, order: Order) -> None:
        """
        Run once an order's payment_status has just become PAID — either
        synchronously in create_from_cart (WALLET / wallet-fully-covered
        gateway order) or from update_payment_status's PENDING -> CONFIRMED
        transition (a Marx confirm, the reconciliation job, or an admin
        override). Every side effect here is independently idempotent, so
        calling this twice for the same order (e.g. a confirm racing the
        reconciliation sweep) is safe.
        """
        # 1. Deferred cashback credit (gateway methods only -- COD/WALLET are
        #    already credited inline at creation, guarded there the same way).
        if (
            order.payment_method in GATEWAY_PAYMENT_METHODS
            and order.cashback_earned
            and order.cashback_earned > 0
            and order.cashback_credited_at is None
        ):
            await WalletService.apply_transaction(
                db,
                user_id=order.user_id,
                tx_type=WalletTransactionType.EARNED,
                amount=order.cashback_earned,
                title="Order cashback",
                order_id=order.order_id,
                order_number=order.order_number,
            )
            order.cashback_credited_at = datetime.utcnow()

        # 2. Delivery handover verification code (non-COD only; idempotent).
        await DeliveryVerificationService.generate_for_order(db, order)

        # 3. Cart clearing deferred from create_from_cart for gateway orders.
        if order.payment_method in GATEWAY_PAYMENT_METHODS:
            await CartService.clear_cart(str(order.user_id))

        await db.commit()
        await db.refresh(order)

        # 4. Courier booking -- separate commit inside, best-effort (a courier
        #    outage must never undo a paid order).
        await OrderService._book_courier_shipment(db, order)

    @staticmethod
    async def _book_courier_shipment(db: AsyncSession, order: Order) -> None:
        """
        Book the courier shipment for a confirmed order (docs/COURIER_API.md
        section 5). Idempotent no-op if already booked. Best-effort: a courier
        outage must never roll back a paid/placed order, so failures are
        logged and recorded on the order rather than raised.
        """
        if order.courier_shipment_id:
            return
        if not order.branch_id or not order.delivery_address_id:
            logger.warning(
                f"Order {order.order_number}: no branch/address resolved, "
                f"skipping courier booking"
            )
            return

        from app.services import branch_service

        branch = await db.get(Branch, order.branch_id)
        address = await db.get(Address, order.delivery_address_id)
        if branch is None or address is None:
            logger.warning(
                f"Order {order.order_number}: branch/address row missing, "
                f"skipping courier booking"
            )
            return

        origin = branch_service.to_courier_origin(branch)
        destination = {
            "recipient_name": address.recipient_name,
            "phone": address.phone,
            "address_line1": address.address_line1,
            "address_line2": address.address_line2,
            "post_office": address.post_office,
            "district": address.district,
            "province": address.province,
            "postal_code": address.postal_code,
        }
        # Query order items directly rather than via order.items -- that
        # relationship is not always eagerly loaded on the Order instance this
        # is called with (e.g. straight out of create_from_cart), and a lazy
        # load would fail under the async session.
        item_rows = (
            await db.execute(
                select(OrderItem.product_id, OrderItem.quantity).where(
                    OrderItem.order_id == order.order_id
                )
            )
        ).all()
        priced_items = await OrderService._attach_weights(
            db,
            [{"product_id": str(product_id), "quantity": quantity} for product_id, quantity in item_rows],
        )
        weight_kg = sum(
            Decimal(str(i.get("weight_kg") or 0)) * Decimal(str(i["quantity"]))
            for i in priced_items
        )
        item_count = sum(i["quantity"] for i in priced_items)
        is_cod = order.payment_method == PaymentMethodEnum.COD.value
        courier_payment_method = to_courier_payment_method(order.payment_method)

        try:
            result = await CourierService.book_shipment(
                order_number=order.order_number,
                quote_id=order.courier_quote_id,
                origin=origin,
                destination=destination,
                weight_kg=weight_kg,
                item_count=item_count,
                payment_method=courier_payment_method,
                cod_amount_lkr=order.total_amount if is_cod else Decimal("0"),
            )
        except CourierQuoteExpiredError:
            # The customer already paid/agreed the OLD shipping_amount; we
            # cannot re-bill them for a higher courier quote now. Re-quote and
            # book at the new price, absorbing any positive delta -- this is a
            # deliberate default that needs product/finance sign-off, not
            # silently assumed (see the plan's Open Risks).
            logger.warning(
                f"Courier quote expired for order {order.order_number}, re-quoting at booking time"
            )
            try:
                fresh = await CourierService.get_rate(
                    origin=origin,
                    destination=destination,
                    weight_kg=weight_kg,
                    item_count=item_count,
                    order_value_lkr=order.subtotal,
                    payment_method=courier_payment_method,
                )
                result = await CourierService.book_shipment(
                    order_number=order.order_number,
                    quote_id=fresh.quote_id,
                    origin=origin,
                    destination=destination,
                    weight_kg=weight_kg,
                    item_count=item_count,
                    payment_method=courier_payment_method,
                    cod_amount_lkr=order.total_amount if is_cod else Decimal("0"),
                )
            except (CourierQuoteExpiredError, CourierUnavailableError) as exc:
                order.courier_booking_status = "failed"
                await db.commit()
                logger.error(f"Courier re-booking failed for order {order.order_number}: {exc}")
                return
        except CourierUnavailableError as exc:
            order.courier_booking_status = "failed"
            await db.commit()
            logger.error(f"Courier booking failed for order {order.order_number}: {exc}")
            return

        order.courier_shipment_id = result.get("shipment_id")
        order.courier_waybill_number = result.get("waybill_number")
        order.courier_tracking_status = result.get("status", "booked")
        order.courier_booking_status = "booked"
        await db.commit()
        logger.info(
            f"Courier shipment booked for order {order.order_number}: "
            f"{order.courier_shipment_id} ({order.courier_waybill_number})"
        )

    @staticmethod
    async def get_by_courier_shipment_id(db: AsyncSession, shipment_id: str) -> Optional[Order]:
        """Look up an order by the courier's shipment id (for inbound webhooks)."""
        result = await db.execute(
            select(Order).where(Order.courier_shipment_id == shipment_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def reconcile_pending_gateway_payments(db: AsyncSession) -> dict:
        """
        Sweep gateway orders stuck in PENDING and resolve them against Marx's
        own record of what happened (doc section 7's own recommendation).

        Only considers orders older than a 10-minute grace period so this
        never races a shopper still on the hosted pay page. Shared by
        ``scripts/reconcile_marx_payments.py`` and the admin-triggered
        endpoint, so both drive the exact same logic.
        """
        cutoff = datetime.utcnow() - timedelta(minutes=10)
        result = await db.execute(
            select(Order).where(
                Order.payment_method.in_(GATEWAY_PAYMENT_METHODS),
                Order.payment_status == PaymentStatus.PENDING.value,
                Order.gateway_tr_id.isnot(None),
                Order.created_at < cutoff,
            )
        )
        orders = result.scalars().all()

        resolved = {"checked": len(orders), "paid": 0, "failed": 0, "still_pending": 0}
        for order in orders:
            try:
                summary = await MarxIPGService.get_order_summary(tr_id=order.gateway_tr_id)
            except MarxIPGError as exc:
                logger.warning(f"Reconciliation: could not fetch summary for {order.order_number}: {exc}")
                resolved["still_pending"] += 1
                continue

            order.gateway_status = summary["summary_result"]
            order.gateway_response_code = summary.get("gateway_code")
            order.gateway_raw_response = summary["raw"]
            order.gateway_confirmed_at = datetime.utcnow()

            if summary["summary_result"] == "SUCCESS" and summary.get("order_status") in ("SUCCESS", "CAPTURED"):
                await db.commit()
                await OrderService.update_payment_status(
                    db, order, PaymentStatusEnum.PAID, payment_id=order.gateway_tr_id
                )
                resolved["paid"] += 1
            elif summary["summary_result"] == "FAILURE":
                order.payment_status = PaymentStatus.FAILED.value
                await db.commit()
                resolved["failed"] += 1
            else:
                await db.commit()
                resolved["still_pending"] += 1

        logger.info(f"Marx reconciliation sweep: {resolved}")
        return resolved

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
        status: OrderStatusEnum,
        changed_by: str = "Admin",
        notes: Optional[str] = None,
    ) -> Order:
        """Update order status and log history."""
        old_status = order.status
        was_cancelled = order.status == OrderStatus.CANCELLED.value
        was_delivered = order.status == OrderStatus.DELIVERED.value

        order.status = status.value

        colombo_tz = ZoneInfo("Asia/Colombo")
        now = datetime.now(colombo_tz)
        if status == OrderStatusEnum.SHIPPED:
            order.shipped_at = now
        elif status == OrderStatusEnum.DELIVERED:
            order.delivered_at = now
            if (
                order.payment_method == PaymentMethodEnum.COD.value
                and order.payment_status != PaymentStatus.PAID.value
            ):
                order.payment_status = PaymentStatus.PAID.value
            if not was_delivered:
                from app.services.referral_service import ReferralService

                await ReferralService.mark_claimable_for_order(db, order.order_id)
        elif status == OrderStatusEnum.CANCELLED:
            order.cancelled_at = now
            if not was_cancelled:
                await OrderService._reverse_order_financials(db, order)

        # Log status transition in audit history
        history = OrderStatusHistory(
            order_id=order.order_id,
            old_status=old_status,
            new_status=status.value,
            changed_by=changed_by,
            notes=notes or f"Status updated to {status.value.replace('_', ' ').title()}",
            created_at=now,
        )
        db.add(history)

        await db.commit()
        await db.refresh(order)

        if status == OrderStatusEnum.CANCELLED and not was_cancelled and order.courier_shipment_id:
            await OrderService._cancel_courier_shipment(order)

        logger.info(f"Order {order.order_number} status updated to {status.value}")

        return order

    @staticmethod
    async def _cancel_courier_shipment(order: Order) -> None:
        """Best-effort courier-side cancellation; never blocks an order cancellation."""
        try:
            await CourierService.cancel_shipment(order.courier_shipment_id)
            logger.info(f"Courier shipment cancelled for order {order.order_number}")
        except CourierUnavailableError as exc:
            logger.warning(
                f"Could not cancel courier shipment for order {order.order_number}: {exc}"
            )

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

        # If payment successful, confirm order and run the "just got paid"
        # side effects (deferred cashback, delivery code, courier booking).
        just_confirmed = (
            payment_status == PaymentStatusEnum.PAID and order.status == OrderStatus.PENDING.value
        )
        if just_confirmed:
            order.status = OrderStatus.CONFIRMED.value

        await db.commit()
        await db.refresh(order)

        if just_confirmed:
            await OrderService._on_payment_confirmed(db, order)
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

        if order.courier_shipment_id:
            await OrderService._cancel_courier_shipment(order)

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
