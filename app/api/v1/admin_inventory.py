"""
Admin Branch Inventory API (Module 7.5)

Branch-scoped stock management over ``branch_inventory`` rows.

RBAC: super_admin, branch_manager, inventory_manager (enforced at router level).
Branch isolation: every route depends on ``inject_branch_filter`` — Branch
Managers and Inventory Managers only ever see/modify their own branch's rows,
while Super Admins see all branches (optionally narrowed by ``?branch_id=``).

Prefix "/admin/inventory" is applied by app/api/v1/router.py.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import BranchScope, inject_branch_filter, require_roles
from app.models.branch import Branch
from app.models.product import BranchInventory, Product
from app.schemas.inventory import BranchOverrideCreate, InventoryUpdateRequest
from app.services.product_service import ProductService

# Stock management is limited to Super Admins, Branch Managers and Inventory Managers.
router = APIRouter(
    dependencies=[Depends(require_roles("super_admin", "branch_manager", "inventory_manager"))],
    tags=["Admin Inventory"],
)


def _format_inventory_row(inv: BranchInventory, product: Product, branch: Branch) -> dict:
    """
    Serialize a branch_inventory row joined with its product and branch.

    Exposes the override and the global value side by side so the dashboard can
    show a Branch Manager exactly what they are changing and what the product
    falls back to. ``effective_*`` is what a customer in this branch actually
    sees, and is computed by the same merge the storefront uses.
    """
    stock = inv.stock_quantity or 0
    reserved = inv.reserved_quantity or 0
    threshold = inv.low_stock_threshold or 0
    effective = ProductService.compute_effective_data(product, inv)

    return {
        "inventory_id": str(inv.inventory_id),
        "product_id": str(product.product_id),
        "product_name": product.name,
        "sku": product.sku,
        "branch_id": str(branch.branch_id),
        "branch_name": branch.name,
        # Pricing: the local override (may be null), the global fallback, and
        # the merged result the customer is charged.
        "branch_price": float(inv.branch_price) if inv.branch_price is not None else None,
        "global_price": float(product.price) if product.price is not None else 0.0,
        "effective_price": effective["effective_price"],
        "discount_percentage": inv.discount_percentage,
        "global_discount_percentage": product.discount_percentage,
        "effective_discount": effective["effective_discount"],
        "is_on_sale": inv.is_on_sale,
        # Stock
        "stock_quantity": stock,
        "reserved_quantity": reserved,
        "available_quantity": stock - reserved,
        "low_stock_threshold": threshold,
        "is_low_stock": stock <= threshold,
        "is_active": inv.is_active,
    }


@router.get("", response_model=dict)
async def list_inventory(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None, description="Match product name or SKU"),
    low_stock_only: bool = Query(False, description="Only rows at/below threshold"),
    branch_id: Optional[UUID] = Query(
        None,
        description="Super Admin only: narrow to a single branch (ignored for scoped admins)",
    ),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Paginated branch inventory list.

    Branch isolation is applied via ``scope.resolve()``: scoped admins always
    get their own branch; Super Admins get all branches unless they pass
    ``branch_id``.
    """
    effective_branch = scope.resolve(branch_id)
    offset = (page - 1) * limit

    rows, total = await ProductService.list_inventory(
        db,
        branch_id=effective_branch,
        limit=limit,
        offset=offset,
        search=search,
        low_stock_only=low_stock_only,
    )

    return {
        "success": True,
        "data": {
            "items": [_format_inventory_row(inv, product, branch) for inv, product, branch in rows],
            "pagination": {
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": (total + limit - 1) // limit if limit else 1,
            },
            # Signals to the frontend whether to show the Branch column.
            "scope": {
                "is_super_admin": scope.is_super_admin,
                "branch_id": str(effective_branch) if effective_branch else None,
            },
        },
    }


@router.get("/catalog", response_model=dict)
async def list_unstocked_catalog(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None, description="Match product name or SKU"),
    branch_id: Optional[UUID] = Query(
        None,
        description="Super Admin only: which branch to stock into",
    ),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Global-catalog products this branch does not carry yet.

    Backs the "Add product to branch" picker: these are the products a Branch
    Manager can pull in and price locally. Products already stocked are omitted
    — those are edited through ``PUT /{inventory_id}`` instead.
    """
    effective_branch = scope.resolve(branch_id)
    if effective_branch is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Specify ?branch_id= to list products available to stock.",
        )

    offset = (page - 1) * limit
    products, total = await ProductService.list_unstocked_products(
        db,
        branch_id=effective_branch,
        limit=limit,
        offset=offset,
        search=search,
    )

    return {
        "success": True,
        "data": {
            "products": [
                {
                    "product_id": str(p.product_id),
                    "name": p.name,
                    "sku": p.sku,
                    "global_price": float(p.price) if p.price is not None else 0.0,
                    "global_stock_quantity": p.stock_quantity or 0,
                    "category_name": p.category.name if p.category else None,
                }
                for p in products
            ],
            "pagination": {
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": (total + limit - 1) // limit if limit else 1,
            },
            "branch_id": str(effective_branch),
        },
    }


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def stock_product_in_branch(
    body: BranchOverrideCreate,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Stock a global-catalog product in a branch, with optional local overrides.

    This is the write that makes a product *exist* for that branch's customers:
    the public listing joins on ``branch_inventory``, so until this row is
    created the product is invisible to shoppers there.

    Omitting ``branch_price`` inherits the global price — the merge treats NULL
    as "fall back to the product". Idempotent per (product, branch): calling it
    again updates the existing override rather than failing.
    """
    effective_branch = scope.resolve(body.branch_id)
    if effective_branch is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="branch_id is required when stocking a product as Super Admin.",
        )

    product = await ProductService.get_by_id(db, body.product_id, include_inactive=True)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found in the global catalog.",
        )

    overrides = body.model_dump(exclude={"product_id", "branch_id"})
    inv, created = await ProductService.upsert_branch_override(
        db,
        product_id=body.product_id,
        branch_id=effective_branch,
        **overrides,
    )

    found = await ProductService.get_inventory_by_id(db, inv.inventory_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load the saved inventory row.",
        )

    logger.info(
        f"[admin] Product {body.product_id} "
        f"{'stocked in' if created else 'override updated for'} branch {effective_branch}"
    )
    return {
        "success": True,
        "data": _format_inventory_row(*found),
        "message": (
            "Product stocked in branch successfully"
            if created
            else "Branch override updated successfully"
        ),
    }


@router.put("/{inventory_id}", response_model=dict)
async def update_inventory(
    inventory_id: UUID,
    body: InventoryUpdateRequest,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Update the local overrides on a single branch_inventory row — price,
    discount, stock, threshold and branch visibility.

    Scoped admins may only touch rows in their own branch. Sending
    ``branch_price: null`` clears the local price so the product falls back to
    the global catalog price.
    """
    found = await ProductService.get_inventory_by_id(db, inventory_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inventory item not found",
        )

    inv, product, branch = found

    # Branch isolation: a scoped admin cannot modify another branch's stock.
    if not scope.is_super_admin and inv.branch_id != scope.branch_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only modify inventory for your own branch.",
        )

    updated = await ProductService.update_inventory_row(
        db,
        inv,
        **body.model_dump(exclude_unset=True),
    )
    logger.info(f"[admin] Inventory {inventory_id} updated in branch {inv.branch_id}")

    return {
        "success": True,
        "data": _format_inventory_row(updated, product, branch),
        "message": "Inventory updated successfully",
    }
