"""
Courier Location Resolution — post office to SribeesExpress ``city_id``.

Every rate quote and every booking is keyed on a ``city_id``
(``docs/SRIBEESEXPRESS_ECOMMERCE_API.md`` sections 5a, 6, 7), and that id is
not something our addresses carry: it is an internal SribeesExpress grouping
of post offices onto a branch coverage area. So it has to be resolved once and
cached, which is what the contract itself asks for — "cache the result per
address ... only when an address is first saved or changed."

Resolution is **best-effort and never blocks the customer**. A saved address
that cannot be resolved still saves; checkout then falls back to the flat
platform delivery fee, exactly as it does during a courier outage. The reason
is stored so support (and a later re-resolve sweep) can tell "SribeesExpress
has never heard of this post office" apart from "they know it but haven't
configured a delivery area for it yet" — the first is the customer's typo, the
second is a SribeesExpress ops ticket.
"""
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Address
from app.services import courier_credentials
from app.services.courier_service import (
    CourierError,
    CourierService,
    CourierUnavailableError,
)

# resolved: false reasons that can flip to resolved: true later without the
# customer changing anything -- SribeesExpress ops just has to configure the
# area. Worth re-trying on a sweep; the others are not.
RETRYABLE_REASONS = {"not_yet_configured", "no_delivery_to_this_area"}


async def resolve_address(db: AsyncSession, address: Address, *, commit: bool = True) -> Optional[int]:
    """
    Resolve ``address`` to a ``city_id`` and cache it on the row.

    Returns the ``city_id``, or ``None`` if it could not be resolved. Never
    raises: a courier outage here must not fail an address save.

    ``district`` is always sent when we have it — it is the disambiguator for
    duplicate post-office names, and omitting it is precisely what produces
    ``ambiguous_post_office``. Every address in our schema has a district
    (it is NOT NULL), so in practice we should never see that reason.
    """
    try:
        # The account scope, not a branch one: city ids are an account-level
        # notion, and a saved address does not belong to a branch yet.
        async with courier_credentials.for_account(db):
            result = await CourierService.resolve_location(
                post_office=address.post_office,
                district=address.district,
                province=address.province,
            )
    except (
        CourierUnavailableError,
        CourierError,
        courier_credentials.CourierNotConfiguredError,
    ) as exc:
        # Deliberately leaves courier_resolved_at untouched: this is "we don't
        # know yet", not "we asked and the answer was no". A later sweep
        # should retry it, and a NULL resolved_at is how it finds it.
        logger.warning(f"Could not resolve city_id for address {address.address_id}: {exc}")
        return None

    address.courier_resolved_at = datetime.now(timezone.utc)
    if result.resolved:
        address.courier_city_id = result.city_id
        address.courier_city_name = result.city_name
        address.courier_resolve_reason = None
        logger.info(
            f"Address {address.address_id} ({address.post_office}) resolved to "
            f"city_id={result.city_id} ({result.city_name})"
        )
    else:
        address.courier_city_id = None
        address.courier_city_name = None
        address.courier_resolve_reason = result.reason
        logger.warning(
            f"Address {address.address_id} ({address.post_office}) unresolvable: {result.reason}"
        )

    if commit:
        await db.commit()
    return address.courier_city_id


def needs_resolution(address: Address, *, post_office_changed: bool) -> bool:
    """
    Whether ``address`` should be (re-)resolved.

    Re-resolve when the post office or district changed, when we have never
    asked, or when the last answer was one that SribeesExpress ops can flip
    without the customer touching anything.
    """
    if post_office_changed:
        return True
    if address.courier_resolved_at is None:
        return True
    return address.courier_resolve_reason in RETRYABLE_REASONS
