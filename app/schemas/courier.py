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


class CourierEnvironmentConfig(BaseModel):
    """One environment's account-wide setup, described without its secret."""

    environment: str = Field(description="test | live")
    base_url: Optional[str] = Field(
        default=None, description="Origin only — the /api/v1/ecommerce path is appended."
    )
    base_url_ok: bool = Field(
        description=(
            "False when the stored URL already carries the /api/v1/ecommerce prefix the "
            "client appends itself, which doubles the path and 404s every call."
        )
    )
    api_key_set: bool
    api_key_masked: Optional[str] = Field(default=None, description="e.g. sk_test_****HJ0")
    api_key_environment: Optional[str] = Field(
        default=None,
        description=(
            "The environment the key's own prefix claims. A mismatch with `environment` "
            "means a live key is sitting in the sandbox slot, or the reverse."
        ),
    )
    api_key_unreadable: bool = Field(
        default=False,
        description=(
            "A key IS stored but will not decrypt -- the credential master key changed "
            "since it was saved. Re-enter the key to repair it."
        ),
    )
    usable: bool = Field(description="Whether a base URL and a readable key are both present.")


class CourierBranchKeyStatus(BaseModel):
    """One branch's own credentials and which environment it books in."""

    branch_id: str
    branch_code: str
    branch_name: str
    #: NULL when the branch follows the account default.
    environment_override: Optional[str] = None
    effective_environment: str
    test_key_set: bool
    test_key_masked: Optional[str] = None
    live_key_set: bool
    live_key_masked: Optional[str] = None
    key_unreadable: bool = Field(
        default=False, description="A stored key here will not decrypt -- re-enter it."
    )
    #: branch | account — where the key it actually books with comes from.
    key_source: str
    set_at: Optional[str] = None
    set_by: Optional[str] = None


class CourierCredentialsResponse(BaseModel):
    """Everything Settings → Courier needs, with no secret in it."""

    #: The environment branches book in unless they override it.
    environment: str
    environments: List[CourierEnvironmentConfig]
    branches: List[CourierBranchKeyStatus]
    webhook_secret_configured: bool = Field(
        description=(
            "Whether inbound status pushes can be verified at all. False means every "
            "webhook SribeesExpress sends is rejected, and orders only move when the "
            "reconciliation sweep runs."
        )
    )
    any_key_unreadable: bool = Field(
        default=False,
        description=(
            "At least one stored credential will not decrypt. Every affected slot has to "
            "be re-entered; nothing can book until they are."
        ),
    )
    credential_encryption: str = Field(
        description=(
            "dedicated — CREDENTIAL_ENCRYPTION_KEY is set. derived — it is being derived "
            "from JWT_SECRET_KEY, which works but means rotating that secret would make "
            "every stored key unreadable."
        )
    )


class CourierKeyUpdate(BaseModel):
    """Set or clear one key. An empty value clears the slot."""

    environment: str = Field(description="test | live")
    api_key: Optional[str] = Field(default=None, max_length=200)


class CourierBaseUrlUpdate(BaseModel):
    """Set the host for one environment. Origin only."""

    environment: str = Field(description="test | live")
    base_url: Optional[str] = Field(default=None, max_length=500)


class CourierEnvironmentUpdate(BaseModel):
    """Switch the account default, or pin/unpin one branch."""

    environment: Optional[str] = Field(
        default=None, description="test | live. Null (branch only) = follow the account."
    )


class CourierKeyTestResult(BaseModel):
    """Outcome of a live call made with one scope's credentials."""

    ok: bool
    environment: Optional[str] = None
    key_source: Optional[str] = None
    detail: str
    post_offices: Optional[int] = Field(
        default=None, description="Coverage rows the key could see, when the call succeeded."
    )
