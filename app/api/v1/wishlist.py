"""
Wishlist API Endpoints
"""
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.dependencies import get_current_user
from app.schemas.cart import WishlistItemAdd
from app.services.wishlist_service import WishlistService

# Prefix "/wishlist" is applied by app/api/v1/router.py — do not repeat it here.
router = APIRouter(tags=["Wishlist"])


# ============================================================================
# Helper Functions
# ============================================================================

def format_wishlist_item(item) -> dict:
    """Format wishlist item for response."""
    data = {
        "wishlist_item_id": str(item.wishlist_item_id),
        "user_id": str(item.user_id),
        "product_id": str(item.product_id),
        "variant_id": str(item.variant_id) if item.variant_id else None,
        "price_at_watch": float(item.price_at_watch) if item.price_at_watch else None,
        "added_at": item.added_at.isoformat() if item.added_at else None,
    }

    # Add product info if available
    if item.product:
        data["product"] = {
            "product_id": str(item.product.product_id),
            "name": item.product.name,
            "slug": item.product.slug,
            "price": float(item.product.price) if item.product.price else 0,
            # The product-wide rate, matching the global price this endpoint
            # already returns. A branch override is not applied here because
            # the wishlist is not branch-scoped.
            "cashback_percentage": (
                item.product.cashback_percentage
                if item.product.cashback_percentage is not None
                else float(settings.cashback_rate) * 100
            ),
            "images": [
                {"image_url": img.image_url, "is_primary": img.is_primary}
                for img in (item.product.images or [])[:1]  # Just primary image
            ]
        }

    # Add variant info if available
    if item.variant:
        data["variant_name"] = item.variant.name
        data["variant_sku"] = item.variant.sku
        data["current_price"] = float(item.variant.price) if item.variant.price else None
        data["variant_image"] = item.variant.image_url

        # Calculate price drop
        if item.price_at_watch and item.variant.price:
            price_drop = float(item.price_at_watch) - float(item.variant.price)
            data["price_drop"] = max(0, round(price_drop, 2))
            if item.price_at_watch > 0:
                data["price_drop_percentage"] = round(
                    (price_drop / float(item.price_at_watch)) * 100, 2
                )
            else:
                data["price_drop_percentage"] = 0
        else:
            data["price_drop"] = 0
            data["price_drop_percentage"] = 0

    return data


# ============================================================================
# Wishlist Endpoints
# ============================================================================

@router.get("", response_model=dict)
async def get_wishlist(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get current user's wishlist.
    """
    try:
        items = await WishlistService.get_by_user_id(db, current_user.user_id)

        return {
            "success": True,
            "data": {
                "items": [format_wishlist_item(item) for item in items],
                "count": len(items)
            }
        }
    except Exception as e:
        logger.error(f"Error getting wishlist: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get wishlist"
        )


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def add_to_wishlist(
    data: WishlistItemAdd,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Add an item to wishlist.
    """
    try:
        product_uuid = UUID(data.product_id)
        variant_uuid = UUID(data.variant_id) if data.variant_id else None
        price = Decimal(str(data.price_at_watch)) if data.price_at_watch else None

        item = await WishlistService.add_item(
            db,
            user_id=current_user.user_id,
            product_id=product_uuid,
            variant_id=variant_uuid,
            price_at_watch=price
        )

        return {
            "success": True,
            "data": {
                "wishlist_item_id": str(item.wishlist_item_id),
                "product_id": str(item.product_id),
                "variant_id": str(item.variant_id) if item.variant_id else None,
                "in_wishlist": True
            },
            "message": "Added to wishlist"
        }
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid product or variant ID"
        )
    except Exception as e:
        logger.error(f"Error adding to wishlist: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add to wishlist"
        )


@router.delete("/{product_id}", response_model=dict)
async def remove_from_wishlist(
    product_id: str,
    variant_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Remove an item from wishlist.
    """
    try:
        product_uuid = UUID(product_id)
        variant_uuid = UUID(variant_id) if variant_id else None

        removed = await WishlistService.remove_item(
            db,
            user_id=current_user.user_id,
            product_id=product_uuid,
            variant_id=variant_uuid
        )

        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Item not found in wishlist"
            )

        return {
            "success": True,
            "data": {
                "product_id": product_id,
                "in_wishlist": False
            },
            "message": "Removed from wishlist"
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid product or variant ID"
        )
    except Exception as e:
        logger.error(f"Error removing from wishlist: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove from wishlist"
        )


@router.post("/toggle/{product_id}", response_model=dict)
async def toggle_wishlist(
    product_id: str,
    variant_id: Optional[str] = None,
    price_at_watch: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Toggle item in wishlist (add if not exists, remove if exists).
    """
    try:
        product_uuid = UUID(product_id)
        variant_uuid = UUID(variant_id) if variant_id else None

        exists = await WishlistService.exists(
            db,
            user_id=current_user.user_id,
            product_id=product_uuid,
            variant_id=variant_uuid
        )

        if exists:
            await WishlistService.remove_item(
                db,
                user_id=current_user.user_id,
                product_id=product_uuid,
                variant_id=variant_uuid
            )
            return {
                "success": True,
                "data": {
                    "product_id": product_id,
                    "in_wishlist": False
                },
                "message": "Removed from wishlist"
            }
        else:
            price = Decimal(str(price_at_watch)) if price_at_watch else None
            item = await WishlistService.add_item(
                db,
                user_id=current_user.user_id,
                product_id=product_uuid,
                variant_id=variant_uuid,
                price_at_watch=price
            )
            return {
                "success": True,
                "data": {
                    "wishlist_item_id": str(item.wishlist_item_id),
                    "product_id": product_id,
                    "in_wishlist": True
                },
                "message": "Added to wishlist"
            }
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid product or variant ID"
        )
    except Exception as e:
        logger.error(f"Error toggling wishlist: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle wishlist"
        )


@router.get("/check/{product_id}", response_model=dict)
async def check_wishlist(
    product_id: str,
    variant_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Check if item is in wishlist.
    """
    try:
        product_uuid = UUID(product_id)
        variant_uuid = UUID(variant_id) if variant_id else None

        exists = await WishlistService.exists(
            db,
            user_id=current_user.user_id,
            product_id=product_uuid,
            variant_id=variant_uuid
        )

        return {
            "success": True,
            "data": {
                "product_id": product_id,
                "in_wishlist": exists
            }
        }
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid product or variant ID"
        )
    except Exception as e:
        logger.error(f"Error checking wishlist: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to check wishlist"
        )


@router.get("/price-drops", response_model=dict)
async def get_price_drops(
    min_drop: float = 0.50,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get wishlist items with price drops.
    """
    try:
        drops = await WishlistService.get_price_drops(
            db,
            user_id=current_user.user_id,
            min_drop_amount=Decimal(str(min_drop))
        )

        return {
            "success": True,
            "data": {
                "price_drops": drops,
                "count": len(drops)
            }
        }
    except Exception as e:
        logger.error(f"Error getting price drops: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get price drops"
        )


@router.delete("", response_model=dict)
async def clear_wishlist(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Clear entire wishlist.
    """
    try:
        await WishlistService.clear_wishlist(db, current_user.user_id)

        return {
            "success": True,
            "message": "Wishlist cleared successfully"
        }
    except Exception as e:
        logger.error(f"Error clearing wishlist: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to clear wishlist"
        )
