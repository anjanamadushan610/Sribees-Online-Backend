"""
Product API Endpoints

Enforces **Strict Branch-Specific Visibility**:
  - Every customer-facing listing / detail / search endpoint resolves the
    user's ``branch_id`` from Redis session context.
  - Products are only returned when a ``branch_inventory`` row exists for
    that branch, ``is_active = True``, and ``stock_quantity > 0``.
  - Prices, discounts and stock use COALESCE fallback (branch → global).
"""
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis
from app.core.dependencies import get_current_admin, get_current_user
from app.core.media import media_url
from app.schemas.product import (
    ProductCreate,
    ProductImageCreate,
    ProductUpdate,
    ProductVariantCreate,
)
from app.services import branch_service
from app.services.category_service import CategoryService
from app.services.product_review_service import ProductReviewService
from app.services.product_service import ProductService

# Prefix "/products" is applied by app/api/v1/router.py — do not repeat it here.
router = APIRouter(tags=["Products"])


# ============================================================================
# Helper Functions
# ============================================================================

def format_product(
    product,
    include_variants: bool = False,
    branch_overrides: dict = None,
    rating: dict = None,
) -> dict:
    """
    Format product for API response.

    If *branch_overrides* (from ``ProductService.compute_effective_data``)
    is provided, those effective values are merged into the response
    instead of the raw global fields.

    *rating* (optional) carries the aggregated review stats
    ``{"average": float, "count": int}``; when omitted both default to 0.
    """
    eff = branch_overrides or {}
    rating = rating or {}
    data = {
        "_id": str(product.product_id),
        "product_id": str(product.product_id),
        "name": product.name,
        "slug": product.slug,
        "description": product.description,
        "short_description": product.short_description,
        "sku": product.sku,
        "price": eff.get("effective_price", float(product.price) if product.price else 0),
        "global_price": float(product.price) if product.price else 0,
        "compare_at_price": float(product.compare_at_price) if product.compare_at_price else None,
        "discount_percentage": eff.get("effective_discount", product.discount_percentage),
        "discount_price": eff.get("effective_discount_price", float(product.discount_price) if product.discount_price else None),
        "is_on_sale": eff.get("is_on_sale", product.is_on_sale),
        "stock_quantity": eff.get("stock_quantity", product.stock_quantity),
        "is_active": eff.get("is_active", product.is_active),
        "is_featured": product.is_featured,
        "view_count": product.view_count,
        "average_rating": rating.get("average", 0),
        "review_count": rating.get("count", 0),
        "created_at": product.created_at.isoformat() if product.created_at else None,
        "updated_at": product.updated_at.isoformat() if product.updated_at else None,
    }

    # Category. The flat ids sit alongside the nested objects so clients can
    # filter/group without having to reach into the nested shape.
    data["category_id"] = str(product.category_id) if product.category_id else None
    data["subcategory_id"] = str(product.subcategory_id) if product.subcategory_id else None

    if product.category:
        data["category"] = {
            "category_id": str(product.category.category_id),
            "name": product.category.name,
            "slug": product.category.slug,
            "image_url": media_url(product.category.image_url),
        }
    else:
        data["category"] = None

    # Sub-category (a Category row whose parent_category_id == category_id)
    subcategory = getattr(product, "subcategory", None)
    if subcategory:
        data["subcategory"] = {
            "category_id": str(subcategory.category_id),
            "name": subcategory.name,
            "slug": subcategory.slug,
            "image_url": media_url(subcategory.image_url),
        }
    else:
        data["subcategory"] = None

    # Images
    data["images"] = [
        {
            "image_id": str(img.image_id),
            "image_url": media_url(img.image_url),
            "alt_text": img.alt_text,
            "is_primary": img.is_primary,
            "sort_order": img.sort_order,
        }
        for img in (product.images or [])
    ]

    # Variants
    if include_variants and hasattr(product, 'variants') and product.variants:
        data["has_variants"] = len(product.variants) > 0
        data["variants"] = [
            {
                "variant_id": str(v.variant_id),
                "name": v.name,
                "sku": v.sku,
                "price": float(v.price) if v.price else 0,
                "compare_at_price": float(v.compare_at_price) if v.compare_at_price else None,
                "stock_quantity": v.stock_quantity,
                "image_url": media_url(v.image_url),
                "is_active": v.is_active,
            }
            for v in product.variants
        ]
    else:
        data["has_variants"] = False
        data["variants"] = []

    return data


async def _get_branch_context(redis: Redis, user_id):
    """Resolve branch context from Redis; raise 400 if not set."""
    context = await branch_service.get_branch_context(redis, user_id)
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "error": {
                    "message": (
                        "No branch selected. Please select a delivery address "
                        "to view products."
                    ),
                    "code": "NO_BRANCH_CONTEXT",
                },
            },
        )
    return context


# ============================================================================
# Public Endpoints  (all require branch context for visibility)
# ============================================================================

@router.get("", response_model=dict)
async def get_products(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    category_id: Optional[str] = None,
    subcategory_id: Optional[str] = None,
    is_featured: Optional[bool] = None,
    search: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    sort_by: str = Query("created_at", pattern="^(created_at|price|name|view_count)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user),
):
    """
    Get products with filtering, pagination and **branch visibility enforcement**.

    Only products that exist in ``branch_inventory`` for the user's branch
    with ``is_active=True`` and ``stock_quantity > 0`` are returned.

    ``category_id`` matches the category and every sub-category under it;
    ``subcategory_id`` narrows to a single leaf.
    """
    try:
        context = await _get_branch_context(redis, current_user.user_id)
        branch_id = UUID(context["branch_id"])
        offset = (page - 1) * limit

        cat_uuid = None
        if category_id:
            try:
                cat_uuid = UUID(category_id)
            except ValueError:
                pass

        subcat_uuid = None
        if subcategory_id:
            try:
                subcat_uuid = UUID(subcategory_id)
            except ValueError:
                pass

        items, total = await ProductService.get_all_for_branch(
            db,
            branch_id=branch_id,
            limit=limit,
            offset=offset,
            category_id=cat_uuid,
            subcategory_id=subcat_uuid,
            is_featured=is_featured,
            search=search,
            min_price=Decimal(str(min_price)) if min_price else None,
            max_price=Decimal(str(max_price)) if max_price else None,
            sort_by=sort_by,
            sort_order=sort_order,
        )

        return {
            "success": True,
            "data": {
                "products": [
                    format_product(item["product"], branch_overrides=item["effective"])
                    for item in items
                ],
                "pagination": {
                    "total": total,
                    "page": page,
                    "limit": limit,
                    "pages": (total + limit - 1) // limit if limit else 1,
                },
                "branch": {
                    "branchId": context["branch_id"],
                    "branchName": context.get("branch_name"),
                },
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching products: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch products",
        )


@router.get("/featured", response_model=dict)
async def get_featured_products(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user),
):
    """
    Get featured products visible in the user's branch.
    """
    try:
        context = await _get_branch_context(redis, current_user.user_id)
        branch_id = UUID(context["branch_id"])

        items = await ProductService.get_featured_for_branch(db, branch_id, limit)

        return {
            "success": True,
            "data": {
                "products": [
                    format_product(item["product"], branch_overrides=item["effective"])
                    for item in items
                ],
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching featured products: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch featured products",
        )


@router.get("/home-feed", response_model=dict)
async def get_home_feed(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user),
):
    """
    Home-page Quick Sale feed.

    Returns the highest-discount products for the user's branch
    (resolved from Redis session context).  Prices, discounts and stock
    are resolved via COALESCE fallback (branch override -> global default).

    Returns ``400 NO_BRANCH_CONTEXT`` if no branch is selected.
    """
    context = await _get_branch_context(redis, current_user.user_id)
    branch_id = UUID(context["branch_id"])
    items = await ProductService.get_quick_sale_products(db, branch_id, limit)

    return {
        "success": True,
        "data": {
            "products": [
                format_product(item["product"], branch_overrides=item["effective"])
                for item in items
            ],
            "branch": {
                "branchId": context["branch_id"],
                "branchName": context["branch_name"],
            },
            "total": len(items),
        },
    }


@router.get("/search", response_model=dict)
async def search_products(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user),
):
    """
    Search products by name or description, filtered by branch visibility.
    """
    try:
        context = await _get_branch_context(redis, current_user.user_id)
        branch_id = UUID(context["branch_id"])
        offset = (page - 1) * limit

        items, total = await ProductService.get_all_for_branch(
            db,
            branch_id=branch_id,
            limit=limit,
            offset=offset,
            search=q,
        )

        return {
            "success": True,
            "data": {
                "products": [
                    format_product(item["product"], branch_overrides=item["effective"])
                    for item in items
                ],
                "pagination": {
                    "total": total,
                    "page": page,
                    "limit": limit,
                    "pages": (total + limit - 1) // limit if limit else 1,
                },
                "query": q,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error searching products: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to search products",
        )


@router.get("/category/{category_id}", response_model=dict)
async def get_products_by_category(
    category_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user),
):
    """
    Get products by category, filtered by branch visibility.
    """
    try:
        context = await _get_branch_context(redis, current_user.user_id)
        branch_id = UUID(context["branch_id"])
        offset = (page - 1) * limit

        try:
            cat_uuid = UUID(category_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid category ID",
            )

        items, total = await ProductService.get_all_for_branch(
            db,
            branch_id=branch_id,
            limit=limit,
            offset=offset,
            category_id=cat_uuid,
        )

        return {
            "success": True,
            "data": {
                "products": [
                    format_product(item["product"], branch_overrides=item["effective"])
                    for item in items
                ],
                "pagination": {
                    "total": total,
                    "page": page,
                    "limit": limit,
                    "pages": (total + limit - 1) // limit if limit else 1,
                },
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching category products: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch products",
        )


@router.get("/{product_id}", response_model=dict)
async def get_product(
    product_id: str,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user),
):
    """
    Get a single product by ID or slug.

    Returns ``404`` if the product is not active / stocked in the user's
    resolved branch — even if it exists in the Global Catalog.
    """
    context = await _get_branch_context(redis, current_user.user_id)
    branch_id = UUID(context["branch_id"])

    # Resolve the product_id (may be UUID or slug)
    try:
        product_uuid = UUID(product_id)
    except ValueError:
        product_obj = await ProductService.get_by_slug(db, product_id)
        if product_obj is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "success": False,
                    "error": {
                        "message": "Product not found or not available in your branch",
                        "code": "PRODUCT_NOT_FOUND",
                    },
                },
            )
        product_uuid = product_obj.product_id

    result = await ProductService.get_by_id_for_branch(db, product_uuid, branch_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "success": False,
                "error": {
                    "message": "Product not found or not available in your branch",
                    "code": "PRODUCT_NOT_FOUND",
                },
            },
        )

    # Increment view count (fire and forget)
    try:
        await ProductService.increment_view_count(db, product_uuid)
    except Exception:
        pass

    # Aggregate review stats for this product.
    average, count, _ = await ProductReviewService.get_rating_summary(db, product_uuid)

    return {
        "success": True,
        "data": format_product(
            result["product"],
            include_variants=True,
            branch_overrides=result["effective"],
            rating={"average": average, "count": count},
        ),
    }


@router.get("/{product_id}/variants", response_model=dict)
async def get_product_variants(
    product_id: str,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user),
):
    """
    Get variants for a product (only if the product is visible in the branch).
    """
    context = await _get_branch_context(redis, current_user.user_id)
    branch_id = UUID(context["branch_id"])

    try:
        product_uuid = UUID(product_id)
    except ValueError:
        product_obj = await ProductService.get_by_slug(db, product_id)
        if product_obj is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
        product_uuid = product_obj.product_id

    result = await ProductService.get_by_id_for_branch(db, product_uuid, branch_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found or not available in your branch",
        )

    variants = await ProductService.get_variants(db, product_uuid)

    return {
        "success": True,
        "data": {
            "variants": [
                {
                    "variant_id": str(v.variant_id),
                    "name": v.name,
                    "sku": v.sku,
                    "price": float(v.price) if v.price else 0,
                    "compare_at_price": float(v.compare_at_price) if v.compare_at_price else None,
                    "stock_quantity": v.stock_quantity,
                    "image_url": media_url(v.image_url),
                    "is_active": v.is_active,
                }
                for v in variants
            ]
        },
    }


# ============================================================================
# Admin Endpoints
# ============================================================================

@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_product(
    data: ProductCreate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Create a new product. (Admin only)
    """
    # Check for duplicate slug
    existing = await ProductService.get_by_slug(db, data.slug, include_inactive=True)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Product with this slug already exists"
        )

    try:
        await CategoryService.validate_subcategory(db, data.category_id, data.subcategory_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    try:
        product = await ProductService.create(db, data)
        logger.info(f"Product created: {product.product_id} - {product.name}")

        return {
            "success": True,
            "data": format_product(product),
            "message": "Product created successfully"
        }
    except Exception as e:
        logger.error(f"Error creating product: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create product"
        )


@router.put("/{product_id}", response_model=dict)
async def update_product(
    product_id: str,
    data: ProductUpdate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Update a product. (Admin only)
    """
    product = await ProductService.get_by_id_or_slug(db, product_id, include_inactive=True)

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )

    # Check for duplicate slug if changing
    if data.slug and data.slug != product.slug:
        existing = await ProductService.get_by_slug(db, data.slug, include_inactive=True)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Product with this slug already exists"
            )

    # Validate the category/sub-category pair the product ends up with, since a
    # partial update may move only one half of it.
    fields = data.model_dump(exclude_unset=True)
    try:
        await CategoryService.validate_subcategory(
            db,
            fields.get("category_id", product.category_id),
            fields.get("subcategory_id", product.subcategory_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    try:
        updated_product = await ProductService.update(db, product, data)
        logger.info(f"Product updated: {updated_product.product_id}")

        return {
            "success": True,
            "data": format_product(updated_product),
            "message": "Product updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating product: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update product"
        )


@router.delete("/{product_id}", response_model=dict)
async def delete_product(
    product_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Delete a product. (Admin only)
    """
    product = await ProductService.get_by_id_or_slug(db, product_id, include_inactive=True)

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )

    try:
        await ProductService.delete(db, product)
        logger.info(f"Product deleted: {product_id}")

        return {
            "success": True,
            "message": "Product deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting product: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete product"
        )


@router.post("/{product_id}/images", response_model=dict, status_code=status.HTTP_201_CREATED)
async def add_product_image(
    product_id: str,
    data: ProductImageCreate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Add an image to a product. (Admin only)
    """
    product = await ProductService.get_by_id_or_slug(db, product_id, include_inactive=True)

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )

    try:
        image = await ProductService.add_image(db, product.product_id, data)

        return {
            "success": True,
            "data": {
                "image_id": str(image.image_id),
                "image_url": media_url(image.image_url),
                "alt_text": image.alt_text,
                "is_primary": image.is_primary,
                "sort_order": image.sort_order,
            },
            "message": "Image added successfully"
        }
    except Exception as e:
        logger.error(f"Error adding product image: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add image"
        )


@router.delete("/{product_id}/images/{image_id}", response_model=dict)
async def remove_product_image(
    product_id: str,
    image_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Remove an image from a product. (Admin only)
    """
    try:
        await ProductService.remove_image(db, UUID(image_id))

        return {
            "success": True,
            "message": "Image removed successfully"
        }
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid image ID"
        )
    except Exception as e:
        logger.error(f"Error removing product image: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove image"
        )


@router.post("/{product_id}/variants", response_model=dict, status_code=status.HTTP_201_CREATED)
async def add_product_variant(
    product_id: str,
    data: ProductVariantCreate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Add a variant to a product. (Admin only)
    """
    product = await ProductService.get_by_id_or_slug(db, product_id, include_inactive=True)

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )

    try:
        variant = await ProductService.add_variant(db, product.product_id, data)

        return {
            "success": True,
            "data": {
                "variant_id": str(variant.variant_id),
                "name": variant.name,
                "sku": variant.sku,
                "price": float(variant.price) if variant.price else 0,
                "compare_at_price": float(variant.compare_at_price) if variant.compare_at_price else None,
                "stock_quantity": variant.stock_quantity,
                "image_url": media_url(variant.image_url),
                "is_active": variant.is_active,
            },
            "message": "Variant added successfully"
        }
    except Exception as e:
        logger.error(f"Error adding product variant: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add variant"
        )


@router.patch("/{product_id}/stock", response_model=dict)
async def update_product_stock(
    product_id: str,
    quantity: int = Query(..., ge=0),
    variant_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Update product or variant stock quantity. (Admin only)
    """
    product = await ProductService.get_by_id_or_slug(db, product_id, include_inactive=True)

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )

    try:
        var_uuid = UUID(variant_id) if variant_id else None
        await ProductService.update_stock(db, product.product_id, quantity, var_uuid)

        return {
            "success": True,
            "message": "Stock updated successfully"
        }
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid variant ID"
        )
    except Exception as e:
        logger.error(f"Error updating stock: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update stock"
        )
