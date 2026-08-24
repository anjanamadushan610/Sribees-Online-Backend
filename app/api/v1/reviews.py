"""
Product Review API Endpoints

    GET  /api/v1/products/{id}/reviews  - paginated reviews (public)
    POST /api/v1/products/{id}/reviews  - submit a review (auth; one per product)

Prefix "/products" is applied by app/api/v1/router.py (shared with the products
router — the paths here are distinct, so there is no collision).
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_user
from app.schemas.review import SubmitReviewRequest
from app.services.product_review_service import ProductReviewService

router = APIRouter(tags=["Reviews"])


def format_review(review) -> dict:
    """Shape a Review for the mobile contract (raw object, snake_case)."""
    user = getattr(review, "user", None)
    return {
        "review_id": str(review.review_id),
        "product_id": str(review.product_id),
        "user_id": str(review.user_id),
        # The mobile client reads user.full_name for the reviewer's name.
        "user": {"full_name": user.full_name} if user else None,
        "rating": review.rating,
        "title": review.title,
        "comment": review.comment,
        "is_verified_purchase": review.is_verified_purchase,
        "helpful_count": 0,
        "created_at": review.created_at.isoformat() if review.created_at else None,
    }


def _parse_product_id(product_id: str) -> UUID:
    try:
        return UUID(product_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )


@router.get("/{product_id}/reviews", response_model=dict)
async def list_product_reviews(
    product_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
    sort_by: str = Query("newest", description="newest | highest | lowest"),
    db: AsyncSession = Depends(get_db),
):
    """List paginated reviews for a product (newest first by default)."""
    pid = _parse_product_id(product_id)
    try:
        reviews, total, average, distribution = (
            await ProductReviewService.get_product_reviews(
                db, pid, page=page, page_size=page_size, sort_by=sort_by
            )
        )
    except Exception as e:
        logger.error(f"Error fetching reviews: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch reviews",
        )

    return {
        "reviews": [format_review(r) for r in reviews],
        "total": total,
        "average_rating": average,
        "rating_distribution": distribution,
    }


@router.post(
    "/{product_id}/reviews",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
)
async def submit_product_review(
    product_id: str,
    data: SubmitReviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Submit a review for a product.

    A user may review a given product only once (duplicate → 409). The
    review must name a delivered order line (order_item_id) that belongs to
    the caller and matches this product — see ProductReviewService.create_review.
    """
    pid = _parse_product_id(product_id)
    try:
        review = await ProductReviewService.create_review(
            db,
            product_id=pid,
            user_id=current_user.user_id,
            rating=data.rating,
            order_item_id=data.order_item_id,
            title=data.title,
            comment=data.comment,
        )
    except ValueError as e:
        message = str(e)
        if "already reviewed" in message:
            code = status.HTTP_409_CONFLICT
        elif "not found" in message:
            code = status.HTTP_404_NOT_FOUND
        elif "does not belong to you" in message:
            code = status.HTTP_403_FORBIDDEN
        else:
            code = status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message)
    except Exception as e:
        logger.error(f"Error creating review: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to submit review",
        )

    return format_review(review)
