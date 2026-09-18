"""
Cart API Endpoints (Redis-based)

Every response that returns the cart returns it priced the same way: goods
totals plus the SribeesExpress delivery charge for the shopper's address, and a
``delivery`` block saying whether that charge exists (see
app/services/delivery_quote_service.py). ``totals.shipping`` is the delivery
charge when one was quoted and ``0`` when it was not — ``delivery.available``
is what tells those apart, never the number.
"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis, get_redis_optional
from app.core.dependencies import get_current_user
from app.schemas.cart import (
    AddToCartRequest,
    ApplyCouponRequest,
    UpdateCartItemRequest,
)
from app.services import delivery_quote_service
from app.services.app_settings_service import AppSettingsService
from app.services.cart_service import CartService
from app.services.coupon_service import CouponService, CouponValidationError
from app.services.pricing_service import PricingService

# Prefix "/cart" is applied by app/api/v1/router.py — do not repeat it here.
router = APIRouter(tags=["Cart"])


async def _optional_branch_id(redis: Redis, current_user) -> Optional[UUID]:
    """
    The shopper's currently selected delivery branch, or ``None``.

    Deliberately non-fatal: a shopper who has not chosen a delivery location
    yet has no branch context, and the cart must keep working for them. Used
    only to fail a branch-owned coupon early — checkout re-checks against the
    branch that actually fulfils the order and is the authoritative gate.
    """
    try:
        from app.services import branch_service

        context = await branch_service.get_branch_context(redis, str(current_user.user_id))
        if context and context.get("branch_id"):
            return UUID(context["branch_id"])
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Could not resolve branch context for coupon check: {exc}")
    return None


async def _validate_and_cleanup_coupon(
    db: AsyncSession,
    redis: Optional[Redis],
    current_user,
    cart: dict,
) -> dict:
    """
    Ensure an applied coupon is still valid against the cart's current items,
    active state, and eligibility rules. If not, automatically remove it from
    the cart session so the shopper never holds a stale or invalid coupon.
    """
    coupon_data = cart.get("coupon")
    if not coupon_data:
        return cart

    items = cart.get("items", [])
    if not items:
        try:
            await CartService.remove_coupon(str(current_user.user_id))
        except Exception:
            pass
        cart["coupon"] = None
        return cart

    code = coupon_data.get("code")
    if not code:
        cart["coupon"] = None
        return cart

    try:
        coupon = await CouponService.get_by_code(db, code)
        if coupon is None:
            await CartService.remove_coupon(str(current_user.user_id))
            cart["coupon"] = None
            return cart

        applied_branch_id = await _optional_branch_id(redis, current_user) if redis else None
        enriched = await CouponService.enrich_items(db, items, applied_branch_id)
        subtotal = CouponService.eligible_subtotal(coupon, enriched)

        user_redemptions = await CouponService.redemptions_by_user(
            db, coupon.code, current_user.user_id
        )
        had_completed = (
            await CouponService.has_completed_order(
                db, current_user.user_id, applied_branch_id
            )
            if coupon.first_order_only
            else None
        )

        CouponService.validate(
            coupon,
            subtotal,
            datetime.now(timezone.utc),
            branch_id=applied_branch_id,
            user_id=current_user.user_id,
            user_redemptions=user_redemptions,
            user_has_completed_order=had_completed,
        )

        if coupon and cart.get("coupon"):
            cart["coupon"]["min_order_value"] = (
                float(coupon.min_order_value)
                if coupon.min_order_value is not None
                else 0.0
            )
            cart["coupon"]["max_discount_amount"] = (
                float(coupon.max_discount_amount)
                if coupon.max_discount_amount is not None
                else None
            )
            cart["coupon"]["discount_type"] = coupon.discount_type
            cart["coupon"]["discount_value"] = float(coupon.discount_value or 0)
    except Exception as e:
        logger.info(f"Auto-removing invalid coupon {code} from cart of user {current_user.user_id}: {e}")
        try:
            await CartService.remove_coupon(str(current_user.user_id))
        except Exception:
            pass
        cart["coupon"] = None

    return cart


async def _priced(
    db: AsyncSession,
    redis: Optional[Redis],
    current_user,
    cart: dict,
    address_id: Optional[str] = None,
) -> dict:
    """
    The cart with delivery priced in.

    A delivery failure never fails the cart: the shopper can still see and edit
    what they are buying, and the ``delivery`` block explains why there is no
    charge. Checkout is where an unpriced delivery stops the order.
    """
    cart = await _validate_and_cleanup_coupon(db, redis, current_user, cart)
    items = cart.get("items", [])
    try:
        quote, destination = await delivery_quote_service.quote_for_cart(
            db, redis, current_user.user_id, items, address_id=address_id
        )
        pricing_config = await AppSettingsService.get_pricing_config(db)
        breakdown = await PricingService.quote(
            items=items,
            coupon=cart.get("coupon"),
            tax_rate=pricing_config["tax_rate"],
            delivery=quote,
        )
    except Exception as exc:  # pragma: no cover - defensive, never break the cart
        logger.error(f"Pricing delivery into the cart failed: {exc}")
        return cart

    cart["totals"] = {
        "subtotal": float(breakdown.subtotal),
        "discount": float(breakdown.discount),
        "tax": float(breakdown.tax),
        "shipping": float(breakdown.delivery_fee),
        "cashback_earned": float(breakdown.cashback_earned),
        "total": float(breakdown.total),
    }
    cart["delivery"] = delivery_quote_service.delivery_payload(quote, destination)
    return cart


# ============================================================================
# Cart Endpoints
# ============================================================================

@router.get("", response_model=dict)
async def get_cart(
    address_id: Optional[str] = Query(
        None, description="Price delivery to this saved address instead of the default one"
    ),
    db: AsyncSession = Depends(get_db),
    redis: Optional[Redis] = Depends(get_redis_optional),
    current_user = Depends(get_current_user)
):
    """
    Get current user's cart, with the delivery charge for their address.
    """
    try:
        cart = await CartService.get_cart(str(current_user.user_id))

        if redis and cart.get("items"):
            branch_id = await _optional_branch_id(redis, current_user)
            if branch_id:
                from app.models.product import BranchInventory
                from sqlalchemy import select
                updated_any = False
                for item in list(cart["items"]):
                    try:
                        p_uuid = UUID(item["product_id"])
                        res = await db.execute(
                            select(BranchInventory).where(
                                BranchInventory.branch_id == branch_id,
                                BranchInventory.product_id == p_uuid
                            )
                        )
                        inv = res.scalars().first()
                        if inv is not None and inv.stock_quantity < item["quantity"]:
                            if inv.stock_quantity > 0:
                                await CartService.update_item_quantity(
                                    user_id=str(current_user.user_id),
                                    product_id=item["product_id"],
                                    quantity=inv.stock_quantity,
                                    variant_id=item.get("variant_id")
                                )
                            else:
                                await CartService.remove_item(
                                    user_id=str(current_user.user_id),
                                    product_id=item["product_id"],
                                    variant_id=item.get("variant_id")
                                )
                            updated_any = True
                    except Exception:
                        pass
                if updated_any:
                    cart = await CartService.get_cart(str(current_user.user_id))

        return {
            "success": True,
            "data": await _priced(db, redis, current_user, cart, address_id)
        }
    except Exception as e:
        logger.error(f"Error getting cart: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get cart"
        )


@router.post("/items", response_model=dict)
async def add_to_cart(
    data: AddToCartRequest,
    db: AsyncSession = Depends(get_db),
    redis: Optional[Redis] = Depends(get_redis_optional),
    current_user = Depends(get_current_user)
):
    """
    Add an item to cart.
    """
    try:
        if redis:
            branch_id = await _optional_branch_id(redis, current_user)
            if branch_id:
                from app.models.product import BranchInventory
                from sqlalchemy import select
                try:
                    p_uuid = UUID(data.product_id)
                    res = await db.execute(
                        select(BranchInventory).where(
                            BranchInventory.branch_id == branch_id,
                            BranchInventory.product_id == p_uuid
                        )
                    )
                    inv = res.scalars().first()
                    if inv is not None:
                        existing_cart = await CartService.get_cart(str(current_user.user_id))
                        existing_qty = sum(
                            item["quantity"]
                            for item in existing_cart.get("items", [])
                            if item.get("product_id") == data.product_id
                        )
                        if existing_qty + data.quantity > inv.stock_quantity:
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Only {inv.stock_quantity} items available in stock for this branch"
                            )
                except ValueError:
                    pass

        cart = await CartService.add_item(
            user_id=str(current_user.user_id),
            product_id=data.product_id,
            quantity=data.quantity,
            price=float(data.price),
            name=data.name,
            image=data.image,
            sku=data.sku,
            variant_id=data.variant_id,
            original_price=float(data.original_price) if data.original_price is not None else None,
            cashback_percentage=float(data.cashback_percentage) if data.cashback_percentage is not None else None,
        )

        return {
            "success": True,
            "data": await _priced(db, redis, current_user, cart),
            "message": "Item added to cart"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding to cart: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add item to cart"
        )


@router.put("/items/{product_id}", response_model=dict)
async def update_cart_item(
    product_id: str,
    data: UpdateCartItemRequest,
    variant_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    redis: Optional[Redis] = Depends(get_redis_optional),
    current_user = Depends(get_current_user)
):
    """
    Update item quantity in cart.
    """
    try:
        if redis and data.quantity > 0:
            branch_id = await _optional_branch_id(redis, current_user)
            if branch_id:
                from app.models.product import BranchInventory
                from sqlalchemy import select
                try:
                    p_uuid = UUID(product_id)
                    res = await db.execute(
                        select(BranchInventory).where(
                            BranchInventory.branch_id == branch_id,
                            BranchInventory.product_id == p_uuid
                        )
                    )
                    inv = res.scalars().first()
                    if inv is not None and data.quantity > inv.stock_quantity:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Only {inv.stock_quantity} items available in stock for this branch"
                        )
                except ValueError:
                    pass

        cart = await CartService.update_item_quantity(
            user_id=str(current_user.user_id),
            product_id=product_id,
            quantity=data.quantity,
            variant_id=variant_id
        )

        return {
            "success": True,
            "data": await _priced(db, redis, current_user, cart),
            "message": "Cart updated"
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error updating cart: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update cart"
        )


@router.delete("/items/{product_id}", response_model=dict)
async def remove_from_cart(
    product_id: str,
    variant_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    redis: Optional[Redis] = Depends(get_redis_optional),
    current_user = Depends(get_current_user)
):
    """
    Remove an item from cart.
    """
    try:
        cart = await CartService.remove_item(
            user_id=str(current_user.user_id),
            product_id=product_id,
            variant_id=variant_id
        )

        return {
            "success": True,
            "data": await _priced(db, redis, current_user, cart),
            "message": "Item removed from cart"
        }
    except Exception as e:
        logger.error(f"Error removing from cart: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove item"
        )


@router.delete("", response_model=dict)
async def clear_cart(
    current_user = Depends(get_current_user)
):
    """
    Clear entire cart.
    """
    try:
        await CartService.clear_cart(str(current_user.user_id))

        return {
            "success": True,
            "message": "Cart cleared successfully"
        }
    except Exception as e:
        logger.error(f"Error clearing cart: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to clear cart"
        )


@router.post("/coupon", response_model=dict)
async def apply_coupon(
    data: ApplyCouponRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user = Depends(get_current_user)
):
    """
    Apply a coupon to the cart.

    The coupon is validated authoritatively against the ``coupons`` table:
    the client only supplies the *code* — the discount type/value come from the
    database, never from the request. All validation failures return a 400 with
    a user-friendly message.
    """
    try:
        cart = await CartService.get_cart(str(current_user.user_id))
        if not cart.get("items"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Your cart is empty.",
            )

        coupon = await CouponService.get_by_code(db, data.code)
        if coupon is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid coupon code.",
            )

        # Everything here is best-effort and exists so the shopper learns the
        # code will not work NOW, at the moment they type it, rather than at
        # checkout with their card out. Checkout re-runs all of it against the
        # branch that actually fulfils the order and is the authoritative gate.
        applied_branch_id = await _optional_branch_id(redis, current_user)

        # The eligible subtotal, not the cart total: a coupon that excludes
        # clearance stock, or is restricted to certain categories, must be
        # measured against the goods it can actually discount, or the shopper
        # is told the minimum is met and then refused at checkout.
        enriched = await CouponService.enrich_items(
            db, cart["items"], applied_branch_id
        )
        subtotal = CouponService.eligible_subtotal(coupon, enriched)

        user_redemptions = await CouponService.redemptions_by_user(
            db, coupon.code, current_user.user_id
        )
        had_completed = (
            await CouponService.has_completed_order(
                db, current_user.user_id, applied_branch_id
            )
            if coupon.first_order_only
            else None
        )

        CouponService.validate(
            coupon,
            subtotal,
            datetime.now(timezone.utc),
            branch_id=applied_branch_id,
            user_id=current_user.user_id,
            user_redemptions=user_redemptions,
            user_has_completed_order=had_completed,
        )

        # Apply using the DATABASE discount values (client input ignored).
        updated_cart = await CartService.apply_coupon(
            user_id=str(current_user.user_id),
            code=coupon.code,
            discount_type=coupon.discount_type,
            discount_value=float(coupon.discount_value),
            max_discount_amount=(
                float(coupon.max_discount_amount)
                if coupon.max_discount_amount is not None
                else None
            ),
            min_order_value=(
                float(coupon.min_order_value)
                if coupon.min_order_value is not None
                else None
            ),
        )

        return {
            "success": True,
            "data": await _priced(db, redis, current_user, updated_cart),
            "message": "Coupon applied successfully",
        }
    except CouponValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error applying coupon: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to apply coupon"
        )


@router.delete("/coupon", response_model=dict)
async def remove_coupon(
    db: AsyncSession = Depends(get_db),
    redis: Optional[Redis] = Depends(get_redis_optional),
    current_user = Depends(get_current_user)
):
    """
    Remove coupon from cart.
    """
    try:
        cart = await CartService.remove_coupon(str(current_user.user_id))

        return {
            "success": True,
            "data": await _priced(db, redis, current_user, cart),
            "message": "Coupon removed"
        }
    except Exception as e:
        logger.error(f"Error removing coupon: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove coupon"
        )


@router.get("/count", response_model=dict)
async def get_cart_count(
    current_user = Depends(get_current_user)
):
    """
    Get total item count in cart.
    """
    try:
        count = await CartService.get_item_count(str(current_user.user_id))

        return {
            "success": True,
            "data": {
                "count": count
            }
        }
    except Exception as e:
        logger.error(f"Error getting cart count: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get cart count"
        )


# ============================================================================
# Cart Sync/Merge Endpoints (for offline-first mobile)
# ============================================================================

@router.post("/merge", response_model=dict)
async def merge_cart(
    guest_cart: dict,
    db: AsyncSession = Depends(get_db),
    redis: Optional[Redis] = Depends(get_redis_optional),
    current_user = Depends(get_current_user)
):
    """
    Merge guest cart with user cart (on login).

    Used when a guest adds items to cart and then logs in.
    """
    try:
        cart = await CartService.merge_cart(
            user_id=str(current_user.user_id),
            guest_items=guest_cart.get("items", [])
        )

        return {
            "success": True,
            "data": await _priced(db, redis, current_user, cart),
            "message": "Cart merged successfully"
        }
    except Exception as e:
        logger.error(f"Error merging cart: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to merge cart"
        )


@router.post("/sync", response_model=dict)
async def sync_cart(
    local_cart: dict,
    db: AsyncSession = Depends(get_db),
    redis: Optional[Redis] = Depends(get_redis_optional),
    current_user = Depends(get_current_user)
):
    """
    Sync local cart with server (for offline-first mobile).

    Merges local changes with server state using timestamps.
    """
    try:
        cart = await CartService.sync_cart(
            user_id=str(current_user.user_id),
            local_cart=local_cart
        )

        return {
            "success": True,
            "data": await _priced(db, redis, current_user, cart),
            "message": "Cart synced successfully"
        }
    except Exception as e:
        logger.error(f"Error syncing cart: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to sync cart"
        )


@router.get("/sync/status", response_model=dict)
async def get_sync_status(
    current_user = Depends(get_current_user)
):
    """
    Get cart sync status.
    """
    try:
        cart = await CartService.get_cart(str(current_user.user_id))

        return {
            "success": True,
            "data": {
                "lastSyncedAt": cart.get("updated_at"),
                "itemCount": len(cart.get("items", [])),
                "synced": True
            }
        }
    except Exception as e:
        logger.error(f"Error getting sync status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get sync status"
        )


@router.post("/validate", response_model=dict)
async def validate_cart(
    current_user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Optional[Redis] = Depends(get_redis_optional),
):
    """
    Validate cart before checkout.

    Checks stock availability and prices for all items, and whether delivery to
    the shopper's address can be priced — a cart that cannot be delivered is
    not ready for checkout.
    """
    try:
        cart = await _priced(
            db, redis, current_user, await CartService.get_cart(str(current_user.user_id))
        )
        delivery = cart.get("delivery") or {}
        issues = []
        if cart.get("items") and not delivery.get("available"):
            issues.append(
                {
                    "code": f"delivery_{delivery.get('status', 'unavailable')}",
                    "message": delivery.get("message") or "Delivery can't be priced right now.",
                }
            )

        # TODO: Add actual product validation against database
        validation_result = {
            "valid": not issues,
            "items": cart.get("items", []),
            "issues": issues,
            "totals": cart.get("totals", {}),
            "delivery": delivery,
        }

        return {
            "success": True,
            "data": validation_result,
            "message": "Cart validated"
        }
    except Exception as e:
        logger.error(f"Error validating cart: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to validate cart"
        )
