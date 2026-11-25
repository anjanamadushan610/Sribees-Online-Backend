# Product variants (shade / colour) — PLAN

Owner, 2026-11-25. Status: interview done, build not started.

## සාරාංශය (owner සඳහා)

එක product එකක් ඇතුළේ පාට/shade කිහිපයක්. Customer shade එක තෝරද්දී photo එකත් ඒ shade එකේ
photo එකට මාරු වෙනවා. සාමාන්‍යයෙන් හැම shade එකකටම එකම මිල, ඕන නම් එක shade එකකට වෙනම මිලක්.
Stock shade එකින් shade එකට වෙනම. පරණ app එකේ (1.2.9) product එක පේනවා, cart එකට දාන්න ගියොත්
"App එක update කරන්න" කියනවා. App, admin, website තුනම අපි හදනවා. Personal care products 48 +
shades 18 එකවර prod එකට (මිල excel එක ආවම), Matugama stock 100.

## Owner decisions (2026-11-25 interview)

| # | Question | Answer |
|---|---|---|
| 1 | Shades: real feature or separate products | Real variant feature; each shade its own photo; photo swaps when a shade is picked; a shade may have no photo of its own |
| 2 | Prices | Owner fills `Downloads/personal-care-prices.xlsx` (POS does not have them) |
| 3 | New sub category | Create "Makeup & Cosmetics" under Personal Care |
| 4 | Branch | Matugama on, stock 100 (global price) |
| 5 | Shade price | Same as the product by default; an override per shade is allowed |
| 6 | Old app (1.2.9) | Show the product; adding it says "update the app" |
| 7 | Website | We build the picker too (`anjana/support` branch of the storefront repo) |
| 8 | Rollout | Everything together: 45 plain products + 3 shade products in one load |

## What exists today (read 2026-11-25)

- `product_variants` table (name, sku, price NOT NULL, compare_at_price, stock_quantity, image_url,
  is_active, sort_order, weight_grams). Prod has **0** generic variants (weight_grams NULL).
- `GET /products/{id}` already returns generic variants (`has_variants`, `variants[]`) with
  `include_variants=True`; `POST /products/{id}/variants` exists (admin).
- Cart, checkout, OrderItem carry `variant_id` + `variant_name` (built for weight options).
  Checkout prices a variant only through `weight_options_service.price_map`; an unknown variant
  silently drops to the plain product (order_service.py ~L800).
- Stock: `stock_mode` unit/loose/packed; `packed` = per-size count in `branch_variant_inventory`.
  `stock_keeping.item()` downgrades a variant without grams to `unit`.
- Admin: `VariantManager.tsx` exists but is not used by `ProductForm`.
- Mobile: only weight pills; generic variants ignored. Web: nothing.

## Design

1. **Data (migration 123_product_options.sql, replay-safe):**
   - `product_variants.price` DROP NOT NULL — NULL = "follow the product" (branch price, discount,
     Quick Sale, cashback all as the product).
   - `products.option_label VARCHAR(20) NULL` — "Shade" / "Colour"; NOT NULL means the product
     *requires* a choice.
   - Shade products use `stock_mode = 'packed'`; stock per shade in `branch_variant_inventory`;
     `branch_inventory.stock_quantity` = sum (existing `refresh_summary`).
2. **Stock:** `stock_keeping.item()` keeps `packed` for a variant without grams (only `loose`
   needs grams). A packed product with no `variant_id` cannot be taken.
3. **Price (one function, used by product detail, cart and checkout):** a shade's price =
   branch override (`branch_variant_prices`) → `variant.price` → the product's effective branch
   price. The product's discount % applies to an override the same way it applies to the product.
   Extend `weight_options_service.price_map`/`regular_price_map` to cover generic options so
   checkout keeps a single pricing path.
4. **Guard:** cart add + checkout reject a product with `option_label` and no valid active
   `variant_id` → 409 `OPTION_REQUIRED`, message en/si/ta "Please update the app to choose a
   shade". Covers the old app, the chat bot's add-to-cart and any other client.
5. **API:** detail returns `option_label` and per option `{variant_id, name, image_url,
   price, regular_price, available, is_active}`; list/search cards return `option_count`.
6. **Photos:** a shade photo goes through `product_imagery.normalize_cutout` (white square WebP)
   into `product_variants.image_url`; no photo = the product's primary photo.
7. **Admin:** wire options into ProductForm (add/rename/reorder/deactivate, photo, optional
   price); branch inventory gets a per-shade stock modal (pattern of WeightStockModal).
   Deactivate, never delete (order lines point at it).
8. **Mobile (1.2.10, versionCode ≥ 20):** shade chips (photo thumbnails) on product details;
   picking swaps the main photo + price + stock; add-to-cart needs a pick; card shows
   "N shades" and opens details instead of quick add; cart/checkout/order details show the shade.
   en/si/ta strings.
9. **Web:** same picker on product page + cart line label (`anjana/support`).
10. **Staff side:** New Orders app + invoice show `variant_name` on the line (check, fix if not).

Out of scope: Supply/Finance stock push per shade (shade products are stocked from admin only;
Supply's push must skip `packed` products without sizes — verify it does not overwrite).

## Risk

Medium-high: touches the price and stock of an order line. Backend pricing/stock change gets a
separate fresh-context reviewer before main (max 3 rounds).

## Done = all of these pass

1. `pytest tests/test_product_options.py` (new): product with 3 shades; shade follows product
   price incl. branch price + discount; override price used; detail shows per-shade stock;
   checkout of shade B takes only B's stock and records `variant_name`; cancel gives it back;
   cart/checkout without a shade → 409 `OPTION_REQUIRED`; inactive shade rejected; two racing
   checkouts for the last unit of a shade → one fails.
2. Whole local suite green (run_suite.py), `ruff check .` clean.
3. Migration 123 replayed twice on a throwaway Postgres 18 with no error.
4. Admin: `npm run build` + vitest for the options editor.
5. Mobile: `flutter test` widget test — tapping a shade swaps the photo and price; add blocked
   until a pick; emulator screenshot of the picker.
6. Web: build passes; screenshot of the picker.
7. After the prod load: SQL check — 48 products under Personal Care with a photo, 3 with
   `option_label` and 18 active options (each with its own photo), all 48 active in Matugama with
   stock 100 (shades 100 each); every photo URL answers 200.
