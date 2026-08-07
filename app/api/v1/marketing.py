"""
SRIBEESonline - Marketing Manager API Endpoints

Branch-scoped endpoints for managing product discounts and the Quick Sale feed.
Uses the ``branch_inventory`` table for per-branch overrides; global values on
``products`` act as fallbacks when no override is set.

  GET   /products                — list this branch's products (the picker)
  PATCH /inventory/{product_id}  — set/clear branch overrides
  GET   /quick-sale              — preview Quick Sale products for manager's branch
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import require_roles
from app.schemas.product import BranchInventoryUpdateRequest
from app.services.product_service import ProductService

router = APIRouter()

RequireMarketingManager = Depends(
    require_roles("super_admin", "branch_manager", "marketing_manager")
)


# ============================================================================
# Helpers
# ============================================================================

def _resolve_branch_id(admin, branch_id: Optional[UUID] = None) -> UUID:
    """
    Resolve which branch a marketing action applies to.

    Scoped admins (branch/marketing managers) always operate on their own
    assigned branch. Super Admins have no default branch, so they must pass
    ``?branch_id=`` explicitly.
    """
    from app.models.admin import AdminRole

    is_super = admin.role in (AdminRole.SUPER_ADMIN.value, "super_admin", AdminRole.SUPER_ADMIN)

    if is_super:
        if branch_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "success": False,
                    "error": {
                        "message": "Super Admins must specify a branch_id query parameter",
                        "code": "NO_BRANCH_ASSIGNED",
                    },
                },
            )
        return branch_id

    if admin.branch_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "error": {
                    "message": "Your admin account is not assigned to a branch",
                    "code": "NO_BRANCH_ASSIGNED",
                },
            },
        )
    return admin.branch_id


def _format_marketing_product(inv, product) -> dict:
    """Format a branch_inventory row for the Marketing Manager's product picker."""
    effective = ProductService.compute_effective_data(product, inv)
    return {
        "inventoryId": str(inv.inventory_id),
        "productId": str(product.product_id),
        "name": product.name,
        "sku": product.sku,
        "globalPrice": float(product.price) if product.price is not None else 0.0,
        "branchPrice": float(inv.branch_price) if inv.branch_price is not None else None,
        "effectivePrice": effective["effective_price"],
        "discountPercentage": inv.discount_percentage,
        "globalDiscountPercentage": product.discount_percentage,
        "effectiveDiscount": effective["effective_discount"],
        "isOnSale": inv.is_on_sale,
        "isActive": inv.is_active,
        "stockQuantity": inv.stock_quantity or 0,
    }


def _format_quick_sale_item(entry: dict) -> dict:
    """Format a quick-sale row (product + effective data) for the response."""
    p = entry["product"]
    eff = entry["effective"]
    return {
        "productId": str(p.product_id),
        "name": p.name,
        "slug": p.slug,
        "globalPrice": float(p.price) if p.price else 0,
        "effectivePrice": eff["effective_price"],
        "branchPrice": eff["branch_price"],
        "effectiveDiscount": eff["effective_discount"],
        "effectiveDiscountPrice": eff["effective_discount_price"],
        "stockQuantity": eff["stock_quantity"],
        "isOnSale": eff["is_on_sale"],
        "isActive": eff["is_active"],
        "category": (
            {"categoryId": str(p.category.category_id), "name": p.category.name}
            if p.category else None
        ),
        "images": [
            {"imageUrl": img.image_url, "isPrimary": img.is_primary}
            for img in (p.images or [])[:1]
        ],
    }


# ============================================================================
# GET /products — the branch's products (picker for the PATCH below)
# ============================================================================

@router.get(
    "/products",
    response_model=dict,
    summary="List this branch's products for Quick Sale management",
    description=(
        "Paginated, searchable list of products already stocked in the "
        "manager's branch, with current discount/Quick-Sale status. This is "
        "the picker that feeds the PATCH /inventory/{product_id} action below."
    ),
)
async def list_marketing_products(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None, description="Match product name or SKU"),
    branch_id: Optional[UUID] = Query(
        None, description="Super Admin only: which branch to list"
    ),
    db: AsyncSession = Depends(get_db),
    admin=RequireMarketingManager,
):
    resolved_branch = _resolve_branch_id(admin, branch_id)
    offset = (page - 1) * limit

    rows, total = await ProductService.list_inventory(
        db,
        branch_id=resolved_branch,
        limit=limit,
        offset=offset,
        search=search,
    )

    return {
        "success": True,
        "data": {
            "products": [_format_marketing_product(inv, product) for inv, product, _branch in rows],
            "pagination": {
                "total": total,
                "page": page,
                "limit": limit,
                "totalPages": (total + limit - 1) // limit if limit else 1,
            },
            "branchId": str(resolved_branch),
        },
    }


# ============================================================================
# PATCH /inventory/{product_id}
# ============================================================================

@router.patch(
    "/inventory/{product_id}",
    response_model=dict,
    summary="Update branch-specific inventory overrides",
    description=(
        "Marketing Managers update **branch_price**, **discount_percentage**, "
        "**is_on_sale**, **is_active**, and **stock_quantity** for their "
        "assigned branch.  Setting a field to ``null`` clears the override "
        "and the system falls back to the global value on the product."
    ),
)
async def update_branch_inventory(
    product_id: UUID,
    body: BranchInventoryUpdateRequest,
    branch_id: Optional[UUID] = Query(
        None, description="Super Admin only: which branch to update"
    ),
    db: AsyncSession = Depends(get_db),
    admin=RequireMarketingManager,
):
    branch_id = _resolve_branch_id(admin, branch_id)

    update_data = body.model_dump(exclude_unset=True)
    inv = await ProductService.update_branch_inventory(
        db,
        product_id=product_id,
        branch_id=branch_id,
        **update_data,
    )

    # Load product for effective computation
    product = await ProductService.get_by_id(db, product_id, include_inactive=True)
    eff = ProductService.compute_effective_data(product, inv)

    return {
        "success": True,
        "data": {
            "inventoryId": str(inv.inventory_id),
            "productId": str(product_id),
            "branchId": str(branch_id),
            "productName": product.name,
            **eff,
        },
        "message": "Branch inventory updated — overrides applied",
    }


# ============================================================================
# GET /quick-sale
# ============================================================================

@router.get(
    "/quick-sale",
    response_model=dict,
    summary="Preview Quick Sale products for your branch",
    description=(
        "Returns the Quick Sale product feed for the manager's branch, "
        "sorted by highest effective discount first.  Uses COALESCE "
        "fallback (branch override → global default)."
    ),
)
async def preview_quick_sale(
    limit: int = Query(20, ge=1, le=100),
    branch_id: Optional[UUID] = Query(
        None, description="Super Admin only: which branch to preview"
    ),
    db: AsyncSession = Depends(get_db),
    admin=RequireMarketingManager,
):
    branch_id = _resolve_branch_id(admin, branch_id)

    items = await ProductService.get_quick_sale_products(db, branch_id, limit)

    return {
        "success": True,
        "data": {
            "products": [_format_quick_sale_item(item) for item in items],
            "total": len(items),
            "branchId": str(branch_id),
        },
    }
