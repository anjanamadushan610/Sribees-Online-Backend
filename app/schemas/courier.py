"""
Courier-facing schemas.

Deliberately thin. The inbound webhook (``app/api/v1/courier_webhooks.py``) is
signed over its **raw body** (contract section 10.3), so it verifies and then
parses the bytes itself rather than letting FastAPI deserialize a payload
before it has been authenticated — a Pydantic model on that route would mean
parsing untrusted input first, and would also reject the conditional
``handover``/``cod``/``failure_note`` blocks the contract adds on
delivered/failed/returned transitions.

``DeliveryCodeConfirmRequest`` is gone with the old ``/v1/*`` contract: under
``docs/SRIBEESEXPRESS_ECOMMERCE_API.md`` section 7 the handover code is sent
to SribeesExpress at booking time and verified by the rider app against their
copy, so nothing calls into us to confirm it any more.
"""
from typing import List, Optional

from pydantic import BaseModel, Field


class PickupLocationSyncResult(BaseModel):
    """One branch's outcome from the pickup-location registration sweep."""

    branch_code: str
    branch_name: str
    pickup_location_id: Optional[int] = None
    action: str = Field(
        description=(
            "created | updated | recovered | skipped | failed. `recovered` means "
            "SribeesExpress already had a location under this branch's name and we "
            "re-linked its id -- creating a duplicate is refused by design."
        )
    )
    detail: Optional[str] = None


class PickupLocationSyncResponse(BaseModel):
    """Response of POST /admin/courier/pickup-locations/sync."""

    synced: int
    failed: int
    results: List[PickupLocationSyncResult]


class CourierSweepResponse(BaseModel):
    """Response of POST /admin/courier/sync (contract section 7a poll)."""

    seen: int
    applied: int


class CoverageOrphan(BaseModel):
    """A directory row the sync switched off that something still points at."""

    post_office: str
    district: str
    addresses: int = Field(
        description="Saved customer addresses still naming this post office."
    )


class CoverageSyncResponse(BaseModel):
    """Response of POST /admin/courier/coverage/sync."""

    fetched: int
    created: int
    updated: int
    reactivated: int
    deactivated: int
    districts_covered: List[str] = Field(
        description=(
            "Districts SribeesExpress reported on. Only these are reconciled -- "
            "a district they have not configured keeps whatever rows it has."
        )
    )
    orphaned: List[CoverageOrphan] = Field(
        default_factory=list,
        description=(
            "Deactivated post offices still referenced by saved addresses. "
            "Each is a customer who will be refused at checkout until they "
            "re-pick an address, so these need following up, not ignoring."
        ),
    )
