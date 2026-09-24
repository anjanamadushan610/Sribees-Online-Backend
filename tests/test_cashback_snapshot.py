"""
Per-product cashback bonus and its per-line snapshot on order items.
"""
import pytest
from pydantic import ValidationError

from app.models.order import Order, OrderItem
from app.schemas.product import ProductCreate, ProductUpdate


def test_the_rate_is_mapped_on_the_order_item_not_the_order():
    # A fake-object test can't see a mis-mapped column; see a804ce6.
    assert "cashback_percentage" in OrderItem.__table__.columns
    assert "cashback_percentage" not in Order.__table__.columns
    assert OrderItem(cashback_percentage=2.5).cashback_percentage == 2.5


def test_product_update_accepts_a_bonus_and_an_explicit_clear():
    assert ProductUpdate(cashback_percentage=5).cashback_percentage == 5
    cleared = ProductUpdate(cashback_percentage=None)
    # An explicit null must reach the model so the admin can drop a bonus.
    assert cleared.model_dump(exclude_unset=True) == {"cashback_percentage": None}


@pytest.mark.parametrize("bad", [-1, 100.01])
def test_product_bonus_is_a_percentage(bad):
    with pytest.raises(ValidationError):
        ProductUpdate(cashback_percentage=bad)
    with pytest.raises(ValidationError):
        ProductCreate(
            name="x", slug="x", sku="x",
            category_id="00000000-0000-0000-0000-000000000001",
            cashback_percentage=bad,
        )
