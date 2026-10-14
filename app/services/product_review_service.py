"""
Product Review Service

Business logic for product reviews, built on the registered
``app.models.product.Review`` model (the one wired to Product.reviews /
User.reviews). Ratings are aggregated on the fly, so no denormalized columns
on Product are required.
"""
from typing import Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product, Review


class ProductReviewService:
    """Review operations on the registered Review model."""

    @staticmethod
    async def get_rating_summaries(
        db: AsyncSession, product_ids: Iterable[UUID]
    ) -> Dict[UUID, dict]:
        """
        ``{product_id: {"average": float, "count": int}}`` for a page of
        products in one query, so listing cards can show their stars.
        Products with no approved review are simply absent.
        """
        ids = list(product_ids)
        if not ids:
            return {}
        rows = (
            await db.execute(
                select(
                    Review.product_id,
                    func.avg(Review.rating),
                    func.count(Review.review_id),
                )
                .where(
                    Review.product_id.in_(ids),
                    Review.is_approved == True,  # noqa: E712
                )
                .group_by(Review.product_id)
            )
        ).all()
        return {
            pid: {"average": round(float(avg), 2), "count": int(count)}
            for pid, avg, count in rows
        }

    @staticmethod
    async def get_rating_summary(
        db: AsyncSession, product_id: UUID
    ) -> Tuple[float, int, dict]:
        """Return (average_rating, review_count, distribution) for a product."""
        row = (
            await db.execute(
                select(
                    func.avg(Review.rating),
                    func.count(Review.review_id),
                ).where(
                    and_(
                        Review.product_id == product_id,
                        Review.is_approved == True,  # noqa: E712
                    )
                )
            )
        ).one()

        average = round(float(row[0]), 2) if row[0] is not None else 0.0
        count = int(row[1] or 0)

        distribution = {str(i): 0 for i in range(1, 6)}
        dist_rows = (
            await db.execute(
                select(Review.rating, func.count(Review.review_id))
                .where(
                    and_(
                        Review.product_id == product_id,
                        Review.is_approved == True,  # noqa: E712
                    )
                )
                .group_by(Review.rating)
            )
        ).all()
        for rating, c in dist_rows:
            distribution[str(int(rating))] = int(c)

        return average, count, distribution

    @staticmethod
    async def get_product_reviews(
        db: AsyncSession,
        product_id: UUID,
        page: int = 1,
        page_size: int = 10,
        sort_by: str = "newest",
    ) -> Tuple[List[Review], int, float, dict]:
        """Return a page of reviews plus the rating summary."""
        query = (
            select(Review)
            .options(selectinload(Review.user))
            .where(
                and_(
                    Review.product_id == product_id,
                    Review.is_approved == True,  # noqa: E712
                )
            )
        )

        if sort_by == "highest":
            query = query.order_by(Review.rating.desc(), Review.created_at.desc())
        elif sort_by == "lowest":
            query = query.order_by(Review.rating.asc(), Review.created_at.desc())
        else:  # newest
            query = query.order_by(Review.created_at.desc())

        offset = (page - 1) * page_size
        reviews = (
            await db.execute(query.offset(offset).limit(page_size))
        ).scalars().all()

        average, count, distribution = await ProductReviewService.get_rating_summary(
            db, product_id
        )
        return list(reviews), count, average, distribution

    @staticmethod
    async def has_reviewed(
        db: AsyncSession, product_id: UUID, user_id: UUID
    ) -> bool:
        """True if the user already has a review for this product."""
        existing = (
            await db.execute(
                select(Review.review_id).where(
                    and_(
                        Review.product_id == product_id,
                        Review.user_id == user_id,
                    )
                )
            )
        ).scalar_one_or_none()
        return existing is not None

    @staticmethod
    async def get_reviewed_product_ids(
        db: AsyncSession, user_id: UUID, product_ids: List[UUID]
    ) -> set[UUID]:
        """Which of these product ids does the user already have a review
        for? Used to show "✓ Reviewed" instead of a write-review action per
        line item on a delivered order (app/api/v1/orders.py)."""
        if not product_ids:
            return set()
        rows = (
            await db.execute(
                select(Review.product_id).where(
                    and_(
                        Review.user_id == user_id,
                        Review.product_id.in_(product_ids),
                    )
                )
            )
        ).scalars().all()
        return set(rows)

    @staticmethod
    async def create_review(
        db: AsyncSession,
        product_id: UUID,
        user_id: UUID,
        rating: int,
        order_item_id: UUID,
        title: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> Review:
        """
        Create a review. Enforces one review per (user, product), and that
        order_item_id is a real, delivered order line belonging to this user
        for this exact product — a review must be tied to a purchase the
        customer actually received, not just any account/product pair.

        Raises ValueError for validation / duplicate / missing product /
        ownership / delivery-status failures (see submit_product_review in
        app/api/v1/reviews.py for how each message maps to an HTTP status).
        """
        if rating < 1 or rating > 5:
            raise ValueError("Rating must be between 1 and 5")

        product = await db.get(Product, product_id)
        if not product:
            raise ValueError("Product not found")

        if await ProductReviewService.has_reviewed(db, product_id, user_id):
            raise ValueError("You have already reviewed this product")

        row = (
            await db.execute(
                select(OrderItem, Order.user_id, Order.status)
                .join(Order, Order.order_id == OrderItem.order_id)
                .where(OrderItem.order_item_id == order_item_id)
            )
        ).one_or_none()
        if row is None:
            raise ValueError("Order item not found")
        order_item, owner_id, order_status = row
        if owner_id != user_id:
            raise ValueError("This order does not belong to you")
        if order_item.product_id != product_id:
            raise ValueError("This order item is for a different product")
        if order_status != OrderStatus.DELIVERED.value:
            raise ValueError("You can review a product only after it has been delivered")

        review = Review(
            product_id=product_id,
            user_id=user_id,
            order_item_id=order_item_id,
            rating=rating,
            title=title,
            comment=comment,
            is_verified_purchase=True,
        )
        db.add(review)
        await db.commit()

        # Reload with the user relationship for response formatting.
        loaded = (
            await db.execute(
                select(Review)
                .options(selectinload(Review.user))
                .where(Review.review_id == review.review_id)
            )
        ).scalar_one()

        logger.info(f"Review created: {loaded.review_id} for product {product_id}")
        return loaded
