from types import SimpleNamespace
from uuid import uuid4

from app.services.inventory_batches import allocate


def test_fifo_uses_the_oldest_batch_first_and_splits_at_the_price_boundary():
    older = SimpleNamespace(batch_id=uuid4(), remaining=50)
    newer = SimpleNamespace(batch_id=uuid4(), remaining=50)
    used = {}

    assert allocate([older, newer], 100, 51, used) == ([(older, 50), (newer, 1)], 0)
    assert allocate([older, newer], 100, 50, used) == ([(newer, 49)], 1)


def test_fifo_preserves_existing_untracked_stock():
    newest = SimpleNamespace(batch_id=uuid4(), remaining=50)
    assert allocate([newest], 100, 52, {}) == ([(None, 50), (newest, 2)], 0)
