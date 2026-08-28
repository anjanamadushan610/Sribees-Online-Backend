"""
In-memory state for the mock courier.

Resets on every process restart — this is a test double, not a database.
Shipments are keyed both by ``merchant_order_ref`` (for the booking-idempotency
check) and by ``shipment_id`` (for read/cancel/patch lookups), and the pickup
locations and post-office directory back sections 12 and 5 of the contract.
"""
from typing import Dict, List


class _Store:
    def __init__(self) -> None:
        self.quotes: Dict[str, dict] = {}
        self.shipments_by_ref: Dict[str, dict] = {}
        self.shipments_by_id: Dict[str, dict] = {}
        self.pickup_locations: Dict[int, dict] = {}
        self.webhook_endpoints: Dict[int, dict] = {}
        self._next_id = 1

    def next_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value


STORE = _Store()

# Stand-in for SribeesExpress's post-office -> city_id directory (contract
# section 5). Deliberately small and deliberately incomplete: the gaps are
# what let a developer exercise every `resolved: false` reason locally.
CITY_DIRECTORY: List[dict] = [
    {"post_office": "Colombo", "district": "Colombo", "province": "Western", "city_id": 42, "city_name": "Colombo 07"},
    {"post_office": "Welipenna", "district": "Kalutara", "province": "Western", "city_id": 43, "city_name": "Welipenna"},
    {"post_office": "Kandy", "district": "Kandy", "province": "Central", "city_id": 44, "city_name": "Kandy"},
    {"post_office": "Galle", "district": "Galle", "province": "Southern", "city_id": 45, "city_name": "Galle"},
    # Same name in two districts -> ambiguous_post_office unless a district is sent.
    {"post_office": "Nugegoda", "district": "Colombo", "province": "Western", "city_id": 46, "city_name": "Nugegoda"},
    {"post_office": "Nugegoda", "district": "Gampaha", "province": "Western", "city_id": 47, "city_name": "Nugegoda North"},
]

# Known to the directory but with no city_id -> not_yet_configured.
UNCONFIGURED_POST_OFFICES = {"Medawachchiya", "NOT_CONFIGURED_TEST"}
