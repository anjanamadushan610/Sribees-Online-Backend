"""
Festival offer: a short, store-wide promotion with a fixed end.

11.11 (owner, 2026-11-10):
  * Live from the deploy until 2026-11-11 23:59 Sri Lanka time, then off by
    itself -- no one has to remember to switch it back.
  * Every product earns exactly 11% cashback while it runs, whatever its own
    or its branch's rate. A product whose resolved rate is 0% (sold by weight,
    eggs, coconuts: low margin, the owner's choice) stays at 0%.
  * Product pictures are shown on the 11.11 design (``offer_imagery``); a 0%
    product's picture says "11.11 SALE" without the cashback ribbon.

Nothing stored changes. Product and branch cashback rates are left as they
are; the offer is applied where a rate is resolved, so when it ends the old
rates are simply what resolves again. An order freezes the rate it was placed
at (``order_items.cashback_percentage``), so an order placed during the offer
keeps 11% after it ends, and is held and settled by the usual rules.

``FESTIVAL_OFFER_DISABLED=1`` in the environment switches it off early.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from app.config.settings import settings


@dataclass(frozen=True)
class FestivalOffer:
    code: str
    ends_at: datetime  # exclusive, UTC
    cashback_percent: float


# 2026-11-11 23:59:59 Asia/Colombo (UTC+05:30) -> ends at 18:30 UTC.
OFFER_1111 = FestivalOffer(
    code="1111",
    ends_at=datetime(2026, 11, 11, 18, 30, tzinfo=timezone.utc),
    cashback_percent=11.0,
)

_OFFERS = (OFFER_1111,)

# URL path the offer pictures are served under (see app.api.offer_images).
OFFER_PATH = "/offer"


def _disabled() -> bool:
    return os.environ.get("FESTIVAL_OFFER_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")


def active_offer(now: Optional[datetime] = None) -> Optional[FestivalOffer]:
    """The offer running at *now* (server UTC), or None."""
    if _disabled():
        return None
    now = now or datetime.now(timezone.utc)
    for offer in _OFFERS:
        if now < offer.ends_at:
            return offer
    return None


def offer_by_code(code: str) -> Optional[FestivalOffer]:
    for offer in _OFFERS:
        if offer.code == code:
            return offer
    return None


def apply_cashback(rate, now: Optional[datetime] = None):
    """
    The cashback percentage a product earns, given its resolved *rate*.

    During an offer: 0 stays 0, anything else becomes the offer's rate. Outside
    one, *rate* is returned untouched (same type, so callers see no change).
    """
    offer = active_offer(now)
    if offer is None or rate is None:
        return rate
    if float(rate) <= 0:
        return rate
    return offer.cashback_percent


def offer_image_url(path: Optional[str], cashback, now: Optional[datetime] = None) -> Optional[str]:
    """
    The URL a shopper should load for a product picture stored at *path*.

    During an offer, our own uploaded picture is drawn on the offer design (the
    ribbon only when *cashback* -- the rate the product earns -- is above 0).
    Anything else (no offer, an external URL, a non-product file) resolves the
    usual way.
    """
    from app.core.media import media_url

    offer = active_offer(now)
    if offer is None or not path:
        return media_url(path)
    cleaned = path.strip()
    prefix = f"/{settings.media_url_prefix.strip('/')}/products/"
    if not cleaned.startswith(prefix):
        return media_url(path)
    variant = "c" if cashback is not None and float(cashback) > 0 else "n"
    base = (settings.media_base_url or "").rstrip("/")
    return f"{base}{OFFER_PATH}/{offer.code}/{variant}{quote(cleaned)}"


def strip_offer_url(url: str) -> str:
    """Undo ``offer_image_url``: the stored media path behind an offer URL.

    Guards the admin's save path, so a picture URL the API handed out during an
    offer can never be written back into the catalogue."""
    from urllib.parse import unquote

    # Only our own offer URLs: on MEDIA_BASE_URL, or same-origin relative.
    base = (settings.media_base_url or "").rstrip("/")
    marker = f"{OFFER_PATH}/"
    if base and url.startswith(base + marker):
        rest = url[len(base) + len(marker):]
    elif url.startswith(marker):
        rest = url[len(marker):]
    else:
        return url
    parts = rest.split("/", 2)  # code, variant, path
    if len(parts) < 3 or offer_by_code(parts[0]) is None or parts[1] not in ("c", "n"):
        return url
    return "/" + unquote(parts[2]).lstrip("/")


def plain_image_url(url: Optional[str]) -> Optional[str]:
    """The plain (absolute) picture URL behind *url*; any other URL unchanged.
    For values kept beyond the offer, such as an order line's photo."""
    if not url:
        return None
    from app.core.media import media_url

    stripped = strip_offer_url(url.strip())
    return media_url(stripped) if stripped != url.strip() else url
