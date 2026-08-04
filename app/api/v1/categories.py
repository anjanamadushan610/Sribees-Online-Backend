"""
Category API Endpoints
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_admin
from app.core.media import media_url
from app.schemas.category import (
    CategoryCreate,
    CategoryUpdate,
)
from app.services.category_service import CategoryService

# Prefix "/categories" is applied by app/api/v1/router.py — do not repeat it here.
router = APIRouter(tags=["Categories"])


# ============================================================================
# Public Endpoints
# ============================================================================

@router.get("", response_model=dict)
async def get_categories(
    hierarchical: bool = Query(False, description="Return categories in tree structure"),
    include_inactive: bool = Query(False, description="Include inactive categories"),
    top_level_only: bool = Query(
        False,
        description="Only categories with no parent — what the mobile home screen shows",
    ),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all categories.

    - **hierarchical**: If true, returns categories in parent-child tree structure
    - **include_inactive**: If true, includes inactive categories (admin use)
    - **top_level_only**: If true, drops sub-categories. The mobile home screen
      renders one image tile per top-level category, so it would otherwise fetch
      the whole tree and throw most of it away.
    """
    try:
        if hierarchical:
            categories = await CategoryService.get_hierarchical(db)
        else:
            categories_raw = await CategoryService.get_all(db, include_inactive)
            if top_level_only:
                categories_raw = [
                    c for c in categories_raw if not c.get("parent_category_id")
                ]
            # Format for mobile app compatibility
            categories = [
                {
                    "_id": str(cat["category_id"]),
                    "category_id": str(cat["category_id"]),
                    "name": cat["name"],
                    "slug": cat["slug"],
                    "description": cat.get("description"),
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
    category_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get a single category by ID or slug.
    """
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
            "name": category.name,
            "slug": category.slug,
            "description": category.description,
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
