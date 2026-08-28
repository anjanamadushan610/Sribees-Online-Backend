"""
Cart Service - Redis-based Cart Management
"""
import json
import time
from typing import Any, Dict, Optional

from app.config.redis import RedisKeys, get_redis_client
from app.services.pricing_service import PricingService

# Constants
CART_TTL = 30 * 24 * 60 * 60  # 30 days in seconds


def _get_redis():
    """Get Redis client, raising error if not available."""
    client = get_redis_client()
    if client is None:
        raise RuntimeError("Redis not initialized")
    return client


class CartService:
    """Redis-based cart service."""

    @staticmethod
    def _get_cart_key(user_id: str) -> str:
        """Get Redis key for user's cart."""
        return RedisKeys.cart(user_id)

    @staticmethod
    async def _calculate_totals(items: list, coupon: Optional[Dict] = None) -> Dict[str, float]:
        """
        Calculate cart totals.

        Delegates to the shared ``PricingService`` (the single source of truth)
        so the totals returned by GET /cart match the checkout quote and the
        final order exactly. Wallet deduction is a checkout-only concept and is
        never applied here. No delivery address is known yet at the cart stage,
        so this always prices delivery at the flat fallback fee (no
        origin/destination to quote the courier with) — the courier-quoted fee
        appears once checkout resolves a delivery address (POST /orders/quote).
        """
        breakdown = await PricingService.quote(items=items, coupon=coupon, use_wallet=False)
        return {
            "subtotal": float(breakdown.subtotal),
            "discount": float(breakdown.discount),
            "tax": float(breakdown.tax),
            # Cart uses the legacy "shipping" key; value is the authoritative
            # delivery fee from PricingService.
            "shipping": float(breakdown.delivery_fee),
            "total": float(breakdown.total),
        }

    @staticmethod
    async def get_cart(user_id: str) -> Dict[str, Any]:
        """Get user's cart."""
        cart_key = CartService._get_cart_key(user_id)
        redis = _get_redis()
        cart_data = await redis.get(cart_key)

        if not cart_data:
            # Return empty cart
            return {
                "items": [],
                "totals": {
                    "subtotal": 0,
                    "discount": 0,
                    "tax": 0,
                    "shipping": 0,
                    "total": 0
                },
                "coupon": None,
                "updated_at": int(time.time())
            }

        cart = json.loads(cart_data)
        # Recalculate totals
        cart["totals"] = await CartService._calculate_totals(
            cart.get("items", []),
            cart.get("coupon")
        )
        return cart

    @staticmethod
    async def _save_cart(user_id: str, cart: Dict[str, Any]) -> None:
        """Save cart to Redis."""
        cart_key = CartService._get_cart_key(user_id)
        cart["updated_at"] = int(time.time())
        redis = _get_redis()
        await redis.setex(cart_key, CART_TTL, json.dumps(cart))

    @staticmethod
    async def add_item(
        user_id: str,
        product_id: str,
        quantity: int,
        price: float,
        name: str,
        image: Optional[str] = None,
        sku: Optional[str] = None,
        variant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Add item to cart."""
        cart = await CartService.get_cart(user_id)

        # Check if item already exists
        existing_index = None
        for i, item in enumerate(cart["items"]):
            item_variant = item.get("variant_id")
            if item["product_id"] == product_id and item_variant == variant_id:
                existing_index = i
                break

        if existing_index is not None:
            # Update quantity
            cart["items"][existing_index]["quantity"] += quantity
        else:
            # Add new item
            cart["items"].append({
                "product_id": product_id,
                "quantity": quantity,
                "price": price,
                "name": name,
                "image": image,
                "sku": sku,
                "variant_id": variant_id
            })

        # Recalculate totals
        cart["totals"] = await CartService._calculate_totals(
            cart["items"],
            cart.get("coupon")
        )

        await CartService._save_cart(user_id, cart)
        return cart

    @staticmethod
    async def update_item_quantity(
        user_id: str,
        product_id: str,
        quantity: int,
        variant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update item quantity in cart."""
        cart = await CartService.get_cart(user_id)

        # Find item
        item_index = None
        for i, item in enumerate(cart["items"]):
            item_variant = item.get("variant_id")
            if item["product_id"] == product_id and item_variant == variant_id:
                item_index = i
                break

        if item_index is None:
            raise ValueError("Item not found in cart")

        if quantity <= 0:
            # Remove item
            cart["items"].pop(item_index)
        else:
            # Update quantity
            cart["items"][item_index]["quantity"] = quantity

        # Recalculate totals
        cart["totals"] = await CartService._calculate_totals(
            cart["items"],
            cart.get("coupon")
        )

        await CartService._save_cart(user_id, cart)
        return cart

    @staticmethod
    async def remove_item(
        user_id: str,
        product_id: str,
        variant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Remove item from cart."""
        cart = await CartService.get_cart(user_id)

        # Filter out the item
        cart["items"] = [
            item for item in cart["items"]
            if not (item["product_id"] == product_id and item.get("variant_id") == variant_id)
        ]

        # Recalculate totals
        cart["totals"] = await CartService._calculate_totals(
            cart["items"],
            cart.get("coupon")
        )

        await CartService._save_cart(user_id, cart)
        return cart

    @staticmethod
    async def clear_cart(user_id: str) -> None:
        """Clear entire cart."""
        cart_key = CartService._get_cart_key(user_id)
        redis = _get_redis()
        await redis.delete(cart_key)

    @staticmethod
    async def apply_coupon(
        user_id: str,
        code: str,
        discount_type: str,
        discount_value: float,
        max_discount_amount: Optional[float] = None
    ) -> Dict[str, Any]:
        """Apply coupon to cart."""
        cart = await CartService.get_cart(user_id)

        # Calculate discount amount
        subtotal = sum(item["price"] * item["quantity"] for item in cart["items"])
        if discount_type == "percentage":
            discount_amount = subtotal * (discount_value / 100)
        else:
            discount_amount = min(discount_value, subtotal)
        # Same cap PricingService applies at quote/checkout — without it this
        # preview figure disagrees with cart["totals"]["discount"] below.
        if max_discount_amount is not None:
            discount_amount = min(discount_amount, max_discount_amount)

        cart["coupon"] = {
            "code": code,
            "discount_type": discount_type,
            "discount_value": discount_value,
            "max_discount_amount": max_discount_amount,
            "discount_amount": round(discount_amount, 2)
        }

        # Recalculate totals
        cart["totals"] = await CartService._calculate_totals(
            cart["items"],
            cart["coupon"]
        )

        await CartService._save_cart(user_id, cart)
        return cart

    @staticmethod
    async def remove_coupon(user_id: str) -> Dict[str, Any]:
        """Remove coupon from cart."""
        cart = await CartService.get_cart(user_id)
        cart["coupon"] = None

        # Recalculate totals
        cart["totals"] = await CartService._calculate_totals(cart["items"], None)

        await CartService._save_cart(user_id, cart)
        return cart

    @staticmethod
    async def get_item_count(user_id: str) -> int:
        """Get total number of items in cart."""
        cart = await CartService.get_cart(user_id)
        return sum(item["quantity"] for item in cart["items"])

    @staticmethod
    async def merge_cart(user_id: str, guest_items: list) -> Dict[str, Any]:
        """
        Merge guest cart items with user cart.

        Used when a guest logs in - combines their cart with any existing user cart.
        """
        cart = await CartService.get_cart(user_id)

        for guest_item in guest_items:
            # Check if item already exists
            existing_index = None
            for i, item in enumerate(cart["items"]):
                if (item["product_id"] == guest_item.get("product_id") and
                    item.get("variant_id") == guest_item.get("variant_id")):
                    existing_index = i
                    break

            if existing_index is not None:
                # Add quantities
                cart["items"][existing_index]["quantity"] += guest_item.get("quantity", 1)
            else:
                # Add new item
                cart["items"].append({
                    "product_id": guest_item.get("product_id"),
                    "quantity": guest_item.get("quantity", 1),
                    "price": guest_item.get("price", 0),
                    "name": guest_item.get("name", ""),
                    "image": guest_item.get("image"),
                    "sku": guest_item.get("sku"),
                    "variant_id": guest_item.get("variant_id")
                })

        # Recalculate totals
        cart["totals"] = await CartService._calculate_totals(
            cart["items"],
            cart.get("coupon")
        )

        await CartService._save_cart(user_id, cart)
        return cart

    @staticmethod
    async def sync_cart(user_id: str, local_cart: dict) -> Dict[str, Any]:
        """
        Sync local cart with server cart.

        Uses timestamps to determine which version is newer.
        For offline-first mobile support.
        """
        server_cart = await CartService.get_cart(user_id)
        local_updated = local_cart.get("updated_at", 0)
        server_updated = server_cart.get("updated_at", 0)

        if local_updated > server_updated:
            # Local cart is newer - replace server cart
            server_cart["items"] = local_cart.get("items", [])
            server_cart["coupon"] = local_cart.get("coupon")
        else:
            # Server cart is newer or same - keep server state
            # But add any items from local that don't exist on server
            local_items = local_cart.get("items", [])
            for local_item in local_items:
                exists = any(
                    item["product_id"] == local_item.get("product_id") and
                    item.get("variant_id") == local_item.get("variant_id")
                    for item in server_cart["items"]
                )
                if not exists:
                    server_cart["items"].append(local_item)

        # Recalculate totals
        server_cart["totals"] = await CartService._calculate_totals(
            server_cart["items"],
            server_cart.get("coupon")
        )

        await CartService._save_cart(user_id, server_cart)
        return server_cart
