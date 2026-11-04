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
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.dependencies import BranchScope, inject_branch_filter, require_permission
from app.models.branch import Branch
from app.models.finance_outbox import FinanceMinPrice
from app.models.product import (
    BranchInventory,
    BranchVariantInventory,
    Product,
    ProductVariant,
)
from app.schemas.inventory import BranchOverrideCreate, InventoryUpdateRequest
from app.services import branch_cashback
from app.services.product_service import ProductService
from app.services.referral_commission.rate_admin import describe_rates

# Allow any admin (or staff) holding inventory:read; writes additionally need inventory:update.
router = APIRouter(
    dependencies=[Depends(require_permission("inventory", "read"))],
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
        "cashback_percentage": inv.cashback_percentage,
        "global_cashback_percentage": product.cashback_percentage,
        "effective_cashback": effective["effective_cashback"],
        # Referral commission: edited only through /admin/referral-commission
        # (role-gated and capped), shown here so the table can display it.
        **describe_rates(product, inv),
        "is_on_sale": inv.is_on_sale,
        # Stock
        "stock_quantity": stock,
        # How the stock is kept (migration 100). For loose goods stock_quantity shows packs of
        # the smallest weight and stock_grams is the figure; packed sizes are on /weights.
        "stock_mode": product.stock_mode or "unit",
        "stock_grams": inv.stock_grams or 0,
        "reserved_quantity": reserved,
        "available_quantity": max(0, stock - reserved),
        "low_stock_threshold": threshold,
        "is_low_stock": stock <= threshold,
        "is_active": inv.is_active,
        "category_id": str(product.category_id) if product.category_id else None,
        "subcategory_id": str(product.subcategory_id) if product.subcategory_id else None,
        "price_review_level": product.price_review_level,
    }


@router.get("", response_model=dict)
async def list_inventory(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None, description="Match product name, SKU or Sinhala name"),
    category_id: Optional[UUID] = Query(None, description="Filter by top-level category"),
    subcategory_id: Optional[UUID] = Query(None, description="Filter by subcategory"),
    price_review_level: Optional[str] = Query(
        None,
        pattern="^(daily|weekly|monthly|long)$",
        description="Filter by price review cycle",
    ),
    stock_state: Optional[str] = Query(
        None,
        pattern="^(all|in|low|out)$",
        description="Filter by stock state (all/in/low/out)",
    ),
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
        category_id=category_id,
        subcategory_id=subcategory_id,
        price_review_level=price_review_level,
        stock_state=stock_state,
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
                **branch_cashback.describe(
                    scope.role, await branch_cashback.branch_max_percent(db)
                ),
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


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_permission("inventory", "update"))])
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
    existing = (
        await db.execute(
            select(BranchInventory.cashback_percentage).where(
                BranchInventory.product_id == body.product_id,
                BranchInventory.branch_id == effective_branch,
            )
        )
    ).first()
    if existing:
        # Already stocked: write only what the caller sent. The schema's
        # defaults (no discount, no cashback, hidden, threshold 10) are for a
        # new row -- applied to a stocked one they wiped the branch's settings
        # each time the Price app changed a price.
        overrides = {k: v for k, v in overrides.items() if k in body.model_fields_set}
    await branch_cashback.guard(
        db, scope.role, overrides, current=existing[0] if existing else None
    )
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


class CashbackUpdate(BaseModel):
    cashback_percentage: Optional[float] = Field(
        ..., ge=0, le=100, description="This branch's cashback %. Null falls back to the product's."
    )
    branch_id: Optional[UUID] = Field(None, description="Super Admin only; others use their own branch.")


async def _cashback_view(db: AsyncSession, scope: BranchScope, product_id: UUID, branch_id: Optional[UUID]):
    """One product's cashback in one branch, for the Sribees Price app."""
    effective_branch = scope.resolve(branch_id)
    if effective_branch is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Specify branch_id.")
    row = (
        await db.execute(
            select(BranchInventory, Product)
            .join(Product, Product.product_id == BranchInventory.product_id)
            .where(BranchInventory.product_id == product_id, BranchInventory.branch_id == effective_branch)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This branch does not stock the product.")
    return row[0], row[1]


def _cashback_data(inv: BranchInventory, product: Product, scope: BranchScope, cap) -> dict:
    return {
        "product_id": str(product.product_id),
        "branch_id": str(inv.branch_id),
        "cashback_percentage": inv.cashback_percentage,
        "global_cashback_percentage": product.cashback_percentage,
        "effective_cashback": ProductService.compute_effective_data(product, inv)["effective_cashback"],
        **branch_cashback.describe(scope.role, cap),
    }


@router.get("/cashback/{product_id}", response_model=dict)
async def get_branch_cashback(
    product_id: UUID,
    branch_id: Optional[UUID] = Query(None, description="Super Admin only"),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """A product's cashback in a branch: the override, the product's rate, and what customers earn."""
    inv, product = await _cashback_view(db, scope, product_id, branch_id)
    cap = await branch_cashback.branch_max_percent(db)
    return {"success": True, "data": _cashback_data(inv, product, scope, cap)}


@router.put("/cashback/{product_id}", response_model=dict)
async def set_branch_cashback(
    product_id: UUID,
    body: CashbackUpdate,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Set or clear a branch's cashback (Branch Manager / Super Admin, capped)."""
    inv, product = await _cashback_view(db, scope, product_id, body.branch_id)
    fields = {"cashback_percentage": body.cashback_percentage}
    await branch_cashback.guard(db, scope.role, fields, current=inv.cashback_percentage)
    inv = await ProductService.update_inventory_row(db, inv, **fields)
    logger.info(f"[admin] cashback {body.cashback_percentage} for product {product_id} branch {inv.branch_id}")
    cap = await branch_cashback.branch_max_percent(db)
    return {"success": True, "data": _cashback_data(inv, product, scope, cap)}


def _floor_row(mp: FinanceMinPrice) -> dict:
    def s(v):
        return None if v is None else f"{v:.2f}"
    return {
        "product_id": str(mp.product_id),
        "variant_id": str(mp.variant_id) if mp.variant_id else None,
        "min_price": s(mp.min_price), "ready_cost": s(mp.ready_cost),
        "purchase_cost": s(mp.purchase_cost), "materials_cost": s(mp.materials_cost),
        "margin_pct": s(mp.margin_pct), "cashback_pct": s(mp.cashback_pct),
        "referral_pct": s(mp.referral_pct), "cost_ref": mp.cost_ref,
        "no_floor_reason": mp.no_floor_reason,
        "as_of": mp.received_at.isoformat() if mp.received_at else None,
    }


@router.get("/price-floors", response_model=dict)
async def get_price_floors(
    product_ids: str = Query(..., max_length=20000,
                             description="Comma-separated product ids (at most 200)"),
    branch_id: Optional[UUID] = Query(None, description="Super Admin only"),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Sribees Finance's minimum price for these products (and their pack sizes) in a
    branch, for the quiet icon by a price that is below it. Advice only: a price below
    the floor can still be saved (a promotion). A product Finance has no floor for is
    simply absent."""
    effective_branch = scope.resolve(branch_id)
    if effective_branch is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Specify branch_id.")
    try:
        ids = [UUID(x.strip()) for x in product_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad product id.")
    if not ids or len(ids) > 200:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Ask for 1 to 200 products.")
    rows = (await db.execute(select(FinanceMinPrice).where(
        FinanceMinPrice.branch_id == effective_branch,
        FinanceMinPrice.product_id.in_(ids)))).scalars().all()
    out: dict = {}
    for mp in rows:
        out.setdefault(str(mp.product_id), []).append(_floor_row(mp))
    return {"success": True, "data": {"branch_id": str(effective_branch), "floors": out}}


@router.put("/{inventory_id}", response_model=dict,
            dependencies=[Depends(require_permission("inventory", "update"))])
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

    # The Quick Edit form always sends the stock figure; only a change of it is refused.
    if body.stock_quantity is not None and body.stock_quantity != (inv.stock_quantity or 0):
        if settings.finance_stock_authority:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="Stock is kept by Sribees Finance; count it there.")
        if (product.stock_mode or "unit") != "unit":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="This product is kept by weight; set its stock under Weights.")

    # Validation: stock_quantity cannot be less than reserved_quantity
    new_stock = body.stock_quantity if body.stock_quantity is not None else inv.stock_quantity
    new_reserved = inv.reserved_quantity  # Reserved quantity is system-managed and read-only
    if new_stock is not None and new_stock < new_reserved:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot set stock below reserved count of {new_reserved}. Resolve pending orders first.",
        )

    fields = body.model_dump(exclude_unset=True, exclude={"reserved_quantity"})
    await branch_cashback.guard(db, scope.role, fields, current=inv.cashback_percentage)
    updated = await ProductService.update_inventory_row(db, inv, **fields)
    logger.info(f"[admin] Inventory {inventory_id} updated in branch {inv.branch_id}")

    return {
        "success": True,
        "data": _format_inventory_row(updated, product, branch),
        "message": "Inventory updated successfully",
    }


# ---------------------------------------------------------------- weight stock (migration 100)
class PackCount(BaseModel):
    variant_id: UUID
    stock_quantity: int = Field(..., ge=0)


class WeightStockUpdate(BaseModel):
    stock_grams: Optional[int] = Field(None, ge=0, description="Loose products: grams on hand")
    packs: Optional[list[PackCount]] = Field(None, description="Pre-packed products: packs per weight")


async def _weights(db: AsyncSession, inv: BranchInventory, product: Product) -> dict:
    from app.services import stock_keeping
    from app.services.weight_options_service import label

    variants = (await db.execute(
        select(ProductVariant).where(ProductVariant.product_id == product.product_id,
                                     ProductVariant.weight_grams.is_not(None),
                                     ProductVariant.is_active.is_not(False))
        .order_by(ProductVariant.weight_grams))).scalars().all()
    counts = dict((await db.execute(
        select(BranchVariantInventory.variant_id, BranchVariantInventory.stock_quantity)
        .where(BranchVariantInventory.branch_id == inv.branch_id,
               BranchVariantInventory.variant_id.in_([v.variant_id for v in variants])))).all()) if variants else {}
    options = []
    for v in variants:
        options.append({
            "variant_id": str(v.variant_id), "weight_grams": v.weight_grams, "label": label(v.weight_grams),
            "packs": counts.get(v.variant_id, 0) if product.stock_mode == "packed" else None,
            "available": await stock_keeping.available(db, inv.branch_id, product.product_id, v.variant_id),
        })
    return {
        "inventory_id": str(inv.inventory_id), "product_id": str(product.product_id),
        "stock_mode": product.stock_mode or "unit", "stock_grams": inv.stock_grams or 0,
        "stock_quantity": inv.stock_quantity or 0, "options": options,
        "kept_by_finance": bool(settings.finance_stock_authority),
    }


async def _scoped_row(db: AsyncSession, scope: BranchScope, inventory_id: UUID):
    found = await ProductService.get_inventory_by_id(db, inventory_id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inventory item not found")
    inv, product, _ = found
    if not scope.is_super_admin and inv.branch_id != scope.branch_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You can only modify inventory for your own branch.")
    return inv, product


@router.get("/{inventory_id}/weights", response_model=dict)
async def get_weight_stock(
    inventory_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """A weight product's stock in a branch: grams (loose) or packs per weight (packed),
    and how many of each weight can still be sold."""
    inv, product = await _scoped_row(db, scope, inventory_id)
    return {"success": True, "data": await _weights(db, inv, product)}


@router.put("/{inventory_id}/weights", response_model=dict,
            dependencies=[Depends(require_permission("inventory", "update"))])
async def set_weight_stock(
    inventory_id: UUID,
    body: WeightStockUpdate,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Count a weight product: grams for a loose one, packs of each weight for a pre-packed one."""
    from app.services import stock_keeping

    inv, product = await _scoped_row(db, scope, inventory_id)
    if settings.finance_stock_authority:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Stock is kept by Sribees Finance; count it there.")
    mode = product.stock_mode or "unit"
    if mode == "loose":
        if body.stock_grams is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Give the grams on hand.")
        await stock_keeping.set_level(db, inv.branch_id, stock_keeping.Item(
            "loose", product.product_id, None, None), body.stock_grams)
    elif mode == "packed":
        if not body.packs:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Give the packs of each weight.")
        mine = set((await db.execute(select(ProductVariant.variant_id).where(
            ProductVariant.product_id == product.product_id, ProductVariant.weight_grams.is_not(None)))).scalars())
        for pack in body.packs:
            if pack.variant_id not in mine:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="That weight is not one of this product's options.")
            await stock_keeping.set_level(db, inv.branch_id, stock_keeping.Item(
                "packed", product.product_id, pack.variant_id, None), pack.stock_quantity)
    else:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This product is counted in units; choose loose or pre-packed first.")
    await db.commit()
    await db.refresh(inv)
    logger.info(f"[admin] weight stock of {product.product_id} set in branch {inv.branch_id}")
    return {"success": True, "data": await _weights(db, inv, product)}
