"""
Admin Catalog API Endpoints

Global Catalog management for the React Admin Dashboard (Modules 7.2 & 7.4):
Category CRUD and Product CRUD + image management, plus a multipart image
upload endpoint that pushes files to object storage.

Unlike the public ``/products`` router — which enforces strict per-branch
visibility from the customer's Redis session — these endpoints operate on the
**global** catalog (all products, active and inactive) with no branch context.

RBAC: every route in this module is restricted to ``super_admin`` and
``inventory_manager`` (enforced at the router level below). This mirrors the
frontend RoleGuard as defense-in-depth.

Prefix "/admin" is applied by app/api/v1/router.py — do not repeat it here.
"""
from typing import Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.products import format_product
from app.config.database import get_db
from app.config.settings import settings
from app.core.dependencies import require_roles
from app.core.media import media_path, media_url
from app.schemas.category import CategoryCreate, CategoryUpdate
from app.schemas.product import ProductCreate, ProductImageCreate, ProductUpdate
from app.services.category_service import CategoryService
from app.services.product_service import ProductService
from app.services.storage import StorageError, get_storage

# Catalog management is limited to Super Admins and Inventory Managers.
router = APIRouter(
    dependencies=[Depends(require_roles("super_admin", "inventory_manager"))],
    tags=["Admin Catalog"],
)

ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}


async def _validate_parent(
    db: AsyncSession,
    parent_id: Optional[UUID],
    self_id: Optional[UUID] = None,
) -> None:
    """
    Guard the two-level Category → Sub-category hierarchy.

    Products carry exactly one ``category_id`` and one ``subcategory_id``, so
    the tree is deliberately capped at two levels: a sub-category's parent must
    be a *root* category. Allowing deeper nesting would create categories that
    no product could ever reference.
    """
    if parent_id is None:
        return

    if self_id is not None and parent_id == self_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A category cannot be its own parent.",
        )

    parent = await CategoryService.get_by_id(db, parent_id)
    if parent is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Parent category not found.",
        )

    if parent.parent_category_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Categories are only two levels deep — a sub-category cannot "
                   "be nested under another sub-category.",
        )

    # Demoting a parent that already has children would orphan them a level down.
    if self_id is not None and await CategoryService.has_children(db, self_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This category has sub-categories, so it cannot itself become "
                   "a sub-category.",
        )


def _validate_category_image(parent_id: Optional[UUID], image_url: Optional[str]) -> None:
    """
    Images belong to top-level categories only.

    The mobile home screen renders one tile per top-level category *from its
    image*; sub-categories appear as text beneath their parent and have nowhere
    to show one. Accepting an image on a sub-category would store a file that no
    screen can ever display, so reject it rather than silently keep it.
    """
    if parent_id is not None and image_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Images are only supported on top-level categories. "
                "A sub-category inherits its parent's imagery."
            ),
        )


async def _upload_image(file: UploadFile, folder: str) -> str:
    """
    Validate an uploaded image, store it, and return its **relative path**.

    The path — not an absolute URL — is what callers persist. ``media_url()``
    turns it into a fetchable URL at serialization time.
    """
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid file type '{file.content_type}'. "
                f"Allowed: {', '.join(sorted(ALLOWED_IMAGE_TYPES))}"
            ),
        )

    max_mb = settings.media_max_upload_size_mb
    contents = await file.read()
    if len(contents) > max_mb * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"File too large ({len(contents) / 1024 / 1024:.1f} MB). "
                f"Maximum allowed: {max_mb} MB."
            ),
        )

    import io

    try:
        return get_storage().save(
            fileobj=io.BytesIO(contents),
            filename=file.filename or "image.jpg",
            folder=folder,
            content_type=file.content_type,
        )
    except StorageError as exc:
        # 500, not 502. The storage backend is in-process (local disk), so a
        # write failure is our fault — reporting it as "bad gateway" points the
        # next person at nginx and the workers, which are fine.
        logger.error(f"Image upload failed ({folder}): {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not store the image on the server. Please try again.",
        )


def _format_category(cat) -> dict:
    """Serialize a Category ORM row (or the dict from CategoryService.get_all)."""
    # image_url is stored as a relative path; resolve it to an absolute URL here,
    # at the edge, so clients never have to know where media happens to live.
    if isinstance(cat, dict):
        parent = cat.get("parent_category_id")
        return {
            "category_id": str(cat["category_id"]),
            "name": cat["name"],
            "slug": cat["slug"],
            "description": cat.get("description"),
            "image_url": media_url(cat.get("image_url")),
            "parent_category_id": str(parent) if parent else None,
            "is_active": cat["is_active"],
            "product_count": cat.get("product_count", 0),
        }
    return {
        "category_id": str(cat.category_id),
        "name": cat.name,
        "slug": cat.slug,
        "description": cat.description,
        "image_url": media_url(cat.image_url),
        "parent_category_id": str(cat.parent_category_id) if cat.parent_category_id else None,
        "is_active": cat.is_active,
    }


# ============================================================================
# Category Management  (/admin/categories)
# ============================================================================

@router.get("/categories", response_model=dict)
async def list_categories(
    db: AsyncSession = Depends(get_db),
):
    """List all categories (including inactive) with product counts."""
    categories = await CategoryService.get_all(db, include_inactive=True)
    return {
        "success": True,
        "data": {"categories": [_format_category(c) for c in categories]},
    }


@router.post(
    "/categories/upload-image",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
)
async def upload_category_image(
    file: UploadFile = File(..., description="Image file (JPEG/PNG/WebP/GIF)"),
):
    """
    Upload a category tile image and return its URL.

    Declared before ``/categories/{category_id}`` so that path parameter does
    not swallow "upload-image". The returned URL is passed back as ``image_url``
    when the category is created or updated, which lets the admin upload before
    the category exists.
    """
    path = await _upload_image(file, folder="categories")
    return {
        "success": True,
        "data": {"image_url": media_url(path), "filename": file.filename},
        "message": "Image uploaded successfully",
    }


@router.post("/categories", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_category(
    data: CategoryCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new category."""
    existing = await CategoryService.get_by_slug(db, data.slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category with this slug already exists",
        )

    await _validate_parent(db, data.parent_category_id)
    _validate_category_image(data.parent_category_id, data.image_url)

    # The admin posts back the absolute URL the upload endpoint handed it; store
    # only the relative path so no origin ever enters the database.
    data.image_url = media_path(data.image_url)

    try:
        category = await CategoryService.create(db, data)
        logger.info(f"[admin] Category created: {category.category_id} - {category.name}")
        return {
            "success": True,
            "data": _format_category(category),
            "message": "Category created successfully",
        }
    except Exception as e:
        logger.error(f"Error creating category: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create category",
        )


@router.put("/categories/{category_id}", response_model=dict)
async def update_category(
    category_id: str,
    data: CategoryUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update an existing category."""
    category = await CategoryService.get_by_id_or_slug(db, category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found",
        )

    if data.slug and data.slug != category.slug:
        existing = await CategoryService.get_by_slug(db, data.slug)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Category with this slug already exists",
            )

    fields = data.model_dump(exclude_unset=True)
    if "parent_category_id" in fields:
        await _validate_parent(db, data.parent_category_id, self_id=category.category_id)

    # Validate the pair the category ENDS UP with: promoting a top-level category
    # with an image into a sub-category changes only parent_category_id, and the
    # now-illegal image would otherwise survive untouched.
    next_parent = fields.get("parent_category_id", category.parent_category_id)
    next_image = fields.get("image_url", category.image_url)
    _validate_category_image(next_parent, next_image)

    # Store only the relative path (see create_category).
    if "image_url" in fields:
        data.image_url = media_path(data.image_url)

    try:
        updated = await CategoryService.update(db, category, data)
        logger.info(f"[admin] Category updated: {updated.category_id}")
        return {
            "success": True,
            "data": _format_category(updated),
            "message": "Category updated successfully",
        }
    except Exception as e:
        logger.error(f"Error updating category: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update category",
        )


@router.delete("/categories/{category_id}", response_model=dict)
async def delete_category(
    category_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete a category (blocked if it still has products)."""
    category = await CategoryService.get_by_id_or_slug(db, category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found",
        )

    if await CategoryService.has_children(db, category.category_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete a category that still has sub-categories. "
                   "Delete or move them first.",
        )

    if await CategoryService.has_products(db, category.category_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete category with products. Reassign or remove them first.",
        )

    try:
        await CategoryService.delete(db, category)
        logger.info(f"[admin] Category deleted: {category_id}")
        return {"success": True, "message": "Category deleted successfully"}
    except Exception as e:
        logger.error(f"Error deleting category: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete category",
        )


# ============================================================================
# Product Management  (/admin/products)
# ============================================================================

@router.get("/products", response_model=dict)
async def list_products(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    category_id: Optional[str] = None,
    subcategory_id: Optional[str] = None,
    is_active: Optional[bool] = Query(None, description="Filter by status; omit for all"),
    sort_by: str = Query("created_at", pattern="^(created_at|price|name|view_count)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
):
    """
    List global-catalog products with search, category filter and pagination.

    Returns products in **all** branches/states (active and inactive); this is
    the admin view, not the branch-scoped customer listing.

    Filtering by ``category_id`` includes products filed under any of that
    category's sub-categories; ``subcategory_id`` narrows to one leaf.
    """
    cat_uuid = None
    if category_id:
        try:
            cat_uuid = UUID(category_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid category ID",
            )

    subcat_uuid = None
    if subcategory_id:
        try:
            subcat_uuid = UUID(subcategory_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid sub-category ID",
            )

    offset = (page - 1) * limit
    products, total = await ProductService.get_all(
        db,
        limit=limit,
        offset=offset,
        category_id=cat_uuid,
        subcategory_id=subcat_uuid,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        include_inactive=True,
        is_active=is_active,
    )

    return {
        "success": True,
        "data": {
            "products": [format_product(p) for p in products],
            "pagination": {
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": (total + limit - 1) // limit if limit else 1,
            },
        },
    }


@router.get("/products/{product_id}", response_model=dict)
async def get_product(
    product_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get a single product by ID or slug (includes inactive)."""
    product = await ProductService.get_by_id_or_slug(db, product_id, include_inactive=True)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )
    return {"success": True, "data": format_product(product, include_variants=True)}


@router.post("/products", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_product(
    data: ProductCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new product."""
    existing = await ProductService.get_by_slug(db, data.slug, include_inactive=True)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Product with this slug already exists",
        )

    try:
        await CategoryService.validate_subcategory(db, data.category_id, data.subcategory_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    try:
        product = await ProductService.create(db, data)
        # Re-fetch with relationships loaded so format_product is safe.
        product = await ProductService.get_by_id(db, product.product_id, include_inactive=True)
        logger.info(f"[admin] Product created: {product.product_id} - {product.name}")
        return {
            "success": True,
            "data": format_product(product),
            "message": "Product created successfully",
        }
    except Exception as e:
        logger.error(f"Error creating product: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create product",
        )


@router.put("/products/{product_id}", response_model=dict)
async def update_product(
    product_id: str,
    data: ProductUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update an existing product."""
    product = await ProductService.get_by_id_or_slug(db, product_id, include_inactive=True)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    if data.slug and data.slug != product.slug:
        existing = await ProductService.get_by_slug(db, data.slug, include_inactive=True)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Product with this slug already exists",
            )

    # Validate the pair the product will *end up* with: a partial update may
    # move the category without resending subcategory_id (or vice versa), and
    # the stale half still has to satisfy the parent-child contract.
    fields = data.model_dump(exclude_unset=True)
    next_category = fields.get("category_id", product.category_id)
    next_subcategory = fields.get("subcategory_id", product.subcategory_id)
    try:
        await CategoryService.validate_subcategory(db, next_category, next_subcategory)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    try:
        updated = await ProductService.update(db, product, data)
        updated = await ProductService.get_by_id(db, updated.product_id, include_inactive=True)
        logger.info(f"[admin] Product updated: {updated.product_id}")
        return {
            "success": True,
            "data": format_product(updated),
            "message": "Product updated successfully",
        }
    except Exception as e:
        logger.error(f"Error updating product: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update product",
        )


@router.delete("/products/{product_id}", response_model=dict)
async def delete_product(
    product_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete a product."""
    product = await ProductService.get_by_id_or_slug(db, product_id, include_inactive=True)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    try:
        await ProductService.delete(db, product)
        logger.info(f"[admin] Product deleted: {product_id}")
        return {"success": True, "message": "Product deleted successfully"}
    except Exception as e:
        logger.error(f"Error deleting product: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete product",
        )


# ============================================================================
# Product Images
# ============================================================================

@router.post("/products/upload-image", response_model=dict, status_code=status.HTTP_201_CREATED)
async def upload_product_image(
    file: UploadFile = File(..., description="Image file (JPEG/PNG/WebP/GIF)"),
):
    """
    Upload a single product image and return its URL.

    This does not attach the image to any product — the client calls
    ``POST /admin/products/{id}/images`` with the returned ``image_url`` to
    link it (allowing uploads before a product exists, then linking on save).

    The client is handed the resolved absolute URL so it can preview the file
    immediately, but what it sends back to be persisted is that same value; the
    link endpoint stores the relative path. See ``_link_path``.
    """
    path = await _upload_image(file, folder="products")
    return {
        "success": True,
        "data": {"image_url": media_url(path), "filename": file.filename},
        "message": "Image uploaded successfully",
    }


@router.post("/products/{product_id}/images", response_model=dict, status_code=status.HTTP_201_CREATED)
async def add_product_image(
    product_id: str,
    data: ProductImageCreate,
    db: AsyncSession = Depends(get_db),
):
    """Link an uploaded image URL to a product."""
    product = await ProductService.get_by_id_or_slug(db, product_id, include_inactive=True)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    # The client sends back the absolute URL it was given; persist the path only.
    data.image_url = media_path(data.image_url) or data.image_url

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
            "message": "Image added successfully",
        }
    except Exception as e:
        logger.error(f"Error adding product image: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add image",
        )


@router.patch("/products/{product_id}/images/{image_id}/primary", response_model=dict)
async def set_primary_product_image(
    product_id: str,
    image_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Make an image the primary thumbnail.

    Sets ``is_primary = true`` for the target image and ``false`` for every
    other image on the product, in one atomic operation.
    """
    product = await ProductService.get_by_id_or_slug(db, product_id, include_inactive=True)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    try:
        img_uuid = UUID(image_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid image ID",
        )

    try:
        image = await ProductService.set_primary_image(db, product.product_id, img_uuid)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Image not found for this product",
        )

    return {
        "success": True,
        "data": {
            "image_id": str(image.image_id),
            "image_url": image.image_url,
            "alt_text": image.alt_text,
            "is_primary": image.is_primary,
            "sort_order": image.sort_order,
        },
        "message": "Primary image updated successfully",
    }


@router.delete("/products/{product_id}/images/{image_id}", response_model=dict)
async def remove_product_image(
    product_id: str,
    image_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Remove an image from a product."""
    try:
        await ProductService.remove_image(db, UUID(image_id))
        return {"success": True, "message": "Image removed successfully"}
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid image ID",
        )
    except Exception as e:
        logger.error(f"Error removing product image: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove image",
        )
