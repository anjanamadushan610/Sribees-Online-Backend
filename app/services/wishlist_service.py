"""
Wishlist Service
"""
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.product import Product
from app.models.wishlist import WishlistItem


class WishlistService:
    """Service class for wishlist operations."""

    @staticmethod
    async def get_by_user_id(
        db: AsyncSession,
        user_id: UUID
    ) -> List[WishlistItem]:
        """Get user's wishlist with details."""
        result = await db.execute(
            select(WishlistItem)
            .options(
                # format_wishlist_item reads product.images, which is
                # lazy by default — without this nested eager-load it
                # triggers an async lazy-load outside of a request context
                # and 500s (SQLAlchemy's "greenlet_spawn has not been
                # called").
                selectinload(WishlistItem.product).selectinload(Product.images),
                selectinload(WishlistItem.variant)
            )
            .where(WishlistItem.user_id == user_id)
            .order_by(WishlistItem.added_at.desc())
        )
        return result.scalars().all()

    @staticmethod
    async def add_item(
        db: AsyncSession,
        user_id: UUID,
        product_id: UUID,
        variant_id: Optional[UUID] = None,
        price_at_watch: Optional[Decimal] = None
    ) -> WishlistItem:
        """Add item to wishlist."""
        # Check if already exists
        existing = await WishlistService.get_item(db, user_id, product_id, variant_id)

        if existing:
            # Update price_at_watch
            existing.price_at_watch = price_at_watch
            await db.commit()
            await db.refresh(existing)
            return existing

        # Create new
        item = WishlistItem(
            user_id=user_id,
            product_id=product_id,
            variant_id=variant_id,
            price_at_watch=price_at_watch
        )

        db.add(item)
        await db.commit()
        await db.refresh(item)

        return item

    @staticmethod
    async def remove_item(
        db: AsyncSession,
        user_id: UUID,
        product_id: UUID,
        variant_id: Optional[UUID] = None
    ) -> bool:
        """Remove item from wishlist."""
        item = await WishlistService.get_item(db, user_id, product_id, variant_id)

        if item:
            await db.delete(item)
            await db.commit()
            return True

        return False

    @staticmethod
    async def get_item(
        db: AsyncSession,
        user_id: UUID,
        product_id: UUID,
        variant_id: Optional[UUID] = None
    ) -> Optional[WishlistItem]:
        """Get specific wishlist item."""
        if variant_id:
            condition = and_(
                WishlistItem.user_id == user_id,
                WishlistItem.product_id == product_id,
                WishlistItem.variant_id == variant_id
            )
        else:
            condition = and_(
                WishlistItem.user_id == user_id,
                WishlistItem.product_id == product_id,
                WishlistItem.variant_id.is_(None)
            )

        result = await db.execute(
            select(WishlistItem).where(condition)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def exists(
        db: AsyncSession,
        user_id: UUID,
        product_id: UUID,
        variant_id: Optional[UUID] = None
    ) -> bool:
        """Check if item exists in wishlist."""
        item = await WishlistService.get_item(db, user_id, product_id, variant_id)
        return item is not None

    @staticmethod
    async def get_price_drops(
        db: AsyncSession,
        user_id: UUID,
        min_drop_amount: Decimal = Decimal("0.50")
    ) -> List[dict]:
        """Get wishlist items with price drops."""
        items = await WishlistService.get_by_user_id(db, user_id)
        price_drops = []

        for item in items:
            if item.price_at_watch and item.variant and item.variant.price:
                current_price = item.variant.price
                price_drop = item.price_at_watch - current_price

                if price_drop >= min_drop_amount:
                    price_drops.append({
                        "wishlist_item_id": str(item.wishlist_item_id),
                        "product_id": str(item.product_id),
                        "variant_id": str(item.variant_id) if item.variant_id else None,
                        "price_at_watch": float(item.price_at_watch),
                        "current_price": float(current_price),
                        "price_drop": float(price_drop),
                        "price_drop_percentage": round((price_drop / item.price_at_watch) * 100, 2)
                    })

        return sorted(price_drops, key=lambda x: x["price_drop"], reverse=True)

    @staticmethod
    async def clear_wishlist(db: AsyncSession, user_id: UUID) -> None:
        """Clear user's wishlist."""
        result = await db.execute(
            select(WishlistItem).where(WishlistItem.user_id == user_id)
        )
        items = result.scalars().all()

        for item in items:
            await db.delete(item)

        await db.commit()

    @staticmethod
    async def get_count(db: AsyncSession, user_id: UUID) -> int:
        """Get wishlist item count."""
        result = await db.execute(
            select(func.count(WishlistItem.wishlist_item_id))
            .where(WishlistItem.user_id == user_id)
        )
        return result.scalar() or 0
