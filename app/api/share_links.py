"""
SRIBEESonline - Product Share Links

Mounted at the site root (not under /api) because Android App Links require
exact, stable URLs:

  GET /.well-known/assetlinks.json  — Digital Asset Links proof that the app
                                      owns this host (enables autoVerify).
  GET /p/{product_id}?b={branch_id} — The link shoppers share on WhatsApp etc.
                                      ``b`` is the sharer's branch, shown as
                                      "from the Kandy branch" here and in the app.

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
from decimal import Decimal
from html import escape
from typing import Optional
from urllib.parse import quote, urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.media import media_url
from app.services import branch_service
from app.services.product_service import ProductService

router = APIRouter(include_in_schema=False)

_SHARE_CACHE_SECONDS = 300


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


def _primary_image(product) -> Optional[str]:
    images = sorted(
        product.images or [],
        key=lambda i: (not i.is_primary, i.sort_order or 0),
    )
    url = media_url(images[0].image_url) if images else None
    return url if url and url.startswith(("http://", "https://")) else None


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
    image = _primary_image(product) if product else None

    store = play_store_url(product_id, branch_id)
    open_app = intent_url(product_id, store, branch_id)
    canonical = str(request.url.replace(query=f"b={branch_id}" if branch_id else ""))

    e = lambda s: escape(s, quote=True)  # noqa: E731
    og_image = f'<meta property="og:image" content="{e(image)}">' if image else ""
    img_tag = f'<img src="{e(image)}" alt="">' if image else ""
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
