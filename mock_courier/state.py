"""
In-memory state for the mock courier.

Resets on every process restart — this is a test double, not a database.
Keyed both by order_number (for the booking-idempotency check) and by
shipment_id (for GET/cancel/simulate-delivery lookups).
"""
from typing import Dict


class _Store:
    def __init__(self) -> None:
        self.quotes: Dict[str, dict] = {}
        self.shipments_by_order: Dict[str, dict] = {}
        self.shipments_by_id: Dict[str, dict] = {}


STORE = _Store()
