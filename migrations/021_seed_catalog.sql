-- =============================================================================
-- 021_seed_catalog.sql — Demo catalog: 3 categories x 5 products, stocked in
-- every branch, with a few flagged into Quick Sale.
--
-- Runs on EVERY deploy (see deploy-backend-ec2.yml step 7b), so it must be
-- idempotent. Fixed UUIDs + ON CONFLICT DO UPDATE mean re-running refreshes the
-- rows rather than duplicating them, and never disturbs real data added later
-- through the admin panel (different ids, untouched by these upserts).
--
-- Branch inventory is what makes a product *visible* to shoppers: the storefront
-- joins products -> branch_inventory, so a product with no row here is invisible
-- even though it exists in the global catalog.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Categories
-- -----------------------------------------------------------------------------
-- ---------------------------------------------------------------------------
-- Demo seed: skipped on a database marked
--     ALTER DATABASE <db> SET sribees.demo_seed = 'off';
-- which production is. Deploys replay every migration, so without this the
-- seed re-creates demo customers, orders, admins and catalog rows every time
-- they are cleared. Unmarked databases (dev, QA, a fresh local one) still seed.
-- ---------------------------------------------------------------------------
SELECT coalesce(nullif(current_setting('sribees.demo_seed', true), ''), 'on') <> 'off' AS demo_seed \gset
\if :demo_seed
\else
\echo 'Skipped: sribees.demo_seed is off for this database.'
\quit
\endif

INSERT INTO categories (category_id, name, slug, description, is_active) VALUES
    ('c1000000-0000-4000-a000-000000000001', 'Fresh Produce', 'fresh-produce',
     'Fruits and vegetables sourced from local growers.', TRUE),
    ('c1000000-0000-4000-a000-000000000002', 'Dairy & Eggs', 'dairy-and-eggs',
     'Milk, curd, yoghurt, butter and farm eggs.', TRUE),
    ('c1000000-0000-4000-a000-000000000003', 'Pantry Staples', 'pantry-staples',
     'Rice, flour, sugar, spices and everyday cooking essentials.', TRUE)
ON CONFLICT (category_id) DO UPDATE SET
    name = EXCLUDED.name,
    slug = EXCLUDED.slug,
    description = EXCLUDED.description,
    is_active = TRUE,
    updated_at = NOW();

-- -----------------------------------------------------------------------------
-- 2. Products (5 per category)
--
-- `price` is the global base price; `discount_percentage` / `is_on_sale` are the
-- global Quick Sale defaults that branch_inventory can override per branch.
-- -----------------------------------------------------------------------------
INSERT INTO products (
    product_id, name, slug, description, short_description, sku,
    price, compare_at_price, category_id,
    stock_quantity, low_stock_threshold,
    weight, weight_unit,
    is_active, is_featured,
    discount_percentage, is_on_sale
) VALUES
    -- Fresh Produce ------------------------------------------------------------
    ('40000000-0000-4000-a000-000000000101', 'Ambul Banana (1 kg)', 'ambul-banana-1kg',
     'Sweet-tart Ambul bananas, picked ripe and packed the same day.',
     'Local Ambul bananas, 1 kg', 'FP-BAN-001',
     380.00, 420.00, 'c1000000-0000-4000-a000-000000000001',
     150, 20, 1.000, 'kg', TRUE, TRUE, NULL, FALSE),

    ('40000000-0000-4000-a000-000000000102', 'Tomato (1 kg)', 'tomato-1kg',
     'Firm salad tomatoes, graded and washed.',
     'Fresh tomatoes, 1 kg', 'FP-TOM-002',
     420.00, NULL, 'c1000000-0000-4000-a000-000000000001',
     120, 20, 1.000, 'kg', TRUE, FALSE, NULL, FALSE),

    ('40000000-0000-4000-a000-000000000103', 'Carrot (500 g)', 'carrot-500g',
     'Nuwara Eliya carrots, tender and sweet.',
     'Up-country carrots, 500 g', 'FP-CAR-003',
     260.00, 300.00, 'c1000000-0000-4000-a000-000000000001',
     100, 15, 0.500, 'kg', TRUE, FALSE, NULL, FALSE),

    ('40000000-0000-4000-a000-000000000104', 'Green Chilli (250 g)', 'green-chilli-250g',
     'Fresh green chillies with a clean, sharp heat.',
     'Green chillies, 250 g', 'FP-CHL-004',
     180.00, NULL, 'c1000000-0000-4000-a000-000000000001',
     80, 15, 0.250, 'kg', TRUE, FALSE, NULL, FALSE),

    ('40000000-0000-4000-a000-000000000105', 'King Coconut (each)', 'king-coconut-each',
     'Thambili picked young for maximum sweetness.',
     'King coconut, single', 'FP-KCO-005',
     150.00, NULL, 'c1000000-0000-4000-a000-000000000001',
     200, 30, 1.200, 'kg', TRUE, TRUE, NULL, FALSE),

    -- Dairy & Eggs -------------------------------------------------------------
    ('40000000-0000-4000-a000-000000000201', 'Fresh Milk (1 L)', 'fresh-milk-1l',
     'Pasteurised full-cream cow''s milk in a 1 litre bottle.',
     'Full-cream fresh milk, 1 L', 'DE-MLK-001',
     460.00, NULL, 'c1000000-0000-4000-a000-000000000002',
     90, 15, 1.030, 'kg', TRUE, TRUE, NULL, FALSE),

    ('40000000-0000-4000-a000-000000000202', 'Buffalo Curd (400 g)', 'buffalo-curd-400g',
     'Set buffalo curd in a traditional clay pot.',
     'Clay-pot buffalo curd, 400 g', 'DE-CRD-002',
     520.00, 580.00, 'c1000000-0000-4000-a000-000000000002',
     60, 10, 0.400, 'kg', TRUE, FALSE, NULL, FALSE),

    ('40000000-0000-4000-a000-000000000203', 'Farm Eggs (10 pack)', 'farm-eggs-10-pack',
     'Brown table eggs from free-range hens, carton of ten.',
     'Free-range brown eggs, 10s', 'DE-EGG-003',
     620.00, NULL, 'c1000000-0000-4000-a000-000000000002',
     110, 20, 0.600, 'kg', TRUE, TRUE, NULL, FALSE),

    ('40000000-0000-4000-a000-000000000204', 'Set Yoghurt (4 cups)', 'set-yoghurt-4-cups',
     'Lightly sweetened set yoghurt, pack of four cups.',
     'Set yoghurt, 4 cups', 'DE-YOG-004',
     340.00, NULL, 'c1000000-0000-4000-a000-000000000002',
     75, 12, 0.320, 'kg', TRUE, FALSE, NULL, FALSE),

    ('40000000-0000-4000-a000-000000000205', 'Butter (200 g)', 'butter-200g',
     'Salted dairy butter, foil-wrapped 200 g block.',
     'Salted butter, 200 g', 'DE-BTR-005',
     890.00, 950.00, 'c1000000-0000-4000-a000-000000000002',
     50, 10, 0.200, 'kg', TRUE, FALSE, NULL, FALSE),

    -- Pantry Staples -----------------------------------------------------------
    ('40000000-0000-4000-a000-000000000301', 'Red Raw Rice (5 kg)', 'red-raw-rice-5kg',
     'Unpolished red raw rice, cleaned and double-sieved.',
     'Red raw rice, 5 kg', 'PS-RIC-001',
     1450.00, 1600.00, 'c1000000-0000-4000-a000-000000000003',
     70, 10, 5.000, 'kg', TRUE, TRUE, NULL, FALSE),

    ('40000000-0000-4000-a000-000000000302', 'White Sugar (1 kg)', 'white-sugar-1kg',
     'Fine-grain refined white sugar.',
     'White sugar, 1 kg', 'PS-SUG-002',
     280.00, NULL, 'c1000000-0000-4000-a000-000000000003',
     140, 20, 1.000, 'kg', TRUE, FALSE, NULL, FALSE),

    ('40000000-0000-4000-a000-000000000303', 'Red Lentils (1 kg)', 'red-lentils-1kg',
     'Split red lentils (mysoor dhal), cleaned and graded.',
     'Red lentils, 1 kg', 'PS-DHL-003',
     640.00, 700.00, 'c1000000-0000-4000-a000-000000000003',
     95, 15, 1.000, 'kg', TRUE, FALSE, NULL, FALSE),

    ('40000000-0000-4000-a000-000000000304', 'Coconut Oil (750 ml)', 'coconut-oil-750ml',
     'Cold-pressed virgin coconut oil in a 750 ml bottle.',
     'Virgin coconut oil, 750 ml', 'PS-OIL-004',
     1250.00, NULL, 'c1000000-0000-4000-a000-000000000003',
     55, 10, 0.700, 'kg', TRUE, TRUE, NULL, FALSE),

    ('40000000-0000-4000-a000-000000000305', 'Curry Powder (250 g)', 'curry-powder-250g',
     'Roasted Sri Lankan curry powder, freshly milled.',
     'Roasted curry powder, 250 g', 'PS-CRY-005',
     390.00, NULL, 'c1000000-0000-4000-a000-000000000003',
     85, 15, 0.250, 'kg', TRUE, FALSE, NULL, FALSE)
ON CONFLICT (product_id) DO UPDATE SET
    name = EXCLUDED.name,
    slug = EXCLUDED.slug,
    description = EXCLUDED.description,
    short_description = EXCLUDED.short_description,
    sku = EXCLUDED.sku,
    price = EXCLUDED.price,
    compare_at_price = EXCLUDED.compare_at_price,
    category_id = EXCLUDED.category_id,
    stock_quantity = EXCLUDED.stock_quantity,
    low_stock_threshold = EXCLUDED.low_stock_threshold,
    weight = EXCLUDED.weight,
    weight_unit = EXCLUDED.weight_unit,
    is_active = TRUE,
    is_featured = EXCLUDED.is_featured,
    updated_at = NOW();

-- -----------------------------------------------------------------------------
-- 3. Branch inventory — stock every seeded product in every branch.
--
-- branch_price / discount_percentage stay NULL so each branch inherits the
-- global price, which is what a Marketing Manager then overrides per branch.
-- The Quick Sale flags below are deliberately *different per branch* so the
-- hyperlocal behaviour is visible: the same product is on sale in one branch
-- and not another.
--
-- DO NOTHING (not DO UPDATE) on conflict: once a row exists, a Marketing
-- Manager owns its price/discount/Quick-Sale state, and the next deploy must
-- not silently revert their work back to these seed values.
-- -----------------------------------------------------------------------------
INSERT INTO branch_inventory (
    inventory_id, product_id, branch_id,
    branch_price, stock_quantity, reserved_quantity, low_stock_threshold,
    discount_percentage, is_on_sale, is_active
)
SELECT
    gen_random_uuid(),
    p.product_id,
    b.branch_id,
    NULL,
    60,
    0,
    10,
    s.discount_percentage,                  -- NULL for rows not in the list below
    COALESCE(s.is_on_sale, FALSE),
    TRUE
FROM products p
CROSS JOIN branches b
LEFT JOIN (VALUES
    -- (product_id, branch_code, discount %, on quick sale)
    ('40000000-0000-4000-a000-000000000101'::uuid, 'CMB', 15.0, TRUE),
    ('40000000-0000-4000-a000-000000000103'::uuid, 'CMB', 20.0, TRUE),
    ('40000000-0000-4000-a000-000000000205'::uuid, 'CMB', 10.0, TRUE),
    ('40000000-0000-4000-a000-000000000301'::uuid, 'CMB', 12.0, TRUE),

    ('40000000-0000-4000-a000-000000000202'::uuid, 'KDY', 18.0, TRUE),
    ('40000000-0000-4000-a000-000000000303'::uuid, 'KDY', 15.0, TRUE),
    ('40000000-0000-4000-a000-000000000304'::uuid, 'KDY', 10.0, TRUE),

    ('40000000-0000-4000-a000-000000000105'::uuid, 'GLE', 25.0, TRUE),
    ('40000000-0000-4000-a000-000000000203'::uuid, 'GLE', 12.0, TRUE),
    ('40000000-0000-4000-a000-000000000302'::uuid, 'GLE', 8.0,  TRUE)
) AS s(product_id, branch_code, discount_percentage, is_on_sale)
    ON s.product_id = p.product_id AND s.branch_code = b.code
WHERE p.product_id IN (
    '40000000-0000-4000-a000-000000000101','40000000-0000-4000-a000-000000000102',
    '40000000-0000-4000-a000-000000000103','40000000-0000-4000-a000-000000000104',
    '40000000-0000-4000-a000-000000000105','40000000-0000-4000-a000-000000000201',
    '40000000-0000-4000-a000-000000000202','40000000-0000-4000-a000-000000000203',
    '40000000-0000-4000-a000-000000000204','40000000-0000-4000-a000-000000000205',
    '40000000-0000-4000-a000-000000000301','40000000-0000-4000-a000-000000000302',
    '40000000-0000-4000-a000-000000000303','40000000-0000-4000-a000-000000000304',
    '40000000-0000-4000-a000-000000000305'
)
ON CONFLICT (product_id, branch_id) DO NOTHING;
