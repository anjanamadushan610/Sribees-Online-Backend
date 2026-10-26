-- =============================================================================
-- 092 - A sold-by-weight product's weights follow its price per kg
-- =============================================================================
--
-- The product's price is its 1 kg price. From this release the app keeps the
-- weight options in step with it (weight_options_service), but options priced
-- before that drifted: White Sugar 1kg showed Rs 1,050 on its card and
-- Rs 1,000 on its 1 kg option after the Price app changed the product's price
-- (2026-10-26).
--
-- Where the active 1 kg option disagrees with the product's price, every
-- active weight option moves by the same ratio, so the 1 kg option lands on
-- the product's price and any per-weight markup is kept.
--
-- REPLAYED ON EVERY DEPLOY: a no-op once the 1 kg option matches.
-- =============================================================================

BEGIN;

WITH drift AS (
    SELECT p.product_id, p.price AS per_kg, v.price AS kilo
      FROM products p
      JOIN product_variants v ON v.product_id = p.product_id
     WHERE p.sold_by_weight
       AND v.weight_grams = 1000
       AND v.is_active IS NOT FALSE
       AND p.price > 0
       AND v.price > 0
       AND v.price <> p.price
)
UPDATE product_variants v
   SET price = CASE WHEN v.weight_grams = 1000 THEN d.per_kg
                    ELSE round(v.price * d.per_kg / d.kilo, 2) END
  FROM drift d
 WHERE v.product_id = d.product_id
   AND v.weight_grams IS NOT NULL
   AND v.is_active IS NOT FALSE;

COMMIT;
