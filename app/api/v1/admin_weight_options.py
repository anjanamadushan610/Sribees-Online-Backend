"""
Weight options of a sold-by-weight product (/admin/products/{id}/weight-options).

The Super Admin sets the options and their global prices; a branch manager
(or the Super Admin, naming the branch) sets that branch's own price for an
option. Pricing rules live in weight_options_service.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import BranchScope, inject_branch_filter, require_permission
from app.models.product import Product
from app.schemas.product import (
    BranchWeightOptionPrice,
    WeightOptionPrice,
    WeightOptionsReplace,
)
from app.services import weight_options_service

router = APIRouter(
    dependencies=[Depends(require_permission("products", "read"))],
    tags=["Admin Weight Options"],
)


async def _product(db: AsyncSession, product_id: UUID) -> Product:
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    return product


def _super_admin_only(scope: BranchScope) -> None:
    if not scope.is_super_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the Super Admin sets a product's weight options and their global prices.",
        )


async def _payload(db: AsyncSession, product: Product, branch_id: Optional[UUID]) -> dict:
    options = await weight_options_service.options_for(db, [product.product_id], branch_id)
    return {
        "product_id": str(product.product_id),
        "sold_by_weight": bool(product.sold_by_weight),
        "branch_id": str(branch_id) if branch_id else None,
        "options": options.get(str(product.product_id), []),
    }


@router.get("/products/{product_id}/weight-options", response_model=dict)
async def get_weight_options(
    product_id: UUID,
    branch_id: Optional[UUID] = Query(
        None, description="Super Admin: price the options for this branch"
    ),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """The product's weight options, priced for the caller's branch (or ``branch_id``)."""
    product = await _product(db, product_id)
    return {"success": True, "data": await _payload(db, product, scope.resolve(branch_id))}


@router.put("/products/{product_id}/weight-options", response_model=dict,
            dependencies=[Depends(require_permission("products", "update"))])
async def replace_weight_options(
    product_id: UUID,
    body: WeightOptionsReplace,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Replace the whole list of options (matched by weight) with their global prices."""
    _super_admin_only(scope)
    product = await _product(db, product_id)
    weights = [o.weight_grams for o in body.options]
    if len(set(weights)) != len(weights):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Each weight may appear only once.")
    await weight_options_service.replace_options(
        db, product, [(o.weight_grams, o.price) for o in body.options]
    )
    await db.commit()
    logger.info(f"[admin] Weight options set for product {product_id}: {sorted(weights)}")
    return {"success": True, "data": await _payload(db, product, None)}


@router.patch("/products/{product_id}/weight-options/{variant_id}", response_model=dict,
              dependencies=[Depends(require_permission("products", "update"))])
async def set_weight_option_price(
    product_id: UUID,
    variant_id: UUID,
    body: WeightOptionPrice,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Set one option's global price; the 1 kg option's is the product's price."""
    _super_admin_only(scope)
    product = await _product(db, product_id)
    option = await weight_options_service.get_option(db, product_id, variant_id)
    if option is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Weight option not found")
    option.price = body.price
    if option.weight_grams == weight_options_service.KILO:
        product.price = body.price
    await db.commit()
    return {"success": True, "data": await _payload(db, product, None)}


@router.put("/products/{product_id}/weight-options/{variant_id}/branch-price",
            response_model=dict,
            dependencies=[Depends(require_permission("inventory", "update"))])
async def set_weight_option_branch_price(
    product_id: UUID,
    variant_id: UUID,
    body: BranchWeightOptionPrice,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Set one branch's price for an option; ``price: null`` goes back to the
    global price. A branch-scoped admin always acts on their own branch.
    """
    branch_id = scope.resolve(body.branch_id) if scope.is_super_admin else scope.branch_id
    if branch_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "branch_id is required when pricing as Super Admin."
        )
    product = await _product(db, product_id)
    option = await weight_options_service.get_option(db, product_id, variant_id)
    if option is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Weight option not found")
    await weight_options_service.set_branch_price(db, branch_id, option, body.price)
    await db.commit()
    return {"success": True, "data": await _payload(db, product, branch_id)}
