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


class CourierKeyStatus(BaseModel):
    """
    One credential slot, described without ever revealing it.

    ``masked`` keeps the ``sk_test_``/``sk_live_`` prefix on purpose: which
    environment a key books in is the single most consequential thing about it,
    and "did go-live actually happen?" should be answerable at a glance.
    """

    scope: str = Field(description="account | branch")
    branch_id: Optional[str] = None
    branch_code: Optional[str] = None
    branch_name: Optional[str] = None
    is_set: bool = Field(description="Whether this slot holds its own key.")
    masked: Optional[str] = Field(default=None, description="e.g. sk_test_****HJ0")
    environment: Optional[str] = Field(
        default=None, description="test | live | unknown — derived from the key prefix."
    )
    source: str = Field(
        description=(
            "Where the key actually used for this scope comes from: "
            "branch | account | environment | none."
        )
    )
    set_at: Optional[str] = None
    set_by: Optional[str] = None


class CourierCredentialsResponse(BaseModel):
    """The deployment's own configuration, the account slot, and one row per branch."""

    config: "CourierConfigStatus"
    account: CourierKeyStatus
    branches: List[CourierKeyStatus]


class CourierKeyUpdate(BaseModel):
    """Set or clear a key. An empty/absent value clears the slot."""

    api_key: Optional[str] = Field(
        default=None,
        max_length=200,
        description="The SribeesExpress merchant key. Empty clears this slot.",
    )


class CourierKeyTestResult(BaseModel):
    """Outcome of a live call made with one scope's key."""

    ok: bool
    environment: Optional[str] = None
    detail: str
    post_offices: Optional[int] = Field(
        default=None, description="Coverage rows the key could see, when the call succeeded."
    )


class CourierConfigStatus(BaseModel):
    """
    What THIS deployment is configured with — the question that otherwise
    needs an SSH session and a look at the server's .env.

    Only presence and shape, never values. ``base_url`` is included in full
    because it is not a secret and because getting it wrong is the single most
    common misconfiguration: the client appends ``/api/v1/ecommerce`` itself,
    so a base URL that already carries the prefix 404s every call.
    """

    base_url: Optional[str] = None
    base_url_ok: bool = Field(
        description=(
            "False when the base URL already contains the /api/v1/ecommerce prefix "
            "the client appends itself, which doubles the path."
        )
    )
    api_key_configured: bool
    api_key_environment: Optional[str] = Field(
        default=None, description="test | live | unknown — from the account-wide key."
    )
    webhook_secret_configured: bool = Field(
        description=(
            "Whether inbound status pushes can be verified at all. False means every "
            "webhook SribeesExpress sends is rejected, and orders only move when the "
            "reconciliation sweep runs."
        )
    )
    webhook_tolerance_seconds: int
    request_timeout_seconds: float


CourierCredentialsResponse.model_rebuild()
