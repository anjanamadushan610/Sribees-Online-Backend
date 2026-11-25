"""Server-owned threshold gifts for ordinary, fixed-unit products."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from app.models.product import BranchInventory, Product, ProductVariant
from app.services.app_settings_service import AppSettingsService

KEY = "checkout_order_gifts"


class GiftItem(BaseModel):
    product_id: UUID
    quantity: int = Field(ge=1, le=20)


class GiftRule(BaseModel):
    enabled: bool = False
    minimum: Decimal = Field(default=Decimal("0"), ge=0, max_digits=10, decimal_places=2)
    items: list[GiftItem] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_rule(self):
        if self.enabled and (self.minimum <= 0 or not self.items):
            raise ValueError("Enter a positive minimum and at least one gift.")
        if len({line.product_id for line in self.items}) != len(self.items):
            raise ValueError("Add each gift product once and set its quantity.")
        return self


def gift_product_filter():
    variants = select(ProductVariant.variant_id).where(
        ProductVariant.product_id == Product.product_id,
        ProductVariant.is_active.is_not(False),
    )
    return (
        Product.is_active.is_(True),
        Product.stock_mode == "unit",
        Product.sold_by_weight.is_not(True),
        ~variants.exists(),
    )


async def get_rule(db):
    setting = await AppSettingsService.get_by_key(db, KEY)
    return GiftRule.model_validate_json(setting.value) if setting and setting.value else GiftRule()


async def qualify(db, branch_id, subtotal, discount, paid_items):
    if not branch_id:
        return []
    rule = await get_rule(db)
    if not rule.enabled or subtotal - discount < rule.minimum:
        return []
    requested = {line.product_id: line.quantity for line in rule.items}
    already_ordered = {}
    for item in paid_items:
        key = str(item.get("product_id"))
        already_ordered[key] = already_ordered.get(key, 0) + int(item["quantity"])

    rows = (await db.execute(
        select(Product, BranchInventory.stock_quantity)
        .join(BranchInventory, BranchInventory.product_id == Product.product_id)
        .where(
            Product.product_id.in_(requested),
            *gift_product_filter(),
            BranchInventory.branch_id == branch_id,
            BranchInventory.is_active.is_(True),
        ).order_by(Product.product_id)
    )).all()
    return [
        {"product_id": str(product.product_id), "quantity": requested[product.product_id],
         "name": product.name, "display_size": product.display_size,
         "display_size_unit": product.display_size_unit, "sku": product.sku}
        for product, stock in rows
        if stock >= requested[product.product_id] + already_ordered.get(str(product.product_id), 0)
    ]
