"""
Wishlist API Endpoints
"""
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis
from app.config.settings import settings
from app.core.dependencies import get_current_user
from app.core.media import media_url
from app.models.product import BranchInventory
from app.schemas.cart import WishlistItemAdd
from app.services import branch_service
from app.services.product_service import ProductService
from app.services.wishlist_service import WishlistService

# Prefix "/wishlist" is applied by app/api/v1/router.py — do not repeat it here.
router = APIRouter(tags=["Wishlist"])


# ============================================================================
# Helper Functions
# ============================================================================

def format_wishlist_item(
    item,
    cashback_map: Optional[dict] = None,
    branch_inv_map: Optional[dict] = None,
) -> dict:
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
        inv = (branch_inv_map or {}).get(item.product.product_id)
        eff = ProductService.compute_effective_data(item.product, inv)

        data["product"] = {
            "product_id": str(item.product.product_id),
            "name": item.product.name,
            "slug": item.product.slug,
            "price": eff.get("effective_price") if eff.get("effective_price") is not None else (float(item.product.price) if item.product.price else 0),
            "global_price": float(item.product.price) if item.product.price else 0,
            "discount_percentage": eff.get("effective_discount") if eff.get("effective_discount") is not None else item.product.discount_percentage,
            "discount_price": eff.get("effective_discount_price") or (float(item.product.discount_price) if item.product.discount_price else None),
            "sale_price": eff.get("effective_discount_price") or (float(item.product.discount_price) if item.product.discount_price else None),
            "is_on_sale": eff.get("is_on_sale") if eff.get("is_on_sale") is not None else bool(item.product.is_on_sale),
            # Branch-resolved rate when the caller's branch is known (see
            # get_wishlist), falling back to the product/platform default
            # otherwise — same resolution order as the product listing.
            "cashback_percentage": (
                cashback_map.get(str(item.product.product_id))
                if cashback_map is not None
                else (
                    item.product.cashback_percentage
                    if item.product.cashback_percentage is not None
                    else float(settings.cashback_rate) * 100
                )
            ),
            # The raw, unresolved override (null = no bonus set) — the app
            # uses this, not the resolved rate above, to decide whether to
            # show the cashback badge at all.
            "global_cashback_percentage": (
                float(item.product.cashback_percentage)
                if item.product.cashback_percentage is not None
                else None
            ),
            "images": [
                {"image_url": media_url(img.image_url), "is_primary": img.is_primary}
                for img in (item.product.images or [])[:1]  # Just primary image
            ]
        }

    # Add variant info if available
    if item.variant:
        data["variant_name"] = item.variant.name
        data["variant_sku"] = item.variant.sku
        data["current_price"] = float(item.variant.price) if item.variant.price else None
        data["variant_image"] = media_url(item.variant.image_url)

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
    redis: Redis = Depends(get_redis),
    current_user = Depends(get_current_user)
):
    """
    Get current user's wishlist.
    """
    try:
        items = await WishlistService.get_by_user_id(db, current_user.user_id)

        # Branch-resolved cashback, same source as the product listing and
        # checkout quote. Falls through to product/platform default when the
        # customer has no branch selected yet (branch_id=None is a valid
        # input to get_cashback_map).
        branch_id = None
        context = await branch_service.get_branch_context(redis, str(current_user.user_id))
        if context is not None:
            try:
                branch_id = UUID(context["branch_id"])
            except (KeyError, ValueError, TypeError):
                branch_id = None

        product_ids = [item.product.product_id for item in items if item.product]
        cashback_map = await ProductService.get_cashback_map(db, product_ids, branch_id)

        # Load branch inventory overrides if branch_id is known
        branch_inv_map = {}
        if branch_id and product_ids:
            inv_res = await db.execute(
                select(BranchInventory).where(
                    BranchInventory.branch_id == branch_id,
                    BranchInventory.product_id.in_(product_ids),
                )
            )
            branch_inv_map = {inv.product_id: inv for inv in inv_res.scalars().all()}

        return {
            "success": True,
            "data": {
                "items": [
                    format_wishlist_item(item, cashback_map, branch_inv_map)
                    for item in items
                ],
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
