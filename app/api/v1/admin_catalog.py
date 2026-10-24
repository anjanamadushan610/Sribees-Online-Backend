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
from fastapi.concurrency import run_in_threadpool
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.products import format_product
from app.config.database import get_db
from app.config.redis import get_redis_optional
from app.config.settings import settings
from app.core.dependencies import (
    BranchScope,
    get_current_admin,
    inject_branch_filter,
    require_permission,
)
from app.core.media import media_path, media_url
from app.models.admin import Admin, AdminRole
from app.models.product import BranchInventory
from app.models.search_miss import SearchMissStatus
from app.schemas.category import CategoryCreate, CategoryUpdate
from app.schemas.product import (
    ProductCreate,
    ProductImageCreate,
    ProductUpdate,
    SearchMissAttachRequest,
    SearchMissStatusRequest,
    SuggestKeywordsRequest,
    SuggestSkuRequest,
)
from app.services import (
    product_imagery,
    sales_velocity_service,
    search_insights_service,
    weight_options_service,
)
from app.services.category_service import CategoryService
from app.services.embedding_service import GeminiAPIError
from app.services.keyword_suggestion_service import (
    SuggestionBudgetExceeded,
    suggest_within_budget,
)
from app.services.product_service import ProductService
from app.services.storage import StorageError, get_storage

# Any admin (or staff) with products:read can browse the catalog;
# write routes add granular create/update/delete checks per-route.
router = APIRouter(
    dependencies=[Depends(require_permission("products", "read"))],
    tags=["Admin Catalog"],
)

ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}

# A transparent cutout is re-encoded to a small WebP on the server, so it may
# arrive larger than an ordinary photo, which is stored as sent.
_CUTOUT_TYPES = {"image/png", "image/webp"}
_CUTOUT_MAX_MB = 10


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
    Images belong to top-level categories only and are required for them.

    The mobile home screen renders one tile per top-level category *from its
    image*; sub-categories appear as text beneath their parent and have nowhere
    to show one. Accepting an image on a sub-category would store a file that no
    screen can ever display, and a top-level category without an image breaks
    the mobile home screen layout.
    """
    if parent_id is not None and image_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Images are only supported on top-level categories. "
                "A sub-category inherits its parent's imagery."
            ),
        )
    if parent_id is None and (not image_url or not str(image_url).strip()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category Image is required for top-level categories.",
        )


async def _upload_image(file: UploadFile, folder: str, max_mb: Optional[int] = None) -> str:
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

    limit_mb = max_mb if max_mb is not None else settings.media_max_upload_size_mb
    contents = await file.read()
    _check_size(contents, limit_mb)
    return _store_bytes(contents, file.filename or "image.jpg", folder, file.content_type)


def _check_size(contents: bytes, limit_mb: int) -> None:
    if len(contents) > limit_mb * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Image file size ({len(contents) / 1024 / 1024:.1f}MB) exceeds the maximum allowed limit of {limit_mb}MB. Please upload an image under {limit_mb}MB."
            ),
        )


def _store_bytes(contents: bytes, filename: str, folder: str, content_type: Optional[str]) -> str:
    import io

    try:
        return get_storage().save(
            fileobj=io.BytesIO(contents),
            filename=filename,
            folder=folder,
            content_type=content_type,
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


# Per-language columns the admin console edits directly. Customer endpoints
# resolve these down to one value (see app.utils.i18n); the admin needs them
# raw so every language is editable on the same form.
_CATEGORY_TRANSLATION_FIELDS = (
    "name_si",
    "name_ta",
    "description_si",
    "description_ta",
)


def _format_category(cat) -> dict:
    """Serialize a Category ORM row (or the dict from CategoryService.get_all)."""
    # image_url is stored as a relative path; resolve it to an absolute URL here,
    # at the edge, so clients never have to know where media happens to live.
    if isinstance(cat, dict):
        parent = cat.get("parent_category_id")
        data = {
            "category_id": str(cat["category_id"]),
            "name": cat["name"],
            "slug": cat["slug"],
            "description": cat.get("description"),
            "image_url": media_url(cat.get("image_url")),
            "parent_category_id": str(parent) if parent else None,
            "is_active": cat["is_active"],
            "product_count": cat.get("product_count", 0),
        }
        data.update({f: cat.get(f) for f in _CATEGORY_TRANSLATION_FIELDS})
        return data

    data = {
        "category_id": str(cat.category_id),
        "name": cat.name,
        "slug": cat.slug,
        "description": cat.description,
        "image_url": media_url(cat.image_url),
        "parent_category_id": str(cat.parent_category_id) if cat.parent_category_id else None,
        "is_active": cat.is_active,
    }
    data.update({f: getattr(cat, f, None) for f in _CATEGORY_TRANSLATION_FIELDS})
    return data


# ============================================================================
# Category Management  (/admin/categories)
# ============================================================================

@router.get("/categories", response_model=dict)
async def list_categories(
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    List categories with product counts.
    For Super Admin (unscoped), returns all global categories.
    For Scoped Admins (Branch Managers), returns all categories with their
    branch-specific visibility status (active/inactive).
    """
    if not scope.is_unscoped and scope.branch_id:
        categories = await CategoryService.get_all(
            db, include_inactive=True, branch_id=scope.branch_id
        )
    else:
        categories = await CategoryService.get_all(db, include_inactive=True)

    return {
        "success": True,
        "data": {"categories": [_format_category(c) for c in categories]},
    }


@router.post(
    "/categories/upload-image",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("categories", "create"))],
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


@router.post("/categories", response_model=dict, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_permission("categories", "create"))])
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


@router.put("/categories/{category_id}", response_model=dict,
            dependencies=[Depends(require_permission("categories", "update"))])
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


@router.delete("/categories/{category_id}", response_model=dict,
               dependencies=[Depends(require_permission("categories", "update"))])
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

async def _get_branch_inventory_map(
    db: AsyncSession, branch_id: UUID, product_ids: list[UUID]
) -> dict[UUID, BranchInventory]:
    if not product_ids:
        return {}
    rows = (
        await db.execute(
            select(BranchInventory).where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.product_id.in_(product_ids),
            )
        )
    ).scalars().all()
    return {row.product_id: row for row in rows}


async def _ensure_weight_options(db: AsyncSession, product) -> None:
    """
    A product just switched to sold-by-weight starts with the default options
    (250 g ... 5 kg) priced pro rata from its price, read as the price per kg.
    Options it already has are left alone, so switching off and on again keeps
    whatever the super admin set.
    """
    if not product.sold_by_weight:
        return
    if await weight_options_service.options_for(db, [product.product_id], None):
        return
    await weight_options_service.replace_options(
        db, product, weight_options_service.default_options(product.price or 0)
    )
    await db.commit()


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
    movement: Optional[str] = Query(
        None,
        pattern="^(fast|steady|slow|none)$",
        description=(
            "Branch-scoped sales velocity over the last 30 days. Slow-moving "
            "stock is what Quick Sale exists to clear."
        ),
    ),
    stock_state: Optional[str] = Query(
        None, pattern="^(in|low|out)$", description="Branch stock level band"
    ),
    on_quick_sale: Optional[bool] = Query(
        None, description="Only products currently on (or off) Quick Sale in this branch"
    ),
    perishable_only: bool = Query(False, description="Only short-shelf-life stock"),
    price_review_level: Optional[str] = Query(
        None,
        pattern="^(daily|weekly|monthly|long)$",
        description="Price re-check cadence (Sribees Price app tabs)",
    ),
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    List global-catalog products with search, category filter and pagination.

    For Super Admin, returns master products with global is_active status.
    For Scoped Admins (Branch Managers/Staff), returns products overlayed with
    their branch-specific is_active status.
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
        price_review_level=price_review_level,
    )

    if not scope.is_unscoped and scope.branch_id:
        p_ids = [p.product_id for p in products]
        inv_map = await _get_branch_inventory_map(db, scope.branch_id, p_ids)
        # One grouped query for the page's 30-day sales, not one per row.
        sold_map = await sales_velocity_service.units_sold(db, scope.branch_id, p_ids)
        formatted_products = []
        for p in products:
            inv = inv_map.get(p.product_id)
            # Overlay the FULL branch-effective data, not just is_active.
            # Price now lives on branch_inventory (the global catalog entry
            # carries no price at all for products added after that change),
            # so overlaying only the status made every such product read
            # LKR 0.00 on this page while the branch was in fact selling it.
            eff = ProductService.compute_effective_data(p, inv)
            if inv is None:
                # Not stocked here at all: compute_effective_data falls back to
                # the global row's is_active, but a product this branch has
                # never added is not active on its storefront.
                eff["is_active"] = False

            row = format_product(p, branch_overrides=eff)

            # Branch operations: how much is on the shelf, how fast it is
            # leaving, and whether it is already being cleared. Together these
            # answer the only question this page is really asked — "what should
            # I put on Quick Sale?" — which no combination of the global
            # catalog fields could.
            units = sold_map.get(p.product_id, 0)
            qty = int(eff.get("stock_quantity") or 0)
            low_threshold = p.low_stock_threshold or 10
            row["units_sold_30d"] = units
            row["movement"] = sales_velocity_service.classify(units)
            row["stock_state"] = sales_velocity_service.stock_state(qty, low_threshold)
            row["is_on_quick_sale"] = bool(eff.get("is_on_sale"))
            formatted_products.append(row)
    else:
        formatted_products = [format_product(p) for p in products]

    # Movement, stock band and Quick-Sale state are all derived per branch, so
    # they cannot be SQL predicates without duplicating the banding rules in
    # two languages. Applied to the page instead; `limit` caps it at 100 rows.
    #
    # The honest cost: these narrow the CURRENT PAGE, so a filtered view can
    # show fewer than `limit` rows while `total` still reports the unfiltered
    # count. The alternative — silently paginating over a filtered set whose
    # size the query does not know — would misreport the total instead, which
    # is worse.
    if movement:
        formatted_products = [r for r in formatted_products if r.get("movement") == movement]
    if stock_state:
        formatted_products = [
            r for r in formatted_products if r.get("stock_state") == stock_state
        ]
    if on_quick_sale is not None:
        formatted_products = [
            r for r in formatted_products if bool(r.get("is_on_quick_sale")) is on_quick_sale
        ]
    if perishable_only:
        formatted_products = [r for r in formatted_products if r.get("is_perishable")]

    return {
        "success": True,
        "data": {
            "products": formatted_products,
            "pagination": {
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": (total + limit - 1) // limit if limit else 1,
            },
        },
    }


@router.get("/products/search-misses", response_model=dict)
async def list_search_misses(
    status_filter: str = Query(SearchMissStatus.OPEN, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """
    Customer searches that found nothing, busiest first: the inbox for
    teaching search the words shoppers use (search_insights_service).

    Registered before /products/{product_id} so "search-misses" is never
    parsed as a product id.
    """
    if status_filter not in SearchMissStatus.ALL:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown status")
    items = await search_insights_service.list_misses(db, status_filter, limit)
    return {"success": True, "data": {"items": items}}


@router.get("/products/{product_id}", response_model=dict)
async def get_product(
    product_id: str,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Get a single product by ID or slug (includes inactive)."""
    product = await ProductService.get_by_id_or_slug(db, product_id, include_inactive=True)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    eff = None
    if not scope.is_unscoped and scope.branch_id:
        inv_map = await _get_branch_inventory_map(db, scope.branch_id, [product.product_id])
        inv = inv_map.get(product.product_id)
        # Same full overlay as the list endpoint — see the comment there.
        eff = ProductService.compute_effective_data(product, inv)
        if inv is None:
            eff["is_active"] = False

    weight_options = None
    if product.sold_by_weight:
        weight_options = (await weight_options_service.options_for(
            db, [product.product_id], scope.branch_id if not scope.is_unscoped else None
        )).get(str(product.product_id), [])
    data = format_product(
        product, include_variants=True, include_translations=True, branch_overrides=eff,
        weight_options=weight_options,
    )
    # Admin-only: the public product payload never carries commission rates.
    # Edited through /admin/referral-commission/products/{id}.
    rate = product.referral_commission_percentage
    data["referral_commission_percentage"] = float(rate) if rate is not None else None
    return {"success": True, "data": data}


@router.post("/products/suggest-keywords", response_model=dict,
             dependencies=[Depends(require_permission("products", "update"))])
async def suggest_product_keywords(
    data: SuggestKeywordsRequest,
    redis=Depends(get_redis_optional),
):
    """
    AI-assisted search keyword suggestions (Sinhala/Tamil/Singlish/Tamilish).

    Gated on products:update, not the router's products:read: every call is
    billed, and only someone who can save the keywords has a reason to ask.
    Capped per day by ``gemini_keyword_daily_limit``, with repeats served
    from cache for free; see keyword_suggestion_service.suggest_within_budget.

    Takes draft form fields directly rather than a product id, so it works
    while creating a new product too, not just editing a saved one. One-off
    admin action, never called from the customer search path (see
    ``catalog_search_service``), so this never adds AI latency or
    cost to a live search request. The admin reviews/edits the suggestion
    before it's saved.
    """
    try:
        result = await suggest_within_budget(
            redis,
            name=data.name,
            description=data.description or data.short_description,
            category=data.category,
        )
    except SuggestionBudgetExceeded as exc:
        detail = (
            "AI keyword suggestions are turned off."
            if exc.limit <= 0
            else f"Today's {exc.limit} AI keyword suggestions are used up. "
            "Type the keywords by hand, or try again tomorrow."
        )
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)
    except GeminiAPIError as exc:
        detail = (
            "AI keyword suggestions aren't configured yet. Add GEMINI_API_KEY "
            "to the backend .env."
            if "GEMINI_API_KEY not configured" in str(exc)
            else "Could not get AI keyword suggestions right now. Please try again."
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)

    return {
        "success": True,
        "data": {
            "suggested_keywords": result["keywords"],
            "cached": result["cached"],
            "remaining_today": result["remaining_today"],
        },
    }


@router.post("/products/search-misses/attach", response_model=dict)
async def attach_search_miss(
    data: SearchMissAttachRequest,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(require_permission("products", "update")),
):
    """Add a missed search to a product's keywords, as the shopper typed it."""
    try:
        miss = await search_insights_service.attach(
            db, data.query_key, data.product_id, admin.admin_id
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc).capitalize())
    logger.info(
        f"Search miss {miss.query_key!r} attached to product {data.product_id} "
        f"by admin {admin.admin_id}"
    )
    return {"success": True, "data": {"query_key": miss.query_key, "status": miss.status}}


@router.post("/products/search-misses/status", response_model=dict,
             dependencies=[Depends(require_permission("products", "update"))])
async def set_search_miss_status(
    data: SearchMissStatusRequest,
    db: AsyncSession = Depends(get_db),
):
    """Ignore a missed search (noise, or something we don't sell), or reopen it."""
    try:
        miss = await search_insights_service.set_status(db, data.query_key, data.status)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc).capitalize())
    return {"success": True, "data": {"query_key": miss.query_key, "status": miss.status}}


@router.post("/products/suggest-sku", response_model=dict)
async def suggest_product_sku(
    data: SuggestSkuRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Generate a unique SKU based on the product name and category.
    """
    import re

    category_prefix = "UNCAT"
    if data.category_id:
        category = await CategoryService.get_by_id(db, data.category_id)
        if category and category.name:
            clean_cat = re.sub(r"[^A-Z0-9]", "", category.name.upper())
            if clean_cat:
                category_prefix = clean_cat[:4]

    name_prefix = "PROD"
    if data.name:
        clean_name = re.sub(r"[^A-Z0-9]", "", data.name.upper())
        if clean_name:
            name_prefix = clean_name[:4]

    base_sku = f"{category_prefix}-{name_prefix}"

    counter = 1
    while True:
        sku = f"{base_sku}-{counter:03d}"
        existing = await ProductService.get_by_sku(db, sku)
        if not existing:
            break
        counter += 1

    return {"success": True, "data": {"sku": sku}}


@router.post("/products", response_model=dict, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_permission("products", "create"))])
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

    if data.sku and data.sku.strip():
        existing_sku = await ProductService.get_by_sku(db, data.sku.strip())
        if existing_sku:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Product with this SKU already exists",
            )

    try:
        await CategoryService.validate_subcategory(db, data.category_id, data.subcategory_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    if not data.images or len(data.images) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one product image (thumbnail) is required",
        )

    if data.weight is None or data.weight <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Product weight is required and must be greater than 0",
        )

    if not data.weight_unit or not str(data.weight_unit).strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Product weight unit is required",
        )

    # Normalize image URLs to storage paths
    for img in data.images:
        img.image_url = media_path(img.image_url) or img.image_url

    try:
        product = await ProductService.create(db, data)
        await _ensure_weight_options(db, product)
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


@router.put("/products/{product_id}", response_model=dict,
            dependencies=[Depends(require_permission("products", "update"))])
async def update_product(
    product_id: str,
    data: ProductUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Update an existing product."""
    product = await ProductService.get_by_id_or_slug(db, product_id, include_inactive=True)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    if data.weight is not None and data.weight <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Product weight must be greater than 0",
        )

    # Branch Manager / Scoped Admin active status toggle isolation:
    # Non-super admins can only toggle branch-level visibility (BranchInventory.is_active)
    # for their branch. Global catalog fields (name, price, SKU, category, etc.)
    # can only be mutated by Super Admin.
    if current_admin.role != AdminRole.SUPER_ADMIN:
        if scope.branch_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Your account is not assigned to a branch.",
            )
        if data.is_active is not None:
            await ProductService.upsert_branch_override(
                db,
                product_id=product.product_id,
                branch_id=scope.branch_id,
                is_active=data.is_active,
            )
        inv_map = await _get_branch_inventory_map(db, scope.branch_id, [product.product_id])
        inv = inv_map.get(product.product_id)
        eff = ProductService.compute_effective_data(product, inv)
        if inv is None:
            eff["is_active"] = False
        return {
            "success": True,
            "data": format_product(
                product, include_variants=True, include_translations=True, branch_overrides=eff
            ),
            "message": "Branch product visibility updated successfully",
        }

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
    if next_category is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category is required — a product cannot be left uncategorised.",
        )
    try:
        await CategoryService.validate_subcategory(db, next_category, next_subcategory)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    # Same "what does the update leave behind" check for SKU: a partial update
    # that doesn't touch it is fine, but explicitly clearing it is not.
    next_sku = fields.get("sku", product.sku)
    if not next_sku or not str(next_sku).strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SKU is required.",
        )

    if next_sku and str(next_sku).strip():
        existing_sku = await ProductService.get_by_sku(db, str(next_sku).strip())
        if existing_sku and existing_sku.product_id != product.product_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Product with this SKU already exists",
            )

    try:
        updated = await ProductService.update(db, product, data)
        await _ensure_weight_options(db, updated)
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


@router.delete("/products/{product_id}", response_model=dict,
               dependencies=[Depends(require_permission("products", "delete"))])
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

@router.post("/products/upload-image", response_model=dict, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_permission("products", "create"))])
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

    A PNG or WebP with a transparent background is a product cutout: it is not
    stored as sent but laid out on the standard white square (see
    ``product_imagery``), so every product in the app sits the same way. Any
    other picture is stored exactly as uploaded.
    """
    if file.content_type in _CUTOUT_TYPES:
        contents = await file.read()
        _check_size(contents, _CUTOUT_MAX_MB)
        try:
            picture = await run_in_threadpool(product_imagery.normalize_cutout, contents)
        except product_imagery.ImageryError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        if picture is not None:
            stem = (file.filename or "product").rsplit(".", 1)[0]
            path = _store_bytes(picture, f"{stem}.webp", "products", "image/webp")
            return {
                "success": True,
                "data": {"image_url": media_url(path), "filename": file.filename},
                "message": "Image uploaded successfully",
            }
        await file.seek(0)

    path = await _upload_image(file, folder="products", max_mb=1)
    return {
        "success": True,
        "data": {"image_url": media_url(path), "filename": file.filename},
        "message": "Image uploaded successfully",
    }


@router.post("/products/{product_id}/images", response_model=dict, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_permission("products", "update"))])
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


@router.patch("/products/{product_id}/images/{image_id}/primary", response_model=dict,
              dependencies=[Depends(require_permission("products", "update"))])
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


@router.delete("/products/{product_id}/images/{image_id}", response_model=dict,
               dependencies=[Depends(require_permission("products", "update"))])
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
