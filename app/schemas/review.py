"""
SRIBEESonline - Review Schemas

Pydantic schemas for product reviews API.
"""
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ReviewCreate(BaseModel):
    """Schema for creating a review."""
    product_id: UUID
    rating: int = Field(..., ge=1, le=5, description="Rating from 1 to 5 stars")
    title: Optional[str] = Field(None, max_length=200)
    comment: Optional[str] = Field(None, max_length=5000)
    order_id: Optional[UUID] = None  # Optional: link to purchase for verified review

    @field_validator("title", "comment", mode="before")
    @classmethod
    def strip_strings(cls, v):
        if isinstance(v, str):
            return v.strip() or None
        return v


class SubmitReviewRequest(BaseModel):
    """Body for POST /products/{id}/reviews — product_id comes from the path.

    order_item_id is required: a review must name the delivered order line
    it's reviewing, so the server can verify it belongs to the caller,
    matches this product, and the order is actually delivered — see
    ProductReviewService.create_review.
    """
    rating: int = Field(..., ge=1, le=5, description="Star rating, 1-5")
    title: Optional[str] = Field(None, max_length=255)
    comment: Optional[str] = Field(None, max_length=2000)
    order_item_id: UUID

    @field_validator("title", "comment", mode="before")
    @classmethod
    def _strip(cls, v):
        if isinstance(v, str):
            return v.strip() or None
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "rating": 5,
                "title": "Great quality",
                "comment": "Fresh and delivered fast.",
                "order_item_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            }
        }


class ReviewUpdate(BaseModel):
    """Schema for updating a review."""
    rating: Optional[int] = Field(None, ge=1, le=5)
    title: Optional[str] = Field(None, max_length=200)
    comment: Optional[str] = Field(None, max_length=5000)


class ReviewUserResponse(BaseModel):
    """User info in review response."""
    user_id: UUID
    first_name: str
    last_name: Optional[str] = None
    avatar_url: Optional[str] = None

    class Config:
        from_attributes = True


class ReviewResponse(BaseModel):
    """Schema for review response."""
    review_id: UUID
    product_id: UUID
    rating: int
    title: Optional[str] = None
    comment: Optional[str] = None
    is_verified_purchase: bool = False
    is_featured: bool = False
    helpful_count: int = 0
    not_helpful_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime] = None

    # User info (without sensitive data)
    user: Optional[ReviewUserResponse] = None

    class Config:
        from_attributes = True


class ReviewListResponse(BaseModel):
    """Paginated list of reviews."""
    reviews: List[ReviewResponse]
    total: int
    page: int
    page_size: int
    average_rating: float
    rating_distribution: dict  # {1: count, 2: count, ...}


class ReviewSummary(BaseModel):
    """Summary statistics for product reviews."""
    product_id: UUID
    total_reviews: int
    average_rating: float
    rating_distribution: dict  # {1: count, 2: count, 3: count, 4: count, 5: count}
    verified_purchase_count: int
    with_comments_count: int


class ReviewVoteRequest(BaseModel):
    """Request to vote on a review."""
    is_helpful: bool


class ReviewVoteResponse(BaseModel):
    """Response after voting on a review."""
    review_id: UUID
    helpful_count: int
    not_helpful_count: int
    user_vote: Optional[bool] = None  # User's current vote
