"""
SRIBEESonline - Product Share Links

Mounted at the site root (not under /api) because Android App Links require
exact, stable URLs:

  GET /.well-known/assetlinks.json  — Digital Asset Links proof that the app
                                      owns this host (enables autoVerify).
  GET /.well-known/apple-app-site-association
                                    — the iOS equivalent: lets the iPhone app
                                      open /p/... links (Universal Links).
  GET /p/{product_id}?b={branch_id} — The link shoppers share on WhatsApp etc.
                                      ``b`` is the sharer's branch, shown as
                                      "from the Kandy branch" here and in the app.
  GET /p/{product_id}/share.jpg     — The branded picture that goes with it: the
                                      app attaches it when sharing, and it is the
                                      link's preview image (og:image).

How a tap on ``https://<host>/p/<id>`` resolves:

  * App installed + App Links verified → Android opens the app directly; this
    page is never loaded.
  * App installed but not verified (debug-signed build, fingerprint not yet
    configured) → the page fires an ``intent://`` URL that opens the app.
  * App not installed → the same intent falls back to the Play Store, carrying
    ``product_id`` in the install referrer so the app can open the product on
    first launch.
  * iOS / desktop → a plain page with a Play Store button.

The HTML also carries Open Graph tags so WhatsApp renders a product preview.
"""
import asyncio
import hashlib
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.media import media_url
from app.services import branch_service, product_imagery
from app.services.product_service import ProductService
from app.services.storage import get_storage

router = APIRouter(include_in_schema=False)

_SHARE_CACHE_SECONDS = 300
# The app asks for the card without a version, so keep a replaced photo
# from travelling for long under the old card.
_CARD_CACHE_SECONDS = 3600

# Rendering a card is a second or so of CPU; a burst of first-time shares must
# not tie up every worker thread.
_render_slots = asyncio.Semaphore(2)


def _fingerprints() -> list[str]:
    return [
        fp.strip().upper()
        for fp in settings.android_sha256_cert_fingerprints.split(",")
        if fp.strip()
    ]


def play_store_url(product_id: Optional[str] = None, branch_id: Optional[str] = None) -> str:
    url = f"https://play.google.com/store/apps/details?id={settings.android_package_name}"
    if product_id:
        # Play passes `referrer` verbatim to the Install Referrer API.
        referrer = {"utm_source": "share", "product_id": product_id}
        if branch_id:
            referrer["branch_id"] = branch_id
        url += "&referrer=" + quote(urlencode(referrer), safe="")
    return url


def intent_url(product_id: str, fallback: str, branch_id: Optional[str] = None) -> str:
    """Chrome intent URL: open the app on the custom scheme, else `fallback`."""
    query = f"?b={branch_id}" if branch_id else ""
    return (
        f"intent://product/{quote(product_id, safe='')}{query}"
        f"#Intent;scheme=sribees;package={settings.android_package_name};"
        f"S.browser_fallback_url={quote(fallback, safe='')};end"
    )


def _primary_path(product) -> Optional[str]:
    """The primary picture's stored path (relative, or a legacy absolute URL)."""
    images = sorted(
        product.images or [],
        key=lambda i: (not i.is_primary, i.sort_order or 0),
    )
    return images[0].image_url if images else None


def _card_key(image_path: str) -> str:
    """Names one product picture in one card design: a new photo or a new
    design is a new key, so a cached card never outlives either."""
    seed = f"{image_path}|{product_imagery.SHARE_TEMPLATE_VERSION}"
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


def _card_dir() -> Path:
    return Path(settings.media_root) / "share-cards"


def _public_url(request: Request, path: str, query: str = "") -> str:
    """An absolute URL on this host. Caddy terminates TLS, so the scheme the
    shopper used comes from X-Forwarded-Proto, not from our own socket."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    return str(request.url.replace(scheme=scheme or request.url.scheme, path=path, query=query))


@router.get("/.well-known/assetlinks.json")
async def asset_links():
    statements = []
    fingerprints = _fingerprints()
    if fingerprints:
        statements.append(
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": settings.android_package_name,
                    "sha256_cert_fingerprints": fingerprints,
                },
            }
        )
    return JSONResponse(
        statements,
        headers={"Cache-Control": f"public, max-age={_SHARE_CACHE_SECONDS}"},
    )


@router.get("/.well-known/apple-app-site-association")
@router.get("/apple-app-site-association", include_in_schema=False)
async def apple_app_site_association():
    """
    Universal Links: the iPhone app (Team ID + bundle ID) may open product
    share links itself. Apple fetches this over HTTPS with no redirects and
    wants JSON with no file extension. Empty until both IDs are configured,
    so a half-set config claims no app.
    """
    details = []
    if settings.apple_team_id and settings.apple_bundle_id:
        details.append(
            {
                "appIDs": [f"{settings.apple_team_id}.{settings.apple_bundle_id}"],
                "components": [{"/": "/p/*", "comment": "Product share links"}],
            }
        )
    return JSONResponse(
        {"applinks": {"details": details}},
        headers={"Cache-Control": f"public, max-age={_SHARE_CACHE_SECONDS}"},
    )


@router.get("/p/{product_id}", response_class=HTMLResponse)
async def product_share_page(
    product_id: str,
    request: Request,
    b: Optional[str] = Query(None, max_length=64),
    db: AsyncSession = Depends(get_db),
):
    product = None
    try:
        product = await ProductService.get_by_id_or_slug(db, product_id)
    except Exception as exc:  # A broken lookup must not break the redirect.
        logger.warning(f"Share page lookup failed for {product_id!r}: {exc}")

    # Only a live branch's own id is carried on - never the raw query string.
    branch = None
    try:
        if b:
            branch = await branch_service.get_public_branch(db, UUID(b))
    except Exception as exc:
        logger.warning(f"Share page branch lookup failed for {b!r}: {exc}")
    branch_id = str(branch.branch_id) if branch else None

    title = product.name if product else "SRIBEESonline"
    if product and product.price is not None:
        description = f"Rs. {Decimal(product.price):,.2f} — Shop now on SRIBEESonline"
    else:
        description = "Fresh groceries delivered to your door — SRIBEESonline"
    image_path = _primary_path(product) if product else None
    image = (
        _public_url(request, f"/p/{product.product_id}/share.jpg", f"v={_card_key(image_path)}")
        if image_path
        else None
    )

    store = play_store_url(product_id, branch_id)
    open_app = intent_url(product_id, store, branch_id)
    canonical = _public_url(request, request.url.path, f"b={branch_id}" if branch_id else "")

    e = lambda s: escape(s, quote=True)  # noqa: E731
    og_image = (
        f'<meta property="og:image" content="{e(image)}">'
        '<meta property="og:image:type" content="image/jpeg">'
        f'<meta property="og:image:width" content="{product_imagery.SHARE_SIZE}">'
        f'<meta property="og:image:height" content="{product_imagery.SHARE_SIZE}">'
        if image
        else ""
    )
    # The page itself shows the plain photo; the branded card is for previews.
    photo = media_url(image_path) if image_path else None
    img_tag = f'<img src="{e(photo)}" alt="">' if photo else ""
    branch_tag = (
        f'<p class="branch">Shared from the {e(branch.name)} branch</p>' if branch else ""
    )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
<meta property="og:type" content="product">
<meta property="og:site_name" content="SRIBEESonline">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(description)}">
<meta property="og:url" content="{e(canonical)}">
{og_image}
<style>
  body {{ margin:0; font-family:system-ui,sans-serif; background:#f4f8f2; color:#1b2a1b;
         display:flex; min-height:100vh; align-items:center; justify-content:center; padding:16px; box-sizing:border-box; }}
  .card {{ background:#fff; border-radius:16px; padding:24px; max-width:360px; width:100%; text-align:center;
          box-shadow:0 4px 20px rgba(0,0,0,.08); }}
  img {{ max-width:100%; max-height:220px; object-fit:contain; border-radius:12px; }}
  h1 {{ font-size:1.2rem; margin:16px 0 4px; }}
  p {{ color:#4a5a4a; margin:0 0 20px; }}
  a.btn {{ display:block; padding:14px; border-radius:12px; text-decoration:none; font-weight:600; margin-top:10px; }}
  .primary {{ background:#2e7d32; color:#fff; }}
  .secondary {{ background:#e8f2e6; color:#2e7d32; }}
  .branch {{ font-size:.85rem; color:#6b7b6b; margin:-12px 0 20px; }}
</style>
</head>
<body>
<div class="card">
  {img_tag}
  <h1>{e(title)}</h1>
  <p>{e(description)}</p>
  {branch_tag}
  <a class="btn primary" id="open" href="{e(open_app)}">Open in SRIBEESonline app</a>
  <a class="btn secondary" href="{e(store)}">Get it on Google Play</a>
</div>
<script>
  if (/Android/i.test(navigator.userAgent)) {{
    window.location.replace(document.getElementById("open").href);
  }}
</script>
</body>
</html>"""
    return HTMLResponse(
        html,
        headers={"Cache-Control": f"public, max-age={_SHARE_CACHE_SECONDS}"},
    )


@router.get("/p/{product_id}/share.jpg")
async def product_share_card(product_id: str, db: AsyncSession = Depends(get_db)):
    """
    The product's share picture: the product over the brand swoosh and logo.

    Drawn the first time it is asked for and kept on disk, keyed by the photo
    and the card design, so every product -- old uploads included -- gets one
    without a backfill. A photo we cannot read (a legacy absolute URL, a
    missing file) sends the caller to the plain photo instead.
    """
    product = None
    try:
        product = await ProductService.get_by_id_or_slug(db, product_id)
    except Exception as exc:
        logger.warning(f"Share card lookup failed for {product_id!r}: {exc}")
    image_path = _primary_path(product) if product else None
    if not image_path:
        return Response(status_code=404)

    headers = {"Cache-Control": f"public, max-age={_CARD_CACHE_SECONDS}"}
    cached = _card_dir() / f"{_card_key(image_path)}.jpg"
    if cached.is_file():
        return FileResponse(cached, media_type="image/jpeg", headers=headers)

    fallback = media_url(image_path)
    source = await run_in_threadpool(get_storage().read, image_path)
    if source is None:
        return RedirectResponse(fallback, status_code=302)
    async with _render_slots:
        try:
            card = await run_in_threadpool(product_imagery.render_share_card, source)
        except product_imagery.ImageryError as exc:
            logger.warning(f"Share card for {product_id!r} not drawn: {exc}")
            return RedirectResponse(fallback, status_code=302)

    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        # Written aside and renamed in, so a concurrent request never serves
        # half a file.
        partial = cached.with_suffix(f".{uuid4().hex}.part")
        partial.write_bytes(card)
        partial.replace(cached)
    except OSError as exc:
        logger.warning(f"Share card cache write failed: {exc}")
    return Response(card, media_type="image/jpeg", headers=headers)
