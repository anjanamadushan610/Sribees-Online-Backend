-- =============================================================================
-- 025_seed_demo_data.sql — Delivery coverage + a realistic order history, so
-- the Dashboard and the Analytics report have something true to draw.
--
-- Empty tables are the reason analytics screens get signed off as "working"
-- and then look broken on day one: every chart renders, every chart is blank.
-- This file fills the gap end to end — post office coverage, demo customers
-- with addresses, and ~120 days of orders spread across every branch.
--
-- Runs on EVERY deploy (deploy-backend-ec2.yml step 7b), so it is idempotent
-- by construction:
--
--   * Every id is derived with md5(...) from a stable natural key, not
--     gen_random_uuid(), so the same logical row always lands on the same id.
--   * Order ids key off the ABSOLUTE calendar date, never an offset from
--     today. That matters: keying off "N days ago" would freeze the dataset at
--     whichever date it was first seeded, and a month later the "last 30 days"
--     view would be empty again. Keyed off the date, each deploy simply tops up
--     the days that have passed since the last one.
--   * ON CONFLICT DO NOTHING everywhere, so a re-run never disturbs rows that
--     already exist — including any real order placed through the app.
--
-- Intended for the development / QA database. Every demo account uses the
-- reserved .test TLD (RFC 2606) and the shared password Demo@1234.
-- =============================================================================

BEGIN;

-- Deterministic non-negative hash, used everywhere below in place of random().
-- md5 (not the built-in hashtext) because hashtext is an undocumented internal
-- whose output has changed between major PostgreSQL versions — that would make
-- a server upgrade silently regenerate a different dataset. Created in pg_temp
-- so it disappears with the session and leaves nothing behind in the schema.
CREATE OR REPLACE FUNCTION pg_temp.demo_hash(t text) RETURNS int
LANGUAGE sql IMMUTABLE STRICT AS
$$ SELECT ('x' || substr(md5(t), 1, 7))::bit(28)::int $$;

-- -----------------------------------------------------------------------------
-- 1. Post Office directory + branch coverage
--
-- Checkout resolves the fulfilling branch by looking the delivery address's
-- post_office up in post_office_branch_mapping. An unmapped post office yields
-- an order with branch_id = NULL, which then belongs to no branch in any
-- report — so coverage here is what keeps branch-scoped analytics complete.
-- -----------------------------------------------------------------------------
INSERT INTO post_office_directory (id, post_office, district, province, is_active)
SELECT gen_random_uuid(), v.post_office, v.district, v.province, TRUE
FROM (VALUES
    ('Colombo 03',   'Colombo',    'Western'),
    ('Colombo 05',   'Colombo',    'Western'),
    ('Colombo 07',   'Colombo',    'Western'),
    ('Dehiwala',     'Colombo',    'Western'),
    ('Moratuwa',     'Colombo',    'Western'),
    ('Kandy',        'Kandy',      'Central'),
    ('Peradeniya',   'Kandy',      'Central'),
    ('Katugastota',  'Kandy',      'Central'),
    ('Galle',        'Galle',      'Southern'),
    ('Hikkaduwa',    'Galle',      'Southern'),
    ('Ambalangoda',  'Galle',      'Southern'),
    ('Negombo',      'Gampaha',    'Western'),
    ('Ja-Ela',       'Gampaha',    'Western'),
    ('Gampaha',      'Gampaha',    'Western'),
    ('Kurunegala',   'Kurunegala', 'North Western'),
    ('Kuliyapitiya', 'Kurunegala', 'North Western'),
    ('Narammala',    'Kurunegala', 'North Western')
) AS v(post_office, district, province)
ON CONFLICT (post_office) DO NOTHING;

INSERT INTO post_office_branch_mapping (
    mapping_id, post_office, branch_id, branch_name, district, province, is_active
)
SELECT gen_random_uuid(), v.post_office, b.branch_id, b.name, b.district, b.province, TRUE
FROM (VALUES
    ('Colombo 03',   'CMB'),
    ('Colombo 05',   'CMB'),
    ('Colombo 07',   'CMB'),
    ('Dehiwala',     'CMB'),
    ('Moratuwa',     'CMB'),
    ('Kandy',        'KDY'),
    ('Peradeniya',   'KDY'),
    ('Katugastota',  'KDY'),
    ('Galle',        'GLE'),
    ('Hikkaduwa',    'GLE'),
    ('Ambalangoda',  'GLE'),
    ('Negombo',      'NGB'),
    ('Ja-Ela',       'NGB'),
    ('Gampaha',      'NGB'),
    ('Kurunegala',   'KUR'),
    ('Kuliyapitiya', 'KUR'),
    ('Narammala',    'KUR')
) AS v(post_office, branch_code)
JOIN branches b ON b.code = v.branch_code
ON CONFLICT (post_office) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 2. Demo customers
--
-- 40 accounts, deterministically spread across the five branches by their
-- delivery post office. Enough distinct buyers that "active customers",
-- "new customers" and the top-customers ranking all have something to rank.
-- -----------------------------------------------------------------------------
CREATE TEMP TABLE _demo_customer ON COMMIT DROP AS
WITH names(n, full_name) AS (VALUES
    ( 1, 'Nimal Perera'),      ( 2, 'Kumari Silva'),      ( 3, 'Sunil Fernando'),
    ( 4, 'Anusha Jayawardena'),( 5, 'Ruwan Bandara'),     ( 6, 'Dilani Wickrama'),
    ( 7, 'Chaminda Rathnayake'),( 8, 'Sanduni Gunasekara'),( 9, 'Pradeep Weerasinghe'),
    (10, 'Malsha Dissanayake'),(11, 'Tharindu Alwis'),    (12, 'Ishara Madushani'),
    (13, 'Kasun Herath'),      (14, 'Nadeesha Peiris'),   (15, 'Buddhika Senanayake'),
    (16, 'Hasini Ekanayake'),  (17, 'Lahiru Abeysekara'), (18, 'Shanika Rodrigo'),
    (19, 'Gayan Kodikara'),    (20, 'Piyumi Wijesuriya'), (21, 'Nuwan Samarasinghe'),
    (22, 'Thilini Karunaratne'),(23, 'Asanka Liyanage'),  (24, 'Chathurika Mendis'),
    (25, 'Roshan de Silva'),   (26, 'Upeksha Rajapaksa'), (27, 'Janaka Amarasinghe'),
    (28, 'Sewwandi Nawarathna'),(29, 'Dinesh Kulatunga'), (30, 'Amaya Gunawardena'),
    (31, 'Suresh Pathirana'),  (32, 'Nilanka Jayasuriya'),(33, 'Mahesh Ranasinghe'),
    (34, 'Kaveesha Dias'),     (35, 'Isuru Wijeratne'),   (36, 'Tharushi Kumarasiri'),
    (37, 'Danushka Perera'),   (38, 'Rashmi Attanayake'), (39, 'Charith Balasooriya'),
    (40, 'Nethmi Wanigasekara')
),
-- Post office per customer, cycling through each branch's coverage so buyers
-- land in every branch rather than piling into one.
zones(z, post_office, district, province, branch_code) AS (VALUES
    (0, 'Colombo 03',   'Colombo',    'Western',       'CMB'),
    (1, 'Kandy',        'Kandy',      'Central',       'KDY'),
    (2, 'Galle',        'Galle',      'Southern',      'GLE'),
    (3, 'Negombo',      'Gampaha',    'Western',       'NGB'),
    (4, 'Kurunegala',   'Kurunegala', 'North Western', 'KUR'),
    (5, 'Dehiwala',     'Colombo',    'Western',       'CMB'),
    (6, 'Peradeniya',   'Kandy',      'Central',       'KDY'),
    (7, 'Hikkaduwa',    'Galle',      'Southern',      'GLE'),
    (8, 'Ja-Ela',       'Gampaha',    'Western',       'NGB'),
    (9, 'Kuliyapitiya', 'Kurunegala', 'North Western', 'KUR')
)
SELECT
    md5('sribees:demo-customer:' || n.n)::uuid                    AS user_id,
    md5('sribees:demo-address:'  || n.n)::uuid                    AS address_id,
    n.full_name,
    'demo.customer' || lpad(n.n::text, 2, '0') || '@sribees.test' AS email,
    '+9477' || lpad((1000000 + n.n)::text, 7, '0')                AS phone,
    z.post_office,
    z.district,
    z.province,
    b.branch_id
FROM names n
JOIN zones z ON z.z = (n.n - 1) % 10
JOIN branches b ON b.code = z.branch_code;

-- Shared argon2id hash of 'Demo@1234' — the same algorithm/parameters
-- app.core.security uses, so these accounts can actually sign in on the app.
--
-- Every NOT NULL flag is listed explicitly. The ORM declares their defaults
-- Python-side (`default=`, not `server_default=`), so the column carries no
-- DEFAULT in the database and raw SQL like this has to supply the value itself
-- or the insert fails on the NOT NULL constraint.
INSERT INTO users (
    user_id, email, password_hash, full_name, phone,
    is_verified, is_phone_verified, two_factor_enabled, is_active, role,
    created_at, updated_at
)
SELECT
    c.user_id,
    c.email,
    '$argon2id$v=19$m=65536,t=3,p=4$G0NIaQ0BwJizlhKidE6pFQ$v0DIXmUswbDV8qjs6/a9emwzg5AUY4RgfDM1mn+pUu8',
    c.full_name,
    c.phone,
    TRUE, TRUE, FALSE, TRUE, 'customer',
    NOW() - INTERVAL '150 days',
    NOW()
FROM _demo_customer c
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO addresses (
    address_id, user_id, title, recipient_name, phone,
    address_line1, post_office, district, postal_code, province, is_default
)
SELECT
    c.address_id, c.user_id, 'Home', c.full_name, c.phone,
    'No. ' || (10 + (pg_temp.demo_hash((c.email)) % 180)) || ', Main Street',
    c.post_office, c.district, '00000', c.province, TRUE
FROM _demo_customer c
ON CONFLICT (address_id) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 3. Order history — the last 120 days, every branch
--
-- Daily volume is shaped rather than uniform, because a flat line makes the
-- charts useless for judging whether they work: branches differ in size, the
-- weekend is busier than midweek, and there is a mild upward trend so the
-- period-over-period comparison in the UI has a real delta to report.
--
-- All "randomness" is hashed from the row's own key, so the same date always
-- produces the same orders — that is what makes a re-run a no-op.
-- -----------------------------------------------------------------------------
CREATE TEMP TABLE _demo_order ON COMMIT DROP AS
WITH cal AS (
    SELECT (CURRENT_DATE - offs)::date AS order_date, offs
    FROM generate_series(0, 119) AS offs
),
branch AS (
    -- Matched by `code`, not the branch_id 010_seed_branches.sql assigns:
    -- production already had CMB/KDY/GLE created through the admin UI (with
    -- app-generated UUIDs) before that migration existed, and its ON CONFLICT
    -- intentionally never rewrites branch_id on an existing row (FKs from
    -- branch_inventory/orders/banners would cascade). Filtering on the
    -- b0000000-... placeholders here only ever matched NGB and KUR, the two
    -- branches that genuinely were created fresh by that migration — Colombo,
    -- Kandy and Galle silently got zero orders for it, despite carrying the
    -- heaviest base_volume below.
    SELECT branch_id, code,
           CASE code
               WHEN 'CMB' THEN 9   -- flagship
               WHEN 'KDY' THEN 6
               WHEN 'GLE' THEN 5
               WHEN 'NGB' THEN 4
               WHEN 'KUR' THEN 3   -- newest, smallest
               ELSE 2
           END AS base_volume
    FROM branches
    WHERE code IN ('CMB', 'KDY', 'GLE', 'NGB', 'KUR')
),
volume AS (
    SELECT
        cal.order_date,
        b.branch_id,
        b.code,
        b.base_volume
          + CASE WHEN EXTRACT(DOW FROM cal.order_date) IN (0, 6) THEN 2 ELSE 0 END
          + ((119 - cal.offs) / 45)                               -- gentle growth
          + (pg_temp.demo_hash((b.code || cal.order_date::text)) % 3)    -- day-to-day noise
          AS order_count
    FROM cal
    CROSS JOIN branch b
),
slots AS (
    SELECT v.order_date, v.branch_id, v.code, s AS slot
    FROM volume v
    CROSS JOIN LATERAL generate_series(1, v.order_count) AS s
)
SELECT
    md5('sribees:demo-order:' || s.code || ':' || s.order_date::text || ':' || s.slot)::uuid AS order_id,
    'SO-' || to_char(s.order_date, 'YYYYMMDD') || '-' || s.code || '-' || lpad(s.slot::text, 3, '0') AS order_number,
    s.branch_id,
    s.order_date,
    -- Spread through the trading day (08:00–20:00) rather than all at midnight.
    (s.order_date
        + make_interval(hours => 8 + (pg_temp.demo_hash(('h' || s.code || s.order_date::text || s.slot)) % 12),
                        mins  => pg_temp.demo_hash(('m' || s.code || s.order_date::text || s.slot)) % 60)
    )::timestamptz AS created_at,
    pg_temp.demo_hash(('r' || s.code || s.order_date::text || s.slot)) % 100 AS roll,
    pg_temp.demo_hash(('c' || s.code || s.order_date::text || s.slot))       AS cust_roll,
    s.slot
FROM slots s;

-- Attach a buyer: customers already served by this branch, so the address's
-- post office and the fulfilling branch stay consistent with each other.
CREATE TEMP TABLE _demo_order_customer ON COMMIT DROP AS
WITH ranked AS (
    SELECT c.*, row_number() OVER (PARTITION BY c.branch_id ORDER BY c.user_id) - 1 AS rn,
           count(*)        OVER (PARTITION BY c.branch_id)                          AS n
    FROM _demo_customer c
),
cohort AS (
    -- The last two customers of each branch do not start ordering until 20 days
    -- ago. Without a joining date every customer is active in every window, so
    -- "new customers" reads 0 and the customer delta reads 0.0% forever — two
    -- KPIs that look broken precisely because the data is too uniform.
    SELECT r.*,
           CASE WHEN r.rn >= r.n - 2 THEN CURRENT_DATE - 20 ELSE CURRENT_DATE - 200 END
               AS joined_on
    FROM ranked r
),
eligible AS (
    SELECT o.order_id,
           c.user_id,
           c.address_id,
           row_number() OVER (PARTITION BY o.order_id ORDER BY c.user_id) - 1 AS erank,
           count(*)     OVER (PARTITION BY o.order_id)                        AS ecount
    FROM _demo_order o
    JOIN cohort c
      ON c.branch_id = o.branch_id
     AND c.joined_on <= o.order_date
)
SELECT o.*, e.user_id, e.address_id
FROM _demo_order o
JOIN eligible e ON e.order_id = o.order_id AND e.erank = (o.cust_roll % e.ecount);

-- Line items: 1–4 distinct products per order, priced at the branch's price
-- when it overrides the global one (that is what the customer actually paid).
CREATE TEMP TABLE _demo_item ON COMMIT DROP AS
WITH catalog AS (
    SELECT
        p.product_id, p.name, p.sku,
        COALESCE(bi.branch_price, p.price) AS unit_price,
        bi.branch_id,
        row_number() OVER (PARTITION BY bi.branch_id ORDER BY p.sku) - 1 AS pidx,
        count(*)     OVER (PARTITION BY bi.branch_id)                    AS pcount
    FROM products p
    JOIN branch_inventory bi ON bi.product_id = p.product_id
    WHERE p.is_active
),
picks AS (
    SELECT
        o.order_id,
        o.branch_id,
        k AS line_no,
        1 + (pg_temp.demo_hash(('q' || o.order_id::text || k)) % 4) AS quantity,
        pg_temp.demo_hash(('p' || o.order_id::text || k))           AS product_roll
    FROM _demo_order_customer o
    CROSS JOIN LATERAL generate_series(1, 1 + (pg_temp.demo_hash(('n' || o.order_id::text)) % 4)) AS k
)
SELECT DISTINCT ON (picks.order_id, c.product_id)
    picks.order_id,
    c.product_id,
    c.name        AS product_name,
    c.sku         AS product_sku,
    picks.quantity,
    c.unit_price,
    (picks.quantity * c.unit_price)::numeric(10, 2) AS subtotal
FROM picks
JOIN catalog c
  ON c.branch_id = picks.branch_id
 AND c.pidx = (picks.product_roll % c.pcount)
-- The ORDER BY is what makes DISTINCT ON deterministic: when two lines of the
-- same order happen to pick the same product, the lower line number always
-- wins. Without it PostgreSQL is free to keep an arbitrary one, and the seed
-- would stop reproducing itself byte for byte.
ORDER BY picks.order_id, c.product_id, picks.line_no;

-- -----------------------------------------------------------------------------
-- 4. Insert the orders
--
-- Status mix is bucketed off `roll` (0–99) to a plausible funnel: most orders
-- complete, a few are still moving, and a small tail is cancelled or refunded.
-- Analytics counts revenue as paid AND not cancelled/refunded, so the tail is
-- what proves the exclusion is actually working.
-- -----------------------------------------------------------------------------
INSERT INTO orders (
    order_id, user_id, order_number, branch_id,
    subtotal, tax_amount, shipping_amount, discount_amount,
    wallet_deduction, cashback_earned, total_amount,
    status, payment_status, payment_method,
    delivery_address_id, created_at, updated_at, delivered_at, shipped_at, cancelled_at
)
SELECT
    o.order_id,
    o.user_id,
    o.order_number,
    o.branch_id,
    t.subtotal,
    0,
    350.00,
    t.discount,
    0,
    ROUND((t.subtotal - t.discount) * 0.01, 2),          -- 1% platform cashback
    (t.subtotal + 350.00 - t.discount)::numeric(10, 2),
    t.status,
    t.payment_status,
    CASE WHEN o.roll % 3 = 0 THEN 'cash_on_delivery' ELSE 'card' END,
    o.address_id,
    o.created_at,
    o.created_at,
    CASE WHEN t.status = 'delivered' THEN o.created_at + INTERVAL '2 days' END,
    CASE WHEN t.status IN ('delivered', 'shipped', 'out_for_delivery')
         THEN o.created_at + INTERVAL '1 day' END,
    CASE WHEN t.status = 'cancelled' THEN o.created_at + INTERVAL '6 hours' END
FROM _demo_order_customer o
JOIN (
    SELECT
        i.order_id,
        SUM(i.subtotal)::numeric(10, 2) AS subtotal,
        -- Roughly one order in six carries a 10% promo discount.
        CASE WHEN pg_temp.demo_hash(('d' || i.order_id::text)) % 6 = 0
             THEN ROUND(SUM(i.subtotal) * 0.10, 2) ELSE 0 END AS discount,
        CASE
            WHEN o2.roll < 68 THEN 'delivered'
            WHEN o2.roll < 76 THEN 'out_for_delivery'
            WHEN o2.roll < 82 THEN 'shipped'
            WHEN o2.roll < 88 THEN 'processing'
            WHEN o2.roll < 92 THEN 'confirmed'
            WHEN o2.roll < 96 THEN 'pending'
            WHEN o2.roll < 99 THEN 'cancelled'
            ELSE 'refunded'
        END AS status,
        CASE
            WHEN (o2.roll % 3 = 0) AND o2.roll >= 68 AND o2.roll < 99 THEN 'pending'
            WHEN o2.roll < 88 THEN 'paid'
            WHEN o2.roll < 92 THEN 'paid'
            WHEN o2.roll < 96 THEN 'pending'
            WHEN o2.roll < 99 THEN 'failed'
            ELSE 'refunded'
        END AS payment_status
    FROM _demo_item i
    JOIN _demo_order_customer o2 ON o2.order_id = i.order_id
    GROUP BY i.order_id, o2.roll
) t ON t.order_id = o.order_id
ON CONFLICT (order_id) DO NOTHING;

-- Items go in only for orders that actually landed, so a re-run (where the
-- order already existed and DO NOTHING skipped it) cannot bolt a second set of
-- line items onto an order and silently double its item counts.
INSERT INTO order_items (
    order_item_id, order_id, product_id,
    product_name, product_sku, quantity, unit_price, subtotal, tax_amount, created_at
)
SELECT
    md5('sribees:demo-item:' || i.order_id::text || ':' || i.product_id::text)::uuid,
    i.order_id,
    i.product_id,
    i.product_name,
    i.product_sku,
    i.quantity,
    i.unit_price,
    i.subtotal,
    0,
    o.created_at
FROM _demo_item i
JOIN orders o ON o.order_id = i.order_id
WHERE NOT EXISTS (
    SELECT 1 FROM order_items oi WHERE oi.order_id = i.order_id
);

-- -----------------------------------------------------------------------------
-- 5. Push a slice of the shelves into low stock
--
-- 021 stocks every branch/product pair at a flat 60 against a threshold of 10,
-- so "Low Stock Alerts" reads 0 forever and neither the Dashboard KPI nor the
-- per-branch alert column can be told apart from a broken query.
--
-- The `stock_quantity = 60` guard is the safety catch: only rows still sitting
-- at the untouched seed value are moved. The moment an Inventory Manager
-- adjusts a level, that row stops matching and the next deploy leaves it alone.
-- -----------------------------------------------------------------------------
UPDATE branch_inventory
SET stock_quantity = 2 + (pg_temp.demo_hash(inventory_id::text) % 7),
    updated_at = NOW()
WHERE stock_quantity = 60
  AND pg_temp.demo_hash('lowstock:' || product_id::text || branch_id::text) % 6 = 0;

COMMIT;
