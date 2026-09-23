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
from app.models.order import (
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusHistory,
    PaymentStatus,
)
from app.models.product import BranchInventory, Product
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
from app.services import (
    courier_credentials,
    courier_location_service,
    delivery_quote_service,
    delivery_verification_service,
)
from app.services.app_settings_service import AppSettingsService
from app.services.cart_service import CartService
from app.services.coupon_service import CouponService
from app.services.courier_service import (
    CourierError,
    CourierQuoteAlreadyUsedError,
    CourierQuoteExpiredError,
    CourierService,
    CourierUnavailableError,
)
from app.services.delivery_verification_service import DeliveryVerificationService
from app.services.marx_ipg_service import MarxIPGError, MarxIPGService
from app.services.notification_service import NotificationService
from app.services.pricing_service import (
    DELIVERY_MESSAGES,
    PricingService,
    cart_weight_kg,
)
from app.services.wallet_service import WalletService


class DeliveryNotAvailableError(ValueError):
    """
    Checkout refused because delivery to the address could not be priced.

    A ValueError, so the order endpoint answers 400 with the message like every
    other checkout refusal. ``status`` is the PricingService delivery status,
    for a client that wants to branch on it rather than on the sentence.
    """

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


# How long after delivery a customer may start a return in the app.
# One constant, read by the guard in `request_return` and by the
# `return_window_expires_at` the app renders its countdown from, so the
# rule and what the customer is told cannot drift apart.
RETURN_WINDOW = timedelta(hours=12)


async def resolve_recipient_name(
    db: AsyncSession, order: Order, address: Optional[Address]
) -> str:
    """
    The name that goes on the parcel, the courier's system and the rider app.

    This used to be ``address.recipient_name or "Customer"``. ``recipient_name``
    is an optional field on the address form -- most people never fill it in,
    because the address is already theirs -- so the fallback was the common
    case, not the rare one. Riders were handed parcels addressed to a person
    called "Customer", and the same string was what SribeesExpress stored and
    showed in its own screens.

    We are the ones who know who this is. The account carries a full name
    (``users.full_name``, NOT NULL, collected at sign-up), so the order of
    preference is:

      1. ``address.recipient_name`` -- someone deliberately said "deliver this
         to my mother", and that beats the account holder's name.
      2. ``users.full_name`` -- the customer themselves.
      3. the order number, and only if a row is somehow nameless. A parcel
         still has to be identifiable at the door; "Customer" identifies
         nobody, an order number identifies exactly one delivery.

    The courier API is asked for shipping and tracking. It is not the record
    of who our customers are, and it should not be the source of a name we
    already hold.
    """
    named = (getattr(address, "recipient_name", None) or "").strip()
    if named:
        return named

    if order.user_id:
        user = await db.get(User, order.user_id)
        full_name = (getattr(user, "full_name", None) or "").strip()
        if full_name:
            return full_name

    return order.order_number


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
        # Get cart or custom order items
        cart = await CartService.get_cart(str(user_id))
        order_items = (
            data.items
            if (data.items is not None and len(data.items) > 0)
            else cart.get("items", [])
        )

        if not order_items:
            raise ValueError("Cart is empty" if not data.is_buy_now else "No items to order")

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

        # Authoritative totals — same PricingService used by POST /orders/quote,
        # so the charged amount always equals the previewed quote. The delivery
        # fee is a fresh SribeesExpress quote; the tax rate comes from the
        # DB-backed platform settings.
        wallet_balance = Decimal("0")
        if data.use_wallet:
            wallet = await WalletService.get_or_create_wallet(db, user_id)
            wallet_balance = wallet.balance or Decimal("0")

        pricing_config = await AppSettingsService.get_pricing_config(db)

        # Attribute the order to the branch serving the delivery address's
        # postal city, so branch-scoped admins can see and manage it, and so the
        # courier quote is priced from that branch. Best-effort: an unmapped
        # postal city leaves branch_id NULL and the quote is made under the
        # account key.
        #
        # Resolved before pricing because cashback rates (and the courier
        # quote) are branch-specific.
        branch_id = None
        try:
            from app.services import branch_service

            mapping = await branch_service.resolve_branch_by_postal_city(
                db, address.postal_city
            )
            if mapping is not None:
                branch_id = mapping.branch_id
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not resolve branch for order: {exc}")

        # Authoritative coupon: re-validate against the database at checkout and
        # use the DB discount values — never the discount stored in the Redis
        # cart (defence in depth against a stale or tampered cart entry).
        #
        # Runs AFTER branch resolution because every remaining rule needs the
        # fulfilling branch: ownership (a Colombo promo must not burn on a Galle
        # order), and clearance exclusion (whether a line is on Quick Sale is a
        # fact about THIS branch's inventory, and a network-wide coupon has no
        # branch of its own to ask). This is the authoritative gate — the check
        # in cart.py is a courtesy so the shopper finds out at apply time.
        coupon_row = None
        coupon_for_pricing = None
        raw_coupon = (
            {"code": data.coupon_code.strip()}
            if (data.coupon_code and data.coupon_code.strip())
            else (None if data.is_buy_now else cart.get("coupon"))
        )
        if raw_coupon and raw_coupon.get("code"):
            coupon_row = await CouponService.get_by_code(db, raw_coupon["code"])
            if coupon_row is None:
                raise ValueError("The applied coupon is no longer valid.")

            # Eligibility is resolved against the branch's CURRENT inventory,
            # not the snapshot in the cart: an item that went on clearance after
            # it was added must not still be discountable.
            enriched_items = await CouponService.enrich_items(
                db, order_items, branch_id
            )
            eligible_subtotal = CouponService.eligible_subtotal(
                coupon_row, enriched_items
            )

            # Customer-specific facts the synchronous validator cannot look up.
            # Checkout is the authoritative gate, so it supplies all of them.
            user_redemptions = await CouponService.redemptions_by_user(
                db, coupon_row.code, user_id
            )
            target_branch = coupon_row.branch_id
            had_completed = (
                await CouponService.has_completed_order(db, user_id, target_branch)
                if coupon_row.first_order_only
                else None
            )

            # Raises CouponValidationError (a ValueError) → 400 at the endpoint.
            # min_order_value is measured against the ELIGIBLE subtotal.
            CouponService.validate(
                coupon_row,
                eligible_subtotal,
                datetime.now(timezone.utc),
                branch_id=branch_id,
                user_id=user_id,
                user_redemptions=user_redemptions,
                user_has_completed_order=had_completed,
            )
            coupon_for_pricing = {
                "code": coupon_row.code,
                "discount_type": coupon_row.discount_type,
                "discount_value": float(coupon_row.discount_value),
                "eligible_subtotal": float(eligible_subtotal),
                "max_discount_amount": (
                    float(coupon_row.max_discount_amount)
                    if coupon_row.max_discount_amount is not None
                    else None
                ),
            }


        priced_items = await OrderService._attach_cashback_rates(
            db, order_items, branch_id
        )
        priced_items = await OrderService._attach_weights(db, priced_items)

        # Matched against the synced directory now if it never was -- a sync
        # since the address was saved may have added its town.
        postal_city_id = await courier_location_service.postal_city_id_for_address(db, address)
        if postal_city_id is None or branch_id is None:
            # Same answer the cart already gave for this address: no branch
            # sells there, or SribeesExpress does not deliver there. Neither
            # has a price, and an order no branch can dispatch is an order
            # nobody ships.
            raise DeliveryNotAvailableError(
                "not_serviceable", DELIVERY_MESSAGES["not_serviceable"]
            )

        # The courier quote is made on behalf of the fulfilling branch -- a
        # branch with its own SribeesExpress account is priced by that account,
        # from its registered postal city; one on the shared account key names
        # its outlet. Always a fresh quote, never the cart's cached one: this is
        # the quote that gets confirmed and later booked.
        async with courier_credentials.optional_for_branch(db, branch_id):
            breakdown = await PricingService.quote(
                items=priced_items,
                coupon=coupon_for_pricing,
                use_wallet=data.use_wallet,
                wallet_balance=wallet_balance,
                tax_rate=pricing_config["tax_rate"],
                postal_city_id=postal_city_id,
                payment_method=to_courier_payment_method(data.payment_method),
                wallet_amount=data.wallet_amount,
                outlet_id=await delivery_quote_service.outlet_for(db, branch_id),
            )
            if not breakdown.delivery_available:
                # No price, no order. There is no flat fee to fall back to, and
                # an order placed without a delivery charge is either a gift of
                # the delivery or a customer billed after the fact.
                raise DeliveryNotAvailableError(
                    breakdown.delivery_status,
                    DELIVERY_MESSAGES.get(breakdown.delivery_status)
                    or "Delivery to this address can't be priced right now.",
                )
            confirmed_quote = await OrderService._confirm_courier_quote(breakdown.courier_quote_id)

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
            contact_phone=data.contact_phone,
            alternate_phone=data.alternate_phone,
            delivery_slot_date=data.delivery_slot_date,
            delivery_slot_time=data.delivery_slot_time,
            payment_method=data.payment_method,
            coupon_code=coupon_row.code if coupon_row else data.coupon_code,
            notes=data.notes,
            status=OrderStatus.PENDING.value,
            payment_status=payment_status_value,
            gateway_tr_id=gateway_tr_id,
            courier_quote_id=confirmed_quote.quote_id,
            courier_quote_expires_at=confirmed_quote.expires_at,
            courier_quote_amount=breakdown.delivery_fee,
            courier_quote_confirmed_at=datetime.now(timezone.utc),
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

            # Charge this order's discount against the campaign budget, under
            # the same row lock and in the same transaction. Two checkouts
            # racing for the last of the budget cannot both succeed, and an
            # order that would overrun the cap is refused outright rather than
            # quietly spending money the branch did not allocate.
            within_budget = await CouponService.atomic_consume_budget(
                db, coupon_row.coupon_id, breakdown.discount
            )
            if not within_budget:
                await db.rollback()
                raise ValueError(
                    "This promotion has reached its budget and is no longer available."
                )

        # Deduct branch inventory
        if branch_id:
            from app.models.product import BranchInventory
            for item in order_items:
                p_id = UUID(str(item["product_id"]))
                res = await db.execute(
                    select(BranchInventory).where(
                        BranchInventory.branch_id == branch_id,
                        BranchInventory.product_id == p_id
                    )
                )
                inv = res.scalars().first()
                if inv is not None:
                    if inv.stock_quantity < item["quantity"]:
                        await db.rollback()
                        item_name = item.get("name") or item.get("product_name", "Product")
                        if not data.is_buy_now:
                            if inv.stock_quantity > 0:
                                try:
                                    await CartService.update_item_quantity(
                                        user_id=str(user_id),
                                        product_id=str(p_id),
                                        quantity=inv.stock_quantity,
                                        variant_id=item.get("variant_id")
                                    )
                                except Exception:
                                    pass
                                raise ValueError(
                                    f"Stock changed for {item_name}. Only {inv.stock_quantity} available. Your cart has been updated."
                                )
                            else:
                                try:
                                    await CartService.remove_item(
                                        user_id=str(user_id),
                                        product_id=str(p_id),
                                        variant_id=item.get("variant_id")
                                    )
                                except Exception:
                                    pass
                                raise ValueError(
                                    f"{item_name} is currently out of stock and was removed from your cart."
                                )
                        else:
                            raise ValueError(
                                f"Only {inv.stock_quantity} available for {item_name}."
                            )
                    inv.stock_quantity = max(0, inv.stock_quantity - item["quantity"])

        # Create order items
        for item in order_items:
            unit_price_val = item.get("price") if "price" in item else item.get("unit_price", 0)
            order_item = OrderItem(
                order_id=order.order_id,
                product_id=UUID(str(item["product_id"])),
                variant_id=UUID(str(item["variant_id"])) if item.get("variant_id") else None,
                product_name=item.get("name") or item.get("product_name", ""),
                product_sku=item.get("sku") or item.get("product_sku"),
                product_image=item.get("image") or item.get("imageUrl") or item.get("product_image"),
                quantity=item["quantity"],
                unit_price=Decimal(str(unit_price_val)),
                subtotal=Decimal(str(unit_price_val * item["quantity"]))
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

        # Clear cart after successful order -- except for Buy Now orders or a real (pending) gateway
        # order, where the shopper hasn't paid yet and may abandon the hosted
        # page. That case clears the cart in _on_payment_confirmed instead.
        if not is_gateway:
            if data.is_buy_now:
                # Direct Buy Now: user's cart is not affected
                pass
            elif data.items is not None and len(data.items) > 0:
                # Partial cart checkout: remove only purchased items
                for item in order_items:
                    try:
                        await CartService.remove_item(
                            user_id=str(user_id),
                            product_id=str(item["product_id"]),
                            variant_id=item.get("variant_id")
                        )
                    except Exception:
                        pass
            else:
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
        # delivery code). Nothing is booked with the courier here: a parcel is
        # booked when the branch marks it ready for pickup, because until then
        # there is nothing for a rider to collect.
        if payment_status_value == PaymentStatus.PAID.value:
            await OrderService._on_payment_confirmed(db, order)

        return order, payment_redirect_url

    @staticmethod
    async def _confirm_courier_quote(quote_id: Optional[str]):
        """
        Hold the checkout quote's price with SribeesExpress for the fulfilment
        window. Must run inside the credential scope that issued the quote.

        Refuses checkout on any failure. The quote was issued seconds ago, so an
        expiry here means the clock or the courier is badly wrong; an outage
        means we cannot promise the price. Either way the honest answer is
        "try again", not an order whose delivery charge nobody will honour.
        """
        if not quote_id:
            raise DeliveryNotAvailableError(
                "unavailable", "Delivery to this address can't be priced right now."
            )
        try:
            return await CourierService.confirm_quote(quote_id)
        except (CourierUnavailableError, CourierError) as exc:
            logger.warning(f"Could not confirm courier quote {quote_id} at checkout: {exc}")
            raise DeliveryNotAvailableError(
                "unavailable",
                "The delivery charge couldn't be confirmed right now. Please try again.",
            ) from exc

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
        # No courier booking: that happens when the branch marks the parcel
        # ready for pickup (update_status -> READY_FOR_PICKUP).

    @staticmethod
    def payment_blocks_dispatch(order: Order) -> Optional[str]:
        """
        Why this order may not be handed to a courier yet, or None.

        A prepaid order (card, bank, wallet) is dispatched only once the money
        has arrived: a parcel collected for an order whose gateway payment
        later fails is goods given away. COD is paid at the door, so it never
        waits.
        """
        if (order.payment_method or "").upper() == PaymentMethodEnum.COD.value:
            return None
        if order.payment_status != PaymentStatus.PAID.value:
            return (
                "This order has not been paid yet. A prepaid order can only be "
                "handed to the courier once its payment is confirmed."
            )
        return None

    @staticmethod
    async def book_courier_shipment_now(db: AsyncSession, order: Order) -> Tuple[bool, str]:
        """
        Book this order's shipment on an operator's instruction.

        Marking an order ready for pickup books it (``update_status``); this is
        the retry for an order already marked ready whose booking did not stick
        — and for an order that predates the integration. Payment is checked
        the same way the transition checks it.

        Returns ``(ok, message)``. Already-booked is reported as a success
        with the existing waybill rather than an error — pressing the button
        twice must not look like a fault, and must never book a second parcel.
        """
        if order.courier_shipment_id:
            return True, (
                f"Already booked with SribeesExpress as {order.courier_shipment_id}."
            )

        blocked = OrderService.payment_blocks_dispatch(order)
        if blocked is not None:
            return False, blocked

        failure = await OrderService._book_courier_shipment(db, order)
        if failure is not None:
            return False, failure
        if not order.courier_shipment_id:
            # Defensive: every failure path above sets a reason, so this means
            # the booker returned success without a shipment id.
            return False, "Booking did not return a shipment id."
        return True, f"Pickup requested. SribeesExpress waybill {order.courier_shipment_id}."

    @staticmethod
    async def _order_weight_kg(db: AsyncSession, order: Order) -> Decimal:
        """The weight this order's parcel is priced on, resolved as checkout resolved it."""
        # Queried directly rather than via order.items -- that relationship is
        # not always eagerly loaded on the Order instance this is called with,
        # and a lazy load would fail under the async session.
        item_rows = (
            await db.execute(
                select(OrderItem.product_id, OrderItem.quantity).where(
                    OrderItem.order_id == order.order_id
                )
            )
        ).all()
        priced_items = await OrderService._attach_weights(
            db,
            [
                {"product_id": str(product_id), "quantity": quantity}
                for product_id, quantity in item_rows
            ],
        )
        return cart_weight_kg(priced_items)

    @staticmethod
    async def _fresh_courier_quote(
        db: AsyncSession, order: Order, address, cod_amount: Decimal, outlet_id: Optional[int]
    ):
        """
        Price this order with SribeesExpress right now, inside the active scope.

        Returns ``(quote, None)`` or ``(None, reason)``. Needed only when the
        quote the customer agreed to can no longer be booked -- its hold lapsed
        (the parcel sat packed for over a week) or it was somehow already used
        -- or the order predates quotes altogether.
        """
        postal_city_id = await courier_location_service.postal_city_id_for_address(db, address)
        if postal_city_id is None:
            return None, (
                f"The delivery address's postal city ({address.postal_city}, {address.district}) "
                "is not one SribeesExpress delivers to. Sync postal cities under Settings -> "
                "Courier, or correct the address."
            )
        quote = await CourierService.get_rate(
            postal_city_id=postal_city_id,
            weight_kg=await OrderService._order_weight_kg(db, order),
            payment_method=to_courier_payment_method(order.payment_method),
            cod_amount=cod_amount,
            outlet_id=outlet_id,
        )
        if not quote.serviceable:
            return None, f"SribeesExpress does not deliver to this postal city ({quote.reason})."
        return quote, None

    @staticmethod
    async def _book_courier_shipment(db: AsyncSession, order: Order) -> Optional[str]:
        """
        Book the SribeesExpress shipment for an order that is ready for pickup
        (contract section 7). Idempotent no-op if already booked.

        Sends what the customer agreed to: the quote confirmed at checkout
        (its price is what they paid for delivery), and how they pay — ``cod``
        with the order total for the rider to collect, or ``prepaid`` with the
        handover code for the rider to verify. Made in the fulfilling branch's
        credential scope, so the parcel is collected from that branch: a branch
        holding its own key is its own SribeesExpress client, collected from its
        registered address; one on the shared account key names its outlet.

        Never raises. Every failure is recorded on ``courier_booking_status``
        and returned as a short reason, which ``update_status`` turns into a
        refused "ready for pickup" and the admin screen shows the operator.

        Returns ``None`` on success (or on an idempotent no-op), else the reason.
        """
        if order.courier_shipment_id:
            return None
        if not order.delivery_address_id:
            logger.warning(
                f"Order {order.order_number}: no delivery address, skipping courier booking"
            )
            return "This order has no delivery address."

        address = await db.get(Address, order.delivery_address_id)
        if address is None:
            logger.warning(
                f"Order {order.order_number}: address row missing, skipping courier booking"
            )
            return "The order's delivery address row is missing."

        is_cod = (order.payment_method or "").upper() == PaymentMethodEnum.COD.value
        cod_amount = order.total_amount if is_cod else Decimal("0")

        # Only send a code SribeesExpress will accept. Today that is prepaid
        # orders only (contract section 7); a code on a COD shipment is a
        # documented 400, which would fail the whole booking.
        handover_code = (
            order.delivery_verification_code
            if delivery_verification_service.is_code_eligible(order)
            else None
        )

        def _failed(reason: str) -> str:
            order.courier_booking_status = "failed"
            logger.error(f"Courier booking failed for order {order.order_number}: {reason}")
            return reason

        try:
            async with courier_credentials.for_branch(db, order.branch_id):
                outlet_id = await delivery_quote_service.outlet_for(db, order.branch_id)
                booking_kwargs = {
                    "merchant_order_ref": order.order_number,
                    "payment_method": to_courier_payment_method(order.payment_method),
                    "recipient_name": await resolve_recipient_name(db, order, address),
                    # The number the customer confirmed at checkout, not
                    # whatever the address record happens to hold: the
                    # address is edited from several screens and this is the
                    # one they agreed to be called on for this parcel. Falls
                    # back to the address for orders placed before the order
                    # carried its own numbers.
                    #
                    # NOTE: the second number does not go to the courier --
                    # SribeesExpress's ShipmentCreateRequest has a single
                    # `recipient_phone` and no secondary contact field. It
                    # stays on the order for our own support and branch staff.
                    "recipient_phone": order.contact_phone or address.phone or "",
                    "address_line1": address.address_line1,
                    "address_line2": address.address_line2,
                    "postal_city": address.postal_city,
                    "district": address.district,
                    "province": address.province,
                    "postal_code": address.postal_code,
                    "cod_amount": cod_amount,
                    "handover_code": handover_code,
                    "outlet_id": outlet_id,
                    "requested_delivery_date": (
                        order.delivery_slot_date.date().isoformat()
                        if order.delivery_slot_date
                        else None
                    ),
                    "requested_delivery_window": order.delivery_slot_time,
                }

                quote_id = order.courier_quote_id
                if not quote_id:
                    # An order from before quotes were confirmed at checkout.
                    fresh, failure = await OrderService._fresh_courier_quote(
                        db, order, address, cod_amount, outlet_id
                    )
                    if failure is not None:
                        reason = _failed(failure)
                        await db.commit()
                        return reason
                    quote_id = fresh.quote_id
                    order.courier_quote_id = fresh.quote_id
                    order.courier_quote_expires_at = fresh.expires_at
                    order.courier_quote_amount = fresh.amount_lkr

                try:
                    result = await CourierService.book_shipment(quote_id=quote_id, **booking_kwargs)
                except (CourierQuoteExpiredError, CourierQuoteAlreadyUsedError) as exc:
                    # The customer already paid the agreed delivery charge; we
                    # cannot re-bill them now. Re-quote and book at today's
                    # price, and record the courier's new price beside the one
                    # charged, so the difference is visible to finance rather
                    # than absorbed silently.
                    logger.warning(
                        f"Courier quote unusable for order {order.order_number} ({exc.code}), re-quoting"
                    )
                    fresh, failure = await OrderService._fresh_courier_quote(
                        db, order, address, cod_amount, outlet_id
                    )
                    if failure is not None:
                        reason = _failed(failure)
                        await db.commit()
                        return reason
                    order.courier_quote_id = fresh.quote_id
                    order.courier_quote_expires_at = fresh.expires_at
                    order.courier_quote_amount = fresh.amount_lkr
                    if fresh.amount_lkr != (order.shipping_amount or Decimal("0")):
                        logger.warning(
                            f"Order {order.order_number} re-quoted at LKR {fresh.amount_lkr}; "
                            f"the customer was charged LKR {order.shipping_amount}"
                        )
                    result = await CourierService.book_shipment(
                        quote_id=fresh.quote_id, **booking_kwargs
                    )
        except (courier_credentials.CourierNotConfiguredError, courier_credentials.DecryptionError) as exc:
            reason = _failed(str(exc))
            await db.commit()
            return reason
        except (CourierUnavailableError, CourierError) as exc:
            reason = _failed(f"SribeesExpress refused or could not be reached: {exc}")
            await db.commit()
            return reason

        shipment_id = result.get("shipment_id")
        order.courier_shipment_id = shipment_id
        # One identifier under this contract (section 11.1) -- the waybill
        # column stays populated because GET /orders still returns it.
        order.courier_waybill_number = shipment_id
        order.courier_tracking_status = result.get("status", "booked")
        order.courier_tracking_url = result.get("tracking_url")
        order.courier_pickup_location_name = result.get("pickup_location_name")
        order.courier_handover_required = bool((result.get("handover") or {}).get("required"))
        order.courier_booking_status = "booked"
        await db.commit()
        logger.info(f"Courier shipment booked for order {order.order_number}: {shipment_id}")
        return None

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
            if not product or not product.is_active:
                unavailable.append(item.product_name)
                continue

            available_stock = product.stock_quantity if product.stock_quantity is not None else 0
            price = float(product.price)

            if order.branch_id:
                res = await db.execute(
                    select(BranchInventory).where(
                        BranchInventory.branch_id == order.branch_id,
                        BranchInventory.product_id == item.product_id,
                    )
                )
                branch_inv = res.scalars().first()
                if branch_inv is not None:
                    available_stock = branch_inv.stock_quantity
                    if branch_inv.branch_price is not None:
                        price = float(branch_inv.branch_price)
                    elif product.discount_price is not None:
                        price = float(product.discount_price)

            if available_stock <= 0:
                unavailable.append(item.product_name)
                continue

            quantity = min(item.quantity, available_stock)
            await CartService.add_item(
                user_id=str(user_id),
                product_id=str(item.product_id),
                quantity=quantity,
                price=price,
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

        # Return the discount to the campaign budget too. Money the branch
        # never actually gave away must not keep a campaign switched off.
        # Deliberately does not re-activate an auto-stopped coupon — see
        # CouponService.atomic_release_budget.
        await CouponService.atomic_release_budget(
            db, coupon.coupon_id, order.discount_amount
        )

        if released:
            logger.info(
                f"Coupon {coupon.code} redemption and "
                f"Rs {order.discount_amount or 0} of budget released by cancelled "
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
        enforce: bool = False,
        actor_role: Optional[str] = None,
    ) -> Order:
        """
        Update order status and log history.

        ``enforce=True`` (only the admin ``PATCH .../status`` endpoint passes
        it) runs the change through the fulfilment state machine in
        app/services/order_workflow.py and raises
        ``order_workflow.InvalidTransitionError`` (a ValueError) on a forbidden
        transition. Every system/courier caller leaves ``enforce=False`` — a
        courier reporting ``delivered`` right after ``handed_to_courier`` is
        authoritative, not a workflow violation.

        **READY_FOR_PICKUP books the parcel**, whoever asks for it (the state
        machine or a Super Admin override): the status means "a rider is coming
        for this", and saying so without a booking would leave a packed parcel
        on a shelf nobody is sent to. The booking comes first; if payment is not
        in or SribeesExpress refuses, this raises ``ValueError`` with the reason
        and the order keeps its current status.
        """
        old_status = order.status

        if enforce:
            from app.services import order_workflow

            order_workflow.validate_transition(old_status, status.value, actor_role)

        if status == OrderStatusEnum.READY_FOR_PICKUP and not order.courier_shipment_id:
            blocked = OrderService.payment_blocks_dispatch(order)
            if blocked is not None:
                raise ValueError(blocked)
            failure = await OrderService._book_courier_shipment(db, order)
            if failure is not None:
                raise ValueError(f"Could not book the courier pickup: {failure}")

        was_cancelled = order.status == OrderStatus.CANCELLED.value
        was_delivered = order.status == OrderStatus.DELIVERED.value

        order.status = status.value

        colombo_tz = ZoneInfo("Asia/Colombo")
        now = datetime.now(colombo_tz)
        if status == OrderStatusEnum.PACKED:
            order.packed_at = now
        elif status == OrderStatusEnum.READY_FOR_PICKUP:
            order.ready_for_pickup_at = now
        elif status == OrderStatusEnum.HANDED_TO_COURIER:
            order.handed_to_courier_at = now
        elif status == OrderStatusEnum.SHIPPED:
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
                # The same effects as the customer's own button, through the
                # same helper. This branch used to call
                # _reverse_order_financials alone, which returned the wallet
                # portion and quietly kept the card payment.
                await OrderService._apply_cancellation_effects(
                    db,
                    order,
                    reason=notes,
                    # Which admin is already recorded on the adjacent
                    # OrderStatusHistory row (`changed_by`), keyed by the same
                    # order. Repeating the identity here would give it two
                    # homes and one chance to disagree.
                    cancelled_by="admin",
                )

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
            await OrderService._cancel_courier_shipment(db, order)

        logger.info(f"Order {order.order_number} status updated to {status.value}")

        return order

    @staticmethod
    async def _cancel_courier_shipment(db: AsyncSession, order: Order) -> None:
        """
        Best-effort courier-side cancellation; never blocks an order cancellation.

        ``db`` is a parameter now, not a free name: this used to reference a
        ``db`` that was never in scope, so every cancellation of a booked order
        raised NameError after the cancellation had committed — the customer saw
        a 500, and the rider was still sent.
        """
        rider_has_it = OrderService._rider_already_has_it(order)
        try:
            async with courier_credentials.for_branch(db, order.branch_id):
                if rider_has_it:
                    # The parcel is on a motorbike. SribeesExpress refuses a
                    # cancel past pickup -- correctly, because an order marked
                    # cancelled while the goods are still moving would be read
                    # as fact by their rider app, their branch screens and
                    # their invoice. A request reaches the rider carrying it,
                    # who hands nothing over and brings it back.
                    await CourierService.request_cancellation(
                        order.courier_shipment_id,
                        reason="Customer cancelled the order",
                    )
                    logger.info(
                        f"Cancellation requested with the rider for order {order.order_number}"
                    )
                else:
                    await CourierService.cancel_shipment(order.courier_shipment_id)
                    logger.info(f"Courier shipment cancelled for order {order.order_number}")
        except (
            CourierUnavailableError,
            CourierError,
            courier_credentials.CourierNotConfiguredError,
        ) as exc:
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
    @staticmethod
    def gateway_paid_amount(order: Order) -> Decimal:
        """
        What the customer paid with a card, and is therefore owed back.

        The wallet portion is refunded separately by
        :meth:`_reverse_order_financials`, so this is deliberately the
        REMAINDER -- together they return the full order total, which is what
        a customer who cancelled before we booked a courier is owed. We have
        incurred nothing at that point.

        Zero unless the money actually arrived: a COD order is unpaid until
        the rider collects it, and an abandoned gateway order never reached
        PAID. Refunding either would credit a wallet from nothing.
        """
        if order.payment_status != PaymentStatus.PAID.value:
            return Decimal("0")
        if (order.payment_method or "").upper() == PaymentMethodEnum.COD.value:
            return Decimal("0")
        paid = (order.total_amount or Decimal("0")) - (order.wallet_deduction or Decimal("0"))
        return paid if paid > 0 else Decimal("0")

    # Cancelling has three states, not two, and the middle one is the point.
    CANCEL_FREE = "free"        # nothing booked yet -- everything paid comes back
    CANCEL_WITH_FEE = "fee"     # booked, not collected -- minus the delivery charge
    CANCEL_BLOCKED = "blocked"  # a rider has it, or there is nothing to cancel

    @staticmethod
    def customer_cancel_state(order: Order) -> str:
        """
        Whether the customer may still cancel, and on what terms.

        Three states:

        ``free`` -- no courier booked. We have spent nothing on this parcel,
        so everything paid comes back.

        ``fee`` -- booked with SribeesExpress but no rider has collected it.
        The booking is real and the delivery has been arranged, so the
        delivery charge does not come back. The button stays, in red, and the
        app has to say why before the customer confirms.

        ``fee`` also covers a parcel a rider is already carrying. The courier
        cannot simply mark it cancelled at that point -- the goods are in
        somebody's bag, halfway down a road -- so we raise a cancellation
        REQUEST with SribeesExpress, their rider sees it, hands nothing over
        and brings it back. The customer's side is settled either way: they
        paid for goods they are not receiving, so the goods money comes back
        and only the delivery charge is kept.

        ``blocked`` -- the parcel reached the customer, or there is nothing
        left to cancel. Past the doorstep the goods have changed hands and it
        is a return, which is different machinery with its own window, its own
        waybill and its own fee.

        The booking, not the status word, decides between free and fee: a
        status can be moved by hand, a waybill cannot.
        """
        terminal = (
            OrderStatus.DELIVERED.value,
            OrderStatus.DELIVERY_FAILED.value,
            OrderStatus.RTO_INITIATED.value,
            OrderStatus.RETURN_REQUESTED.value,
            OrderStatus.RETURN_APPROVED.value,
            OrderStatus.REFUNDED.value,
            OrderStatus.CANCELLED.value,
        )
        if order.status in terminal:
            return OrderService.CANCEL_BLOCKED

        in_transit = (
            OrderStatus.READY_FOR_PICKUP.value,
            OrderStatus.HANDED_TO_COURIER.value,
            OrderStatus.SHIPPED.value,
            OrderStatus.OUT_FOR_DELIVERY.value,
        )
        if order.courier_shipment_id or order.status in in_transit:
            return OrderService.CANCEL_WITH_FEE

        return OrderService.CANCEL_FREE

    @staticmethod
    def _rider_already_has_it(order: Order) -> bool:
        """Whether a rider is carrying the parcel, so cancelling it with the
        courier has to be a request rather than an instruction."""
        return order.status in (
            OrderStatus.HANDED_TO_COURIER.value,
            OrderStatus.SHIPPED.value,
            OrderStatus.OUT_FOR_DELIVERY.value,
        )

    @staticmethod
    def customer_can_cancel(order: Order) -> bool:
        """Whether the customer's own Cancel button should exist at all."""
        return OrderService.customer_cancel_state(order) != OrderService.CANCEL_BLOCKED

    @staticmethod
    def cancellation_courier_fee(order: Order) -> Decimal:
        """
        The delivery charge kept back when the parcel was already booked.

        ``shipping_amount`` -- what the CUSTOMER was billed for delivery --
        rather than ``courier_quote_amount``, which is what SribeesExpress
        charges us. The two are equal at checkout and differ only when a
        booking had to re-quote after the hold lapsed. Deducting the re-quote
        would take more from the customer than they ever agreed to pay for
        delivery, over a change in price they never saw.

        Zero before a booking exists: there is nothing to have spent.
        """
        if OrderService.customer_cancel_state(order) != OrderService.CANCEL_WITH_FEE:
            return Decimal("0")
        fee = order.shipping_amount or Decimal("0")
        return fee if fee > 0 else Decimal("0")

    @staticmethod
    def cancellation_refund_preview(order: Order) -> Decimal:
        """
        What would actually land in the wallet, so the confirmation can name
        the real figure instead of saying "your money".

        Everything paid, minus the delivery charge if one was incurred, and
        never below zero -- see :meth:`_apply_cancellation_effects` for why
        the fee is capped at what was paid rather than clawed out of an
        unrelated balance.
        """
        paid = OrderService.gateway_paid_amount(order) + (order.wallet_deduction or Decimal("0"))
        fee = min(OrderService.cancellation_courier_fee(order), paid)
        refund = paid - fee
        return refund if refund > 0 else Decimal("0")

    @staticmethod
    async def _record_cancellation(
        db: AsyncSession,
        order: Order,
        *,
        reason: Optional[str],
        refund_amount: Decimal,
        courier_fee: Decimal = Decimal("0"),
        cancelled_by: str = "customer",
        actor_admin_id=None,
        context: Optional[dict] = None,
    ) -> None:
        """
        Write the audit row, and decide whether it deserves a second look.

        Staged in the caller's transaction so a cancellation can never commit
        without its record. ``order_id`` is UNIQUE, so a double-submit raises
        rather than producing two rows -- and therefore cannot refund twice.

        Flagging never blocks anything. The customer's money moves either way;
        the flag only decides whether the row is waiting at the top of
        somebody's screen tomorrow morning.
        """
        from app.models.order import OrderCancellation

        ctx = context or {}
        placed_at = order.created_at
        cancelled_at = datetime.now(timezone.utc)

        flags: list[str] = []
        if cancelled_by == "customer":
            # Minutes, not hours. A person who has just placed an order and
            # changed their mind is the normal case and must not be flagged;
            # an order cancelled in seconds, repeatedly, is not a person.
            if placed_at is not None:
                placed = placed_at if placed_at.tzinfo else placed_at.replace(tzinfo=timezone.utc)
                if (cancelled_at - placed).total_seconds() < 120:
                    flags.append("cancelled within two minutes of ordering")

            # How often this customer has done it lately. The count is the
            # signal; one cancellation is a customer, ten in a month is a
            # pattern.
            recent = await db.execute(
                select(func.count())
                .select_from(OrderCancellation)
                .where(
                    OrderCancellation.user_id == order.user_id,
                    OrderCancellation.cancelled_by == "customer",
                    OrderCancellation.cancelled_at >= cancelled_at - timedelta(days=30),
                )
            )
            recent_count = recent.scalar() or 0
            if recent_count >= 3:
                flags.append(f"{recent_count + 1} cancellations in 30 days")

            if refund_amount > 0 and recent_count >= 1:
                flags.append("repeat cancellation with money refunded")

            # The expensive habit. Cancelling before we book anything costs a
            # picker's time; cancelling after costs a courier booking, every
            # time, and a customer doing it twice in a month is doing it on
            # purpose.
            if courier_fee > 0:
                booked_before = await db.execute(
                    select(func.count())
                    .select_from(OrderCancellation)
                    .where(
                        OrderCancellation.user_id == order.user_id,
                        OrderCancellation.cancelled_by == "customer",
                        OrderCancellation.courier_fee > 0,
                        OrderCancellation.cancelled_at >= cancelled_at - timedelta(days=30),
                    )
                )
                if (booked_before.scalar() or 0) >= 1:
                    flags.append("second cancellation after a courier booking in 30 days")

        db.add(
            OrderCancellation(
                order_id=order.order_id,
                user_id=order.user_id,
                cancelled_by=cancelled_by,
                actor_admin_id=actor_admin_id,
                reason=reason,
                ip_address=ctx.get("ip_address"),
                user_agent=ctx.get("user_agent"),
                device_id=ctx.get("device_id"),
                client_platform=ctx.get("client_platform"),
                order_placed_at=placed_at,
                cancelled_at=cancelled_at,
                refund_amount=refund_amount,
                courier_fee=courier_fee,
                refund_destination="wallet" if refund_amount > 0 else "none",
                flagged=bool(flags),
                flag_reason="; ".join(flags) or None,
            )
        )

    @staticmethod
    async def cancel_order(
        db: AsyncSession,
        order: Order,
        reason: Optional[str] = None,
        *,
        cancelled_by: str = "customer",
        actor_admin_id=None,
        context: Optional[dict] = None,
    ) -> Order:
        """Cancel an order, refund what was paid, and record who did it."""
        # Check if order can be cancelled. Once a rider has collected the parcel
        # (handed_to_courier onwards) a cancellation cannot recall it -- the
        # courier refuses a cancel past pickup -- so it is a support
        # conversation, not a button. Before this listed only shipped onwards,
        # a customer could "cancel" a parcel already in a rider's bag.
        non_cancellable = [
            OrderStatus.HANDED_TO_COURIER.value,
            OrderStatus.SHIPPED.value,
            OrderStatus.OUT_FOR_DELIVERY.value,
            OrderStatus.DELIVERED.value,
            OrderStatus.DELIVERY_FAILED.value,
            OrderStatus.RTO_INITIATED.value,
            OrderStatus.RETURN_REQUESTED.value,
            OrderStatus.RETURN_APPROVED.value,
            OrderStatus.REFUNDED.value,
            OrderStatus.CANCELLED.value,
        ]

        if order.status in non_cancellable:
            raise ValueError(f"Order cannot be cancelled in {order.status} status")

        order.status = OrderStatus.CANCELLED.value
        order.cancelled_at = datetime.utcnow()

        if reason:
            order.notes = f"{order.notes or ''}\nCancellation reason: {reason}".strip()

        await OrderService._apply_cancellation_effects(
            db,
            order,
            reason=reason,
            cancelled_by=cancelled_by,
            actor_admin_id=actor_admin_id,
            context=context,
        )

        await db.commit()
        await db.refresh(order)

        if order.courier_shipment_id:
            await OrderService._cancel_courier_shipment(db, order)

        logger.info(f"Order {order.order_number} cancelled")

        return order

    @staticmethod
    async def _apply_cancellation_effects(
        db: AsyncSession,
        order: Order,
        *,
        reason: Optional[str] = None,
        cancelled_by: str = "customer",
        actor_admin_id=None,
        context: Optional[dict] = None,
    ) -> None:
        """
        Everything a cancellation owes: the money back, and the record of it.

        One place, called by both cancellation paths -- the customer's button
        and an admin moving the status to CANCELLED. They used to diverge: the
        admin path reversed the wallet portion and kept the card payment, with
        nothing anywhere admitting it. Money must not depend on which screen
        somebody happened to be looking at.

        Staged in the CALLER'S transaction. Nothing here commits, so the
        refund, the clawback and the audit row land together or not at all.
        Must be called exactly once per cancellation -- both callers guard the
        transition INTO the cancelled state.
        """
        # Coupon slot, wallet refund, cashback clawback.
        await OrderService._reverse_order_financials(db, order)

        # The card portion. _reverse_order_financials returns what was paid
        # FROM the wallet; this returns what was paid TO the gateway, and
        # together they make the customer whole. Without this a card payment
        # was simply kept -- the order cancelled, the money gone, and nothing
        # in the system admitting it.
        #
        # Credited to the wallet rather than reversed at the gateway because
        # Marx IPG has no refund call: its whole API is create order, initiate
        # payment, read summary (see marx_ipg_service). A wallet credit is
        # immediate, is visible to the customer, and does not depend on a
        # capability the gateway does not have.
        gateway_refund = OrderService.gateway_paid_amount(order)
        if gateway_refund > 0:
            await WalletService.apply_transaction(
                db,
                user_id=order.user_id,
                tx_type=WalletTransactionType.REFUND,
                amount=gateway_refund,
                title="Order cancellation refund",
                order_id=order.order_id,
                order_number=order.order_number,
            )
            # The money came back, so the order is no longer a paid order.
            order.payment_status = PaymentStatus.REFUNDED.value
            order.refund_amount = (order.refund_amount or Decimal("0")) + gateway_refund
            order.refunded_at = datetime.utcnow()

        # The delivery charge, when the parcel had already been booked.
        #
        # Taken as a SEPARATE wallet debit rather than by shrinking the refunds
        # above. Two reasons. The ledger then reads as what actually happened
        # -- everything came back, then the delivery was charged -- instead of
        # a smaller refund nobody can reconcile against the order. And it works
        # for every payment mix: an order paid entirely from the wallet has no
        # gateway refund to shrink, and shrinking only the gateway portion
        # would have let that customer cancel a booked parcel for free.
        #
        # Capped at what was actually paid. A COD order has paid us nothing, so
        # there is nothing to deduct from -- and taking the courier's fee out
        # of a wallet balance the customer earned elsewhere would be charging
        # them for money they never gave us.
        paid_total = gateway_refund + (order.wallet_deduction or Decimal("0"))
        fee = min(OrderService.cancellation_courier_fee(order), paid_total)
        if fee > 0:
            await WalletService.apply_transaction(
                db,
                user_id=order.user_id,
                tx_type=WalletTransactionType.SPENT,
                amount=fee,
                title="Delivery charge on cancelled order",
                order_id=order.order_id,
                order_number=order.order_number,
            )

        await OrderService._record_cancellation(
            db,
            order,
            reason=reason,
            refund_amount=paid_total - fee,
            courier_fee=fee,
            cancelled_by=cancelled_by,
            actor_admin_id=actor_admin_id,
            context=context,
        )

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
    def return_window_expires_at(order: Order) -> Optional[datetime]:
        """When this order stops being returnable, or None if it never started.

        Exposed on the order payload so the app can render a live countdown
        instead of re-deriving the rule — a window the customer discovers by
        being rejected is a support ticket, and a window two codebases each
        compute their own way is a support ticket that nobody can reproduce.
        """
        delivered_at = order.delivered_at
        if delivered_at is None:
            return None
        if delivered_at.tzinfo is None:
            delivered_at = delivered_at.replace(tzinfo=timezone.utc)
        return delivered_at + RETURN_WINDOW

    @staticmethod
    async def request_return(
        db: AsyncSession,
        order: Order,
        reason: str,
        comments: Optional[str] = None,
        items: Optional[list] = None,
        images: Optional[list] = None,
    ) -> Order:
        """
        Customer request to return a DELIVERED order. Moves it to
        RETURN_REQUESTED and records the reason/comments/items.

        Only inside the 12-hour window, measured from ``delivered_at`` — the
        moment the goods actually changed hands — and not from ``updated_at``,
        which any later write (a COD remittance, a support note) would push
        forward and silently reopen a closed window.

        An order with no ``delivered_at`` on file is refused rather than
        waved through: without it there is no window to be inside, and
        defaulting to "allowed" would make every legacy order returnable
        forever.
        """
        if order.status != OrderStatus.DELIVERED.value:
            raise ValueError("Only delivered orders can be returned.")

        expires_at = OrderService.return_window_expires_at(order)
        if expires_at is None:
            raise ValueError(
                "This order has no recorded delivery time, so a return cannot be "
                "started online. Please contact support."
            )
        if datetime.now(timezone.utc) > expires_at:
            raise ValueError(
                "The 12-hour return window for this order has closed. "
                "Please contact support if you still need help."
            )

        order.status = OrderStatus.RETURN_REQUESTED.value
        order.return_reason = reason
        order.return_comments = comments
        order.return_items = (
            [{"order_item_id": str(i.order_item_id), "quantity": i.quantity} for i in items]
            if items
            else None
        )
        # Relative paths, already checked by the router to belong to this
        # order. Stored in the order the customer added them, because the
        # first photo is usually the one they framed as the problem.
        order.return_images = list(images) if images else None
        order.return_requested_at = datetime.utcnow()

        await db.commit()
        await db.refresh(order)
        logger.info(f"Return requested for order {order.order_number}: {reason}")
        return order

    @staticmethod
    async def approve_return(
        db: AsyncSession,
        order: Order,
        resolution: str = "returnless_refund",
        note: Optional[str] = None,
    ) -> Order:
        """
        Approve a pending return. What happens next depends on whether goods
        are physically coming back:

        - ``returnless_refund`` — spoilage, a broken item not worth freighting.
          Nothing is collected, so there is nothing to wait for: the wallet is
          credited here and the order goes straight to REFUNDED.
        - ``reverse_pickup`` — a rider collects from the customer and brings
          the goods to our branch. The order goes to RETURN_APPROVED and
          **the wallet is not credited yet**. It is credited by
          :meth:`complete_return` when the goods are actually back
          (RETURNS_ARCHITECTURE.md §7). Until this existed, approval refunded
          immediately and nothing ever checked that anything came back.

        ``resolution`` and ``note`` were already in the request schema and on
        the table, and the router has been passing them since B4 — against a
        signature that did not accept them, which raised TypeError on every
        call. Approving a return has therefore been broken in production;
        this is the fix, covered by a regression test.
        """
        if order.status != OrderStatus.RETURN_REQUESTED.value:
            raise ValueError("This order has no pending return request.")
        if resolution not in ("returnless_refund", "reverse_pickup"):
            raise ValueError(f"Unknown return resolution '{resolution}'.")

        order.return_resolution = resolution
        if note:
            order.return_resolution_note = note
        order.return_approved_at = datetime.now(timezone.utc)
        # Computed now, at the values the claim was approved on, rather than
        # when the goods land — so a later catalogue price change cannot move
        # what the customer was promised.
        order.refund_amount = OrderService._compute_return_refund(order)

        if resolution == "reverse_pickup":
            order.status = OrderStatus.RETURN_APPROVED.value
            await db.commit()
            await db.refresh(order)
            # Best-effort: a courier outage must not un-approve a claim a
            # manager has already made. The booking is retryable from the
            # admin panel, and `return_booking_status` says it needs retrying.
            await OrderService._book_reverse_pickup(db, order)
            await OrderService._notify_return_status(db, order, OrderStatus.RETURN_APPROVED.value)
            return order

        await OrderService._credit_return_refund(db, order)
        await db.commit()
        await db.refresh(order)
        logger.info(
            f"Return approved ({resolution}) for order {order.order_number}: "
            f"refunded {order.refund_amount}"
        )
        await OrderService._notify_return_status(db, order, OrderStatus.REFUNDED.value)
        return order

    @staticmethod
    async def _credit_return_refund(db: AsyncSession, order: Order) -> None:
        """Put the refund in the customer's wallet and close the order.

        Guarded by ``refunded_at`` rather than by the status alone: the goods
        arriving back is reported by a courier webhook, and webhooks are
        redelivered. Without this, a replayed `delivered` on the return leg
        would credit the wallet a second time.
        """
        if order.refunded_at is not None:
            logger.info(f"Return refund for {order.order_number} already issued, skipping")
            return

        refund = order.refund_amount
        if refund is None:
            refund = OrderService._compute_return_refund(order)
            order.refund_amount = refund

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

        order.refunded_at = datetime.now(timezone.utc)
        order.status = OrderStatus.REFUNDED.value

    @staticmethod
    async def _notify_return_status(db: AsyncSession, order: Order, status_value: str) -> None:
        """Best-effort customer notification — never blocks a refund."""
        try:
            await NotificationService.notify_order_status(
                db, order.user_id, order.order_number, status_value
            )
        except Exception as exc:  # pragma: no cover - notification is best-effort
            logger.warning(f"Return notification failed for {order.order_number}: {exc}")

    @staticmethod
    async def _book_reverse_pickup(db: AsyncSession, order: Order) -> None:
        """Book the return leg with SribeesExpress and keep what it hands back.

        The handover code is the part that matters: it comes back exactly once
        and they store only a hash, so if this write is lost nobody can recover
        it and the branch has to ask for a new one. Persisted before anything
        else can fail.

        Never raises. A courier outage leaves ``return_booking_status =
        'failed'`` with the reason in ``return_booking_error``, which the admin
        panel surfaces and can retry — an approved claim that could not be
        dispatched is an ops problem, not a reason to refuse the manager's
        decision after the fact.
        """
        from app.services.courier_service import CourierError, CourierService

        items = None
        if order.return_items:
            by_id = {str(i.order_item_id): i for i in (order.items or [])}
            items = []
            for line in order.return_items:
                item = by_id.get(str(line.get("order_item_id")))
                if item is None:
                    continue
                items.append(
                    {
                        "sku": item.product_sku,
                        "name": item.product_name,
                        "quantity": int(line.get("quantity", 1)),
                    }
                )

        # Who the rider collects from. Omitted, SribeesExpress reuses the
        # address the parcel was delivered to — which is the right default, so
        # these are only sent when our own record is actually better.
        address = order.delivery_address
        try:
            # Act as the fulfilling branch's SribeesExpress account, the same
            # way the forward booking does. Every call to them needs a scope —
            # a branch with its own key is billed on it, and one on the shared
            # account is not — and without it `CourierService` has no
            # credentials to send and refuses before a request is made. That is
            # exactly what a Branch Manager was hitting: "No SribeesExpress
            # credentials are in scope for this call."
            #
            # `for_branch`, not `optional_for_branch`: this books a parcel, and
            # a booking that silently did not happen is a rider nobody sends.
            async with courier_credentials.for_branch(db, order.branch_id):
                result = await CourierService.create_return(
                    merchant_order_ref=order.order_number,
                    reason=order.return_reason or "Customer return",
                    items=items or None,
                    pickup_contact_name=await resolve_recipient_name(db, order, address),
                    pickup_contact_phone=getattr(address, "phone", None),
                )
        except courier_credentials.CourierNotConfiguredError as exc:
            # Nobody has entered a key for this branch yet. A configuration gap,
            # not an outage — say so, because "try again" will not fix it.
            order.return_booking_status = "failed"
            order.return_booking_error = (
                "This branch has no SribeesExpress account configured yet. "
                "Add its API key under Settings → Courier, then retry."
            )[:500]
            await db.commit()
            logger.error(f"Reverse pickup unconfigured for {order.order_number}: {exc}")
            return
        except CourierError as exc:
            order.return_booking_status = "failed"
            order.return_booking_error = f"{exc.code}: {exc.message}"[:500]
            await db.commit()
            logger.error(f"Reverse pickup booking failed for {order.order_number}: {exc.message}")
            return
        except Exception as exc:  # pragma: no cover - network/unexpected
            order.return_booking_status = "failed"
            order.return_booking_error = str(exc)[:500]
            await db.commit()
            logger.error(f"Reverse pickup booking errored for {order.order_number}: {exc}")
            return

        order.return_waybill_number = result.get("return_id")
        # Shown once, by this call only. See the column's comment in models.
        if result.get("handover_code"):
            order.return_handover_code = result["handover_code"]
        if result.get("return_fee_lkr") is not None:
            order.return_fee = Decimal(str(result["return_fee_lkr"]))
        order.return_tracking_status = result.get("status")
        order.return_booking_status = "booked"
        order.return_booking_error = None
        await db.commit()
        await db.refresh(order)
        logger.info(
            f"Reverse pickup booked for {order.order_number}: "
            f"{order.return_waybill_number} (fee {order.return_fee})"
        )

    @staticmethod
    async def complete_return(db: AsyncSession, order: Order) -> Order:
        """The goods are back with us — credit the refund and close the order.

        Called from the courier sync when the *return leg* reports
        ``delivered``, which on a reverse pickup means the rider handed the
        goods to our counter and our staff gave them the code. That is the
        event the refund waits for, per the business decision in
        RETURNS_ARCHITECTURE.md §7.

        Idempotent — see :meth:`_credit_return_refund`.
        """
        if order.status not in (
            OrderStatus.RETURN_APPROVED.value,
            OrderStatus.RETURN_REQUESTED.value,
        ):
            logger.info(
                f"Return completion for {order.order_number} ignored — order is "
                f"'{order.status}', not an open return."
            )
            return order

        order.return_received_at = datetime.now(timezone.utc)
        await OrderService._credit_return_refund(db, order)
        await db.commit()
        await db.refresh(order)
        logger.info(
            f"Return received for {order.order_number}: refunded {order.refund_amount}"
        )
        await OrderService._notify_return_status(db, order, OrderStatus.REFUNDED.value)
        return order

    @staticmethod
    async def reject_return(db: AsyncSession, order: Order, note: Optional[str] = None) -> Order:
        """Reject a pending return: revert the order back to DELIVERED.

        ``note`` is the manager's reason, kept on the claim so a customer who
        rings support gets the same answer twice. Same signature fix as
        :meth:`approve_return` — the router has been passing it all along.
        """
        if order.status != OrderStatus.RETURN_REQUESTED.value:
            raise ValueError("This order has no pending return request.")

        order.status = OrderStatus.DELIVERED.value
        if note:
            order.return_resolution_note = note
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
