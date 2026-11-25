"""Cart savings must use current catalogue prices, including weight options."""
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import weight_options_service
from app.services.order_service import OrderService
from app.services.product_service import ProductService


@pytest.mark.asyncio
@pytest.mark.parametrize('weighted', [False, True])
async def test_regular_price_is_authoritative_for_discount_display(monkeypatch, weighted):
    product_id, variant_id = uuid4(), uuid4()
    product_key, variant_key = str(product_id), str(variant_id)
    monkeypatch.setattr(ProductService, 'get_price_map', AsyncMock(return_value={product_key: Decimal('420')}))
    monkeypatch.setattr(ProductService, 'get_regular_price_map', AsyncMock(return_value={product_key: Decimal('480')}))
    monkeypatch.setattr(ProductService, 'get_display_size_map', AsyncMock(return_value={}))
    monkeypatch.setattr(ProductService, 'get_option_label_map', AsyncMock(return_value={}))
    monkeypatch.setattr(weight_options_service, 'price_map', AsyncMock(return_value=(
        {variant_key: (product_key, Decimal('210'), '500g', 500)} if weighted else {}
    )))
    monkeypatch.setattr(weight_options_service, 'regular_price_map', AsyncMock(return_value=(
        {variant_key: (product_key, Decimal('240'))} if weighted else {}
    )))
    item = {'product_id': product_key, 'quantity': 11, 'price': 1, 'original_price': 99999}
    if weighted:
        item['variant_id'] = variant_key
    result = (await OrderService._attach_server_prices(AsyncMock(), [item], None))[0]
    assert result['price'] == (210 if weighted else 420)
    assert result['original_price'] == (240 if weighted else 480)
    assert (result['original_price'] - result['price']) * result['quantity'] == (330 if weighted else 660)
    assert item['original_price'] == 99999
