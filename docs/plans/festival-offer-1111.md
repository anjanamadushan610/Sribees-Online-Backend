# 11.11 festival offer (owner, 2026-11-10)

## Decisions
- Runs from the deploy until **2026-11-11 23:59 Sri Lanka time**, then switches off by itself.
- **Exactly 11% cashback** on every product, over product and branch rates (higher ones too).
  Products whose resolved rate is **0%** (sold by weight, eggs, coconuts) **stay 0%**.
- Every product picture shoppers see (app, web, share card) is drawn on the 11.11 design.
  English words only: "11.11 SALE" and an "11% CASHBACK" ribbon (no ribbon on 0% products).
- Nothing stored changes: product/branch rates and stored photos are untouched. Orders placed during
  the offer freeze 11% on their lines and settle by the usual held-cashback rules.

## Where
- `app/services/festival_offer.py` -- window, rate rule, picture URLs, kill switch `FESTIVAL_OFFER_DISABLED=1`.
- `app/services/offer_imagery.py` -- the design (Pillow; Poppins fonts, OFL, in `app/assets/offer`).
- `app/api/offer_images.py` -- `GET /offer/1111/{c|n}/uploads/products/...`, cached in `MEDIA_ROOT/offer-cards`.
- Rate applied in `ProductService.get_cashback_map` (checkout, quote, wishlist) and `format_product` (badge).
- `scripts/warm_offer_pictures.py` -- draw all pictures right after the deploy.

## Done = all pass
1. `tests/test_festival_offer.py` and the full suite green; `ruff check .` clean.
2. On the server after deploy: a product from `/api/v1/products` shows `cashback_percentage` 11 (0 for an egg)
   and an `/offer/1111/...` image URL; that URL returns `image/webp`.
3. After 2026-11-11 18:30 UTC: rates and URLs are back to the plain ones with no deploy.
