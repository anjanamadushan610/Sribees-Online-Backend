"""
SRIBEESonline - Cart Sync Service

Cross-device cart synchronization and conflict resolution.
Syncs between Flutter local storage and Redis server cart.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from app.config.redis import get_redis
from app.services.cart_service import CartService


@dataclass
class CartItem:
    """Cart item structure."""
    product_id: str
    variant_id: Optional[str]
    quantity: int
    price: Decimal
    name: str
    image_url: Optional[str]
    added_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict:
        return {
            "product_id": self.product_id,
            "variant_id": self.variant_id,
            "quantity": self.quantity,
            "price": float(self.price),
            "name": self.name,
            "image_url": self.image_url,
            "added_at": self.added_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "CartItem":
        return cls(
            product_id=data["product_id"],
            variant_id=data.get("variant_id"),
            quantity=data["quantity"],
            price=Decimal(str(data["price"])),
            name=data["name"],
            image_url=data.get("image_url"),
            added_at=datetime.fromisoformat(data.get("added_at", datetime.utcnow().isoformat())),
            updated_at=datetime.fromisoformat(data.get("updated_at", datetime.utcnow().isoformat())),
        )


class CartSyncService:
    """
    Handles cart synchronization between devices.

    Sync Strategies:
    1. Server-wins: Always use server cart (simple, may lose local changes)
    2. Client-wins: Always use client cart (simple, may lose server changes)
    3. Merge: Combine both carts with conflict resolution (complex, most complete)
    4. Last-write-wins: Use the most recently updated version
    """

    SYNC_STRATEGY = "merge"  # Default strategy

    @classmethod
    async def sync_cart(
        cls,
        user_id: str,
        client_cart: Dict[str, Any],
        client_version: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], str, bool]:
        """
        Sync client cart with server cart.

        Args:
            user_id: User identifier
            client_cart: Cart data from client device
            client_version: Hash of last synced server cart
            strategy: Sync strategy override

        Returns:
            Tuple of (merged_cart, new_version, had_conflicts)
        """
        strategy = strategy or cls.SYNC_STRATEGY

        # Get server cart
        server_cart = await CartService.get_cart(user_id)
        server_version = cls._compute_cart_hash(server_cart)

        # Check if server cart changed since last sync
        server_changed = client_version and client_version != server_version

        # Check if client has changes
        client_changed = bool(client_cart.get("items"))

        # No changes needed
        if not client_changed and not server_changed:
            return server_cart, server_version, False

        # Apply sync strategy
        had_conflicts = False

        if strategy == "server_wins":
            merged_cart = server_cart
        elif strategy == "client_wins":
            merged_cart = client_cart
            await cls._save_cart(user_id, merged_cart)
        elif strategy == "last_write_wins":
            client_updated = client_cart.get("updated_at", 0)
            server_updated = server_cart.get("updated_at", 0)

            if client_updated > server_updated:
                merged_cart = client_cart
                await cls._save_cart(user_id, merged_cart)
            else:
                merged_cart = server_cart
        else:  # merge
            merged_cart, had_conflicts = await cls._merge_carts(
                server_cart,
                client_cart,
            )
            await cls._save_cart(user_id, merged_cart)

        new_version = cls._compute_cart_hash(merged_cart)

        if had_conflicts:
            logger.info(f"Cart sync had conflicts for user {user_id}")

        return merged_cart, new_version, had_conflicts

    @classmethod
    async def _merge_carts(
        cls,
        server_cart: Dict[str, Any],
        client_cart: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], bool]:
        """
        Merge two carts with conflict resolution.

        Resolution rules:
        1. Items in client but not server: Add to merged
        2. Items in server but not client: Keep in merged
        3. Items in both: Use the one with later updated_at
        4. Removed items: If client has 0 quantity, remove
        """
        server_items = {
            cls._item_key(item): CartItem.from_dict(item)
            for item in server_cart.get("items", [])
        }

        client_items = {
            cls._item_key(item): CartItem.from_dict(item)
            for item in client_cart.get("items", [])
        }

        merged_items: Dict[str, CartItem] = {}
        had_conflicts = False

        # Process all keys from both carts
        all_keys = set(server_items.keys()) | set(client_items.keys())

        for key in all_keys:
            server_item = server_items.get(key)
            client_item = client_items.get(key)

            if client_item and not server_item:
                # Client-only item: add it
                if client_item.quantity > 0:
                    merged_items[key] = client_item

            elif server_item and not client_item:
                # Server-only item: keep it
                merged_items[key] = server_item

            else:
                # Both have the item: resolve conflict
                had_conflicts = True

                # Check if client removed the item
                if client_item.quantity <= 0:
                    # Client removed it - don't include
                    continue

                # Use the more recent update
                if client_item.updated_at >= server_item.updated_at:
                    merged_items[key] = client_item
                else:
                    merged_items[key] = server_item

        merged_cart = {
            "items": [item.to_dict() for item in merged_items.values()],
            "coupon": client_cart.get("coupon") or server_cart.get("coupon"),
            "updated_at": int(datetime.utcnow().timestamp()),
        }

        # Recalculate totals
        merged_cart["totals"] = CartService._calculate_totals(
            merged_cart["items"],
            merged_cart.get("coupon"),
        )

        return merged_cart, had_conflicts

    @staticmethod
    def _item_key(item: Dict) -> str:
        """Generate unique key for cart item."""
        product_id = item.get("product_id", "")
        variant_id = item.get("variant_id", "")
        return f"{product_id}:{variant_id}"

    @staticmethod
    def _compute_cart_hash(cart: Dict[str, Any]) -> str:
        """Compute hash of cart for version comparison."""
        # Sort items for consistent hashing
        items = sorted(
            cart.get("items", []),
            key=lambda x: (x.get("product_id", ""), x.get("variant_id", "")),
        )

        cart_data = json.dumps({
            "items": items,
            "coupon": cart.get("coupon"),
        }, sort_keys=True)

        return hashlib.md5(cart_data.encode(), usedforsecurity=False).hexdigest()[:16]

    @classmethod
    async def _save_cart(cls, user_id: str, cart: Dict[str, Any]) -> None:
        """Save merged cart to Redis."""
        redis = get_redis()
        if not redis:
            return

        cart["updated_at"] = int(datetime.utcnow().timestamp())

        try:
            await redis.setex(
                f"cart:{user_id}",
                30 * 24 * 60 * 60,  # 30 days
                json.dumps(cart),
            )
        except Exception as e:
            logger.error(f"Failed to save cart: {e}")

    @classmethod
    async def get_cart_version(cls, user_id: str) -> Optional[str]:
        """Get current cart version hash."""
        cart = await CartService.get_cart(user_id)
        return cls._compute_cart_hash(cart)

    @classmethod
    async def transfer_guest_cart(
        cls,
        guest_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """
        Transfer a guest cart to a logged-in user.

        Called after login/registration.
        Merges guest cart with any existing user cart.
        """
        guest_cart = await CartService.get_cart(guest_id)
        user_cart = await CartService.get_cart(user_id)

        if not guest_cart.get("items"):
            # No guest cart, return user cart
            return user_cart

        if not user_cart.get("items"):
            # No user cart, transfer guest cart
            await cls._save_cart(user_id, guest_cart)
            await cls._clear_cart(guest_id)
            return guest_cart

        # Merge both carts
        merged_cart, _ = await cls._merge_carts(user_cart, guest_cart)
        await cls._save_cart(user_id, merged_cart)
        await cls._clear_cart(guest_id)

        logger.info(f"Transferred guest cart {guest_id} to user {user_id}")

        return merged_cart

    @classmethod
    async def _clear_cart(cls, user_id: str) -> None:
        """Clear a cart from Redis."""
        redis = get_redis()
        if redis:
            try:
                await redis.delete(f"cart:{user_id}")
            except Exception as e:
                logger.error(f"Failed to clear cart: {e}")
