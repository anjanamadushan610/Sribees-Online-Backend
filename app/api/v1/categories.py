"""
Category API Endpoints
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis
from app.core.dependencies import get_current_admin, get_current_user_optional
from app.core.media import media_url
from app.schemas.category import (
    CategoryCreate,
    CategoryUpdate,
)
from app.services import branch_service
from app.services.category_service import CategoryService
from app.utils.i18n import localized, resolve_language

# Prefix "/categories" is applied by app/api/v1/router.py — do not repeat it here.
router = APIRouter(tags=["Categories"])


async def _optional_branch_id(redis: Redis, request: Request, current_user):
    """
    Best-effort branch resolution for a customer request.

    Mirrors the session-id convention in ``products._get_branch_context_for_request``
    but never raises: a guest who has not picked a delivery address yet (or a
    request with no device id) simply gets the unfiltered category list, the
    same behaviour this endpoint always had. Once a branch IS known, categories
    hidden for it (globally inactive, or not yet turned on by that branch's
    manager) are excluded — see ``CategoryService.get_all``.
    """
    if current_user is not None:
        session_id = str(current_user.user_id)
    else:
        device_id = request.headers.get("X-Device-Id") or request.headers.get("x-device-id")
        if not device_id or not device_id.strip():
            return None
        session_id = f"guest:{device_id.strip()}"

    context = await branch_service.get_branch_context(redis, session_id)
    if context is None:
        return None
    try:
        return UUID(context["branch_id"])
    except (KeyError, ValueError, TypeError):
        return None


# ============================================================================
# Public Endpoints
# ============================================================================

@router.get("", response_model=dict)
async def get_categories(
    request: Request,
    hierarchical: bool = Query(False, description="Return categories in tree structure"),
    include_inactive: bool = Query(False, description="Include inactive categories"),
    top_level_only: bool = Query(
        False,
        description="Only categories with no parent — what the mobile home screen shows",
    ),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user_optional),
):
    """
    Get all categories.

    - **hierarchical**: If true, returns categories in parent-child tree structure
    - **include_inactive**: If true, includes inactive categories (admin use)
    - **top_level_only**: If true, drops sub-categories. The mobile home screen
      renders one image tile per top-level category, so it would otherwise fetch
      the whole tree and throw most of it away.

    Names and descriptions come back in the caller's ``Accept-Language``
    (si/ta/en), falling back to English per field where no translation exists.

    When the caller's delivery branch is already known (resolved the same way
    product listings are), a category hidden for that branch is excluded —
    see ``CategoryService.get_all``. Unresolved (no address picked yet) falls
    back to the unfiltered list, same as before this existed.
    """
    try:
        lang = resolve_language(request.headers.get("accept-language"))
        branch_id = None if include_inactive else await _optional_branch_id(
            redis, request, current_user
        )

        if hierarchical:
            categories = await CategoryService.get_hierarchical(
                db, lang=lang, branch_id=branch_id
            )
        else:
            categories_raw = await CategoryService.get_all(
                db, include_inactive, branch_id=branch_id
            )
            if top_level_only:
                categories_raw = [
                    c for c in categories_raw if not c.get("parent_category_id")
                ]
            # Format for mobile app compatibility
            categories = [
                {
                    "_id": str(cat["category_id"]),
                    "category_id": str(cat["category_id"]),
                    "name": localized(cat, "name", lang),
                    "slug": cat["slug"],
                    "description": localized(cat, "description", lang),
                    "image_url": media_url(cat.get("image_url")),
                    "parent_category_id": str(cat["parent_category_id"]) if cat.get("parent_category_id") else None,
                    "is_active": cat["is_active"],
                    "product_count": cat.get("product_count", 0),
                }
                for cat in categories_raw
            ]

        return {
            "success": True,
            "data": {
                "categories": categories
            }
        }
    except Exception as e:
        logger.error(f"Error fetching categories: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch categories"
        )


@router.get("/{category_id}", response_model=dict)
async def get_category(
    request: Request,
    category_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get a single category by ID or slug.

    Name and description resolve to the caller's ``Accept-Language``, falling
    back to English per field.
    """
    lang = resolve_language(request.headers.get("accept-language"))
    category = await CategoryService.get_by_id_or_slug(db, category_id)

    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )

    return {
        "success": True,
        "data": {
            "_id": str(category.category_id),
            "category_id": str(category.category_id),
            "name": localized(category, "name", lang),
            "slug": category.slug,
            "description": localized(category, "description", lang),
            "image_url": media_url(category.image_url),
            "parent_category_id": str(category.parent_category_id) if category.parent_category_id else None,
            "is_active": category.is_active,
        }
    }


# ============================================================================
# Admin Endpoints
# ============================================================================

@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_category(
    data: CategoryCreate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Create a new category. (Admin only)
    """
    # Check for duplicate slug
    existing = await CategoryService.get_by_slug(db, data.slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category with this slug already exists"
        )

    try:
        category = await CategoryService.create(db, data)
        logger.info(f"Category created: {category.category_id} - {category.name}")

        return {
            "success": True,
            "data": {
                "_id": str(category.category_id),
                "category_id": str(category.category_id),
                "name": category.name,
                "slug": category.slug,
                "description": category.description,
                "image_url": media_url(category.image_url),
                "parent_category_id": str(category.parent_category_id) if category.parent_category_id else None,
                "is_active": category.is_active,
            },
            "message": "Category created successfully"
        }
    except Exception as e:
        logger.error(f"Error creating category: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create category"
        )


@router.put("/{category_id}", response_model=dict)
async def update_category(
    category_id: str,
    data: CategoryUpdate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Update a category. (Admin only)
    """
    category = await CategoryService.get_by_id_or_slug(db, category_id)

    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )

    # Check for duplicate slug if changing
    if data.slug and data.slug != category.slug:
        existing = await CategoryService.get_by_slug(db, data.slug)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Category with this slug already exists"
            )

    try:
        updated_category = await CategoryService.update(db, category, data)
        logger.info(f"Category updated: {updated_category.category_id}")

        return {
            "success": True,
            "data": {
                "_id": str(updated_category.category_id),
                "category_id": str(updated_category.category_id),
                "name": updated_category.name,
                "slug": updated_category.slug,
                "description": updated_category.description,
                "image_url": media_url(updated_category.image_url),
                "parent_category_id": str(updated_category.parent_category_id) if updated_category.parent_category_id else None,
                "is_active": updated_category.is_active,
            },
            "message": "Category updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating category: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update category"
        )


@router.delete("/{category_id}", response_model=dict)
async def delete_category(
    category_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin)
):
    """
    Delete a category. (Admin only)
    """
    category = await CategoryService.get_by_id_or_slug(db, category_id)

    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )

    # Check if category has products
    has_products = await CategoryService.has_products(db, category.category_id)
    if has_products:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete category with products. Remove products first or reassign them."
        )

    try:
        await CategoryService.delete(db, category)
        logger.info(f"Category deleted: {category_id}")

        return {
            "success": True,
            "message": "Category deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting category: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete category"
        )
