"""
Courier Location — an address's SribeesExpress ``postal_city_id``.

Every delivery quote is keyed on a ``postal_city_id``
(``docs/SRIBEESEXPRESS_ECOMMERCE_API.md`` sections 5 and 6): SribeesExpress's
own id for the Sri Lanka Post office town an address is written in. Their
contract asks us to match an address against their postal city list **by name
and district** and store the id, rather than sending a name for them to match.

That list is synced into ``post_office_directory`` by
``courier_postal_city_service.sync``, so resolving an address is a local
lookup — no network call on address save, and nothing a courier outage can
break. Matching is case-insensitive and exact, with district as the tiebreaker
for the handful of towns that share a name.

An address that does not match keeps ``courier_postal_city_id`` NULL. That is
an honest "we cannot price delivery here": checkout refuses it rather than
guessing a fee, and the next quote attempt looks again, so a directory sync
that adds the town makes the address quotable without the customer re-saving it.
"""
from typing import Optional

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import PostalCityDirectory
from app.models.user import Address


async def postal_city_id_for(
    db: AsyncSession, postal_city: Optional[str], district: Optional[str]
) -> Optional[int]:
    """The SribeesExpress id for a postal city, from the synced directory, or None."""
    if not postal_city or not district:
        return None
    return (
        await db.execute(
            select(PostalCityDirectory.courier_postal_city_id).where(
                func.lower(PostalCityDirectory.postal_city) == postal_city.strip().casefold(),
                func.lower(PostalCityDirectory.district) == district.strip().casefold(),
                PostalCityDirectory.is_active.is_(True),
                PostalCityDirectory.courier_postal_city_id.isnot(None),
            )
        )
    ).scalar_one_or_none()


async def resolve_address(db: AsyncSession, address: Address, *, commit: bool = True) -> Optional[int]:
    """
    Match ``address`` against the directory and store the result on the row.

    Returns the ``postal_city_id``, or None. Clears a stale id when the address
    no longer matches — an address edited to a town the courier does not serve
    must not keep quoting at its old town's price.
    """
    postal_city_id = await postal_city_id_for(db, address.postal_city, address.district)
    if postal_city_id != address.courier_postal_city_id:
        address.courier_postal_city_id = postal_city_id
        if commit:
            await db.commit()
    if postal_city_id is None:
        logger.info(
            f"Address {address.address_id} ({address.postal_city}, {address.district}) "
            f"does not match a SribeesExpress postal city"
        )
    return postal_city_id


async def postal_city_id_for_address(db: AsyncSession, address: Address) -> Optional[int]:
    """
    The id to quote this address with, re-checking the directory if none is stored.

    Cheap enough to call on every quote, and what lets a directory sync reach
    addresses saved before the town was added.
    """
    if address.courier_postal_city_id is not None:
        return address.courier_postal_city_id
    return await resolve_address(db, address)
