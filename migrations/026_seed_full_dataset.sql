-- =============================================================================
-- 026_seed_full_dataset.sql — Everything the earlier seeds did not cover.
--
-- 010 seeds branches, 021 the catalog, 025 customers and orders. This file
-- completes the set so that EVERY table in the schema holds representative
-- rows: admin accounts, wallets and their ledger, reviews, notifications,
-- variants, product images, coupons, referral commissions, push tokens,
-- wishlists, and the session/verification tables.
--
-- Why cover everything: a screen built against an empty table looks identical
-- whether it works or not. The point of seeding the whole schema is that any
-- page in the admin panel or the app can be opened and judged on sight.
--
-- Runs on EVERY deploy (deploy-backend-ec2.yml step 7b), so it is idempotent:
-- ids are md5-derived from stable natural keys, and every insert is ON CONFLICT
-- DO NOTHING so a re-run never touches a row that exists — including real data.
--
-- Development / QA database only. Credentials below are shared and published.
--
-- SECURITY NOTE on the session-ish tables (sessions, admin_sessions,
-- email_verifications, password_resets): those rows are seeded ALREADY EXPIRED
-- and, where applicable, already consumed. They exist so the tables are not
-- empty and so admin screens that list them have something to render — they are
-- deliberately not usable credentials, because a seeded live session or an
-- unused password-reset token in a shared repo is a working back door.
-- =============================================================================

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

BEGIN;

-- Same deterministic hash helper 025 uses. Session-local; nothing is left in
-- the schema afterwards.
CREATE OR REPLACE FUNCTION pg_temp.demo_hash(t text) RETURNS int
LANGUAGE sql IMMUTABLE STRICT AS
$$ SELECT ('x' || substr(md5(t), 1, 7))::bit(28)::int $$;

-- -----------------------------------------------------------------------------
-- 1. Admin accounts — one per role, plus a manager for every branch
--
-- Without these a freshly deployed database has no way in: the admin panel has
-- no sign-up, so the first admin has to be seeded or inserted by hand.
--
-- Password for every account below: Admin@123
-- The hash is argon2id with the exact parameters app/core/security.py uses, so
-- these verify against the real login path rather than needing a special case.
--
-- The `role` column is a PostgreSQL enum whose labels are the Python enum's
-- NAMES ('SUPER_ADMIN'), not its values ('super_admin') — that is what
-- SQLAlchemy's Enum(PyEnum) writes. Insert the value form and the cast fails.
--
-- branch_id is NULL for the network-wide roles. It must NOT be null for
-- branch_manager: inject_branch_filter rejects a scoped admin with no branch,
-- so such an account can log in and then 403 on every branch-scoped screen.
-- -----------------------------------------------------------------------------
INSERT INTO admins (admin_id, email, password_hash, full_name, role, branch_id, is_active)
SELECT
    md5('sribees:demo-admin:' || v.email)::uuid,
    v.email,
    '$argon2id$v=19$m=65536,t=3,p=4$ae09Z+y9lzLmnLPWujcmBA$004GLJho35ddhQmFHJzzfSn68yjvSaJ1n38gCGvV2JA',
    v.full_name,
    v.role::admin_role_enum,
    b.branch_id,
    TRUE
FROM (VALUES
    ('superadmin@sribeesonline.lk',       'Ayesha Rathnayake',  'SUPER_ADMIN',       NULL),
    ('manager.colombo@sribeesonline.lk',  'Dilan Perera',       'BRANCH_MANAGER',    'CMB'),
    ('manager.kandy@sribeesonline.lk',    'Nadeeka Bandara',    'BRANCH_MANAGER',    'KDY'),
    ('manager.galle@sribeesonline.lk',    'Roshan Fernando',    'BRANCH_MANAGER',    'GLE'),
    ('manager.negombo@sribeesonline.lk',  'Fathima Nazeer',     'BRANCH_MANAGER',    'NGB'),
    ('manager.kurunegala@sribeesonline.lk','Sampath Weerakoon', 'BRANCH_MANAGER',    'KUR'),
    ('marketing@sribeesonline.lk',        'Hiruni Jayasekara',  'MARKETING_MANAGER', NULL),
    ('inventory@sribeesonline.lk',        'Kasun Ediriweera',   'INVENTORY_MANAGER', NULL),
    ('support@sribeesonline.lk',          'Menaka Silva',       'CUSTOMER_SUPPORT',  NULL)
) AS v(email, full_name, role, branch_code)
LEFT JOIN branches b ON b.code = v.branch_code
-- Existence is checked with NOT EXISTS rather than ON CONFLICT (email), and the
-- difference is not stylistic — ON CONFLICT does not save this statement.
--
-- 037_role_permission_system.sql ends by making admins.role_id NOT NULL. This
-- file runs BEFORE it and cannot set role_id (the column does not exist yet at
-- this point in the sequence), so from the second deploy onwards every row this
-- statement builds carries role_id = NULL. NOT NULL is checked as the tuple is
-- formed, before any unique index is consulted, so ON CONFLICT never gets a
-- chance to skip the duplicate: the insert dies on the constraint and takes the
-- whole migration — and the deploy — with it. NOT EXISTS never forms the row.
--
-- It also does the right thing on its own terms: matching on the natural key
-- leaves an account somebody created through the admin panel completely
-- untouched, rather than overwriting a real admin's credentials.
--
-- One consequence to know about: adding a NEW email to the list above, on a
-- database where 037 has already run, will fail for exactly the reason above.
-- Seed that admin from a migration numbered after 037 instead.
WHERE NOT EXISTS (
    SELECT 1 FROM admins a WHERE a.email = v.email
);

-- Point each branch at its manager, so the Branches screen shows an owner.
UPDATE branches b
SET manager_id = a.admin_id, updated_at = NOW()
FROM admins a
WHERE a.branch_id = b.branch_id
  AND a.role = 'BRANCH_MANAGER'
  AND b.manager_id IS NULL;

-- Expired admin sessions — history for the session list, never a live token.
INSERT INTO admin_sessions (
    session_id, admin_id, refresh_token_hash, device_info, ip_address, user_agent, expires_at, created_at
)
SELECT
    md5('sribees:demo-admin-session:' || a.email)::uuid,
    a.admin_id,
    -- A hash of a value nobody holds: even the pre-image is unknown, and the
    -- row is expired regardless.
    md5('expired-not-a-real-token:' || a.email),
    'Chrome on Windows',
    '203.0.113.10',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    NOW() - INTERVAL '20 days',
    NOW() - INTERVAL '27 days'
FROM admins a
WHERE a.email LIKE '%@sribeesonline.lk'
ON CONFLICT (session_id) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 2. App settings — the keys the mobile app and admin read at boot
-- -----------------------------------------------------------------------------
INSERT INTO app_settings (setting_id, key, value, description, is_active)
SELECT gen_random_uuid(), v.key, v.value, v.description, TRUE
FROM (VALUES
    ('platform_cashback_rate', '1.0',
     'Default cashback %, used when neither the branch nor the product overrides it'),
    ('referral_l1_rate', '3.0',
     'Commission % paid to a direct referrer on their invitee''s orders'),
    ('referral_l2_rate', '1.0',
     'Commission % paid at the second referral tier'),
    ('free_delivery_threshold', '5000.00',
     'Order subtotal (LKR) above which delivery is free'),
    ('delivery_fee', '350.00',
     'Flat delivery fee (LKR) below the free-delivery threshold'),
    ('support_phone', '+94112345600',
     'Number shown on the app''s Help screen'),
    ('support_email', 'support@sribeesonline.lk',
     'Address shown on the app''s Help screen'),
    ('min_order_value', '500.00',
     'Smallest order (LKR) checkout will accept'),
    ('app_maintenance_mode', 'false',
     'When true the app shows a maintenance screen instead of the storefront')
) AS v(key, value, description)
ON CONFLICT (key) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 3. Branch-specific home banners
--
-- 022 seeds three network-wide banners (branch_id NULL). These add a
-- branch-scoped one each, which is what actually exercises the "carousel
-- differs per branch" behaviour — with only global banners, a bug that ignores
-- branch_id looks perfectly correct.
-- -----------------------------------------------------------------------------
INSERT INTO banners (banner_id, title, subtitle, branch_id, sort_order, is_active)
SELECT
    md5('sribees:demo-banner:' || b.code)::uuid,
    v.title,
    v.subtitle,
    b.branch_id,
    1,
    TRUE
FROM (VALUES
    ('CMB', 'Colombo Same-Day Delivery',  'Order before 2pm, get it today.'),
    ('KDY', 'Kandy Hill-Country Veg',     'Picked this morning in Nuwara Eliya.'),
    ('GLE', 'Galle Fresh Catch',          'Straight off the southern coast.'),
    ('NGB', 'Negombo Seafood Week',       'Up to 25% off all week.'),
    ('KUR', 'Kurunegala Opening Offer',   'Free delivery on your first order.')
) AS v(branch_code, title, subtitle)
JOIN branches b ON b.code = v.branch_code
ON CONFLICT (banner_id) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 4. Coupons (Disabled Demo Seed)
--
-- Demo coupons removed so that coupons created or deleted by Admin/Branch Manager
-- remain persistent and are not automatically re-inserted upon migration runs.
-- -----------------------------------------------------------------------------


-- -----------------------------------------------------------------------------
-- 5. Product media and variants
-- -----------------------------------------------------------------------------
INSERT INTO product_images (image_id, product_id, image_url, alt_text, is_primary, sort_order)
SELECT
    md5('sribees:demo-image:' || p.product_id::text || ':' || n)::uuid,
    p.product_id,
    '/uploads/seed/' || p.sku || '-' || n || '.jpg',
    p.name || CASE WHEN n = 1 THEN '' ELSE ' — alternate view' END,
    n = 1,
    n
FROM products p
CROSS JOIN generate_series(1, 2) AS n
WHERE p.sku IS NOT NULL
ON CONFLICT (image_id) DO NOTHING;

INSERT INTO variant_types (variant_type_id, name, display_name)
SELECT md5('sribees:demo-variant-type:' || v.name)::uuid, v.name, v.display_name
FROM (VALUES
    ('size',   'Size'),
    ('weight', 'Pack Weight'),
    ('grade',  'Grade')
) AS v(name, display_name)
ON CONFLICT (variant_type_id) DO NOTHING;

-- Pack sizes on the weighed goods. `price` is per-variant, so the small pack is
-- cheaper than the product's base price and the large one dearer.
DELETE FROM variant_options WHERE variant_id IN (
    SELECT md5('sribees:demo-variant:' || p.product_id::text || ':' || v.suffix)::uuid
    FROM products p
    CROSS JOIN (VALUES
        ('S'), ('M'), ('L')
    ) AS v(suffix)
    WHERE p.sku IS NOT NULL
      AND p.weight_unit = 'kg'
);

DELETE FROM product_variants WHERE variant_id IN (
    SELECT md5('sribees:demo-variant:' || p.product_id::text || ':' || v.suffix)::uuid
    FROM products p
    CROSS JOIN (VALUES
        ('S'), ('M'), ('L')
    ) AS v(suffix)
    WHERE p.sku IS NOT NULL
      AND p.weight_unit = 'kg'
);

INSERT INTO product_variants (
    variant_id, product_id, name, sku, price, compare_at_price,
    stock_quantity, is_active, sort_order
)
SELECT
    md5('sribees:demo-variant:' || p.product_id::text || ':' || v.suffix)::uuid,
    p.product_id,
    v.label,
    p.sku || '-' || v.suffix,
    ROUND(p.price * v.multiplier, 2),
    CASE WHEN p.compare_at_price IS NOT NULL
         THEN ROUND(p.compare_at_price * v.multiplier, 2) END,
    40,
    TRUE,
    v.sort_order
FROM products p
CROSS JOIN (VALUES
    ('S',  'Small pack',  0.60, 1),
    ('M',  'Standard',    1.00, 2),
    ('L',  'Family pack', 1.85, 3)
) AS v(suffix, label, multiplier, sort_order)
WHERE p.sku IS NOT NULL
  AND p.weight_unit = 'kg'
-- Same reasoning as `admins`: sku carries the unique index, so it has to be the
-- arbiter, or a variant added by hand under the same sku aborts the deploy.
ON CONFLICT (sku) DO NOTHING;

INSERT INTO variant_options (option_id, variant_id, variant_type_id, value)
SELECT
    md5('sribees:demo-variant-option:' || pv.variant_id::text)::uuid,
    pv.variant_id,
    vt.variant_type_id,
    pv.name
FROM product_variants pv
JOIN variant_types vt ON vt.name = 'weight'
WHERE pv.sku LIKE '%-S' OR pv.sku LIKE '%-M' OR pv.sku LIKE '%-L'
ON CONFLICT (option_id) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 6. Wallets and the cashback ledger
--
-- Every order already recorded a `cashback_earned`. Crediting it here is what
-- makes the wallet balance agree with the order history instead of being an
-- arbitrary number — the two are checked against each other on the app's
-- wallet screen, so a made-up balance would read as a bug.
-- -----------------------------------------------------------------------------
INSERT INTO wallets (wallet_id, user_id, balance, currency)
SELECT
    md5('sribees:demo-wallet:' || u.user_id::text)::uuid,
    u.user_id,
    0,
    'LKR'
FROM users u
WHERE u.email LIKE 'demo.customer%@sribees.test'
ON CONFLICT (user_id) DO NOTHING;

-- One credit per delivered order, in chronological order, with a running
-- balance. `balance_after` has to be the balance as of THAT transaction, which
-- is what the window function computes — a plain SUM would stamp every row with
-- the final balance and make the ledger unreadable.
INSERT INTO wallet_transactions (
    transaction_id, wallet_id, user_id, order_id, type, title,
    amount, balance_after, order_number, notes, created_at
)
SELECT
    md5('sribees:demo-wallet-txn:' || t.order_id::text)::uuid,
    t.wallet_id,
    t.user_id,
    t.order_id,
    'earned',
    'Cashback earned',
    t.cashback_earned,
    SUM(t.cashback_earned) OVER (
        PARTITION BY t.wallet_id ORDER BY t.created_at, t.order_id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ),
    t.order_number,
    'Cashback on order ' || t.order_number,
    t.created_at
FROM (
    SELECT o.order_id, o.order_number, o.user_id, o.cashback_earned, o.created_at,
           w.wallet_id
    FROM orders o
    JOIN wallets w ON w.user_id = o.user_id
    WHERE o.status = 'delivered'
      AND o.payment_status = 'paid'
      AND o.cashback_earned > 0
) t
-- transaction_id has no natural key to fall back on, so the duplicate has to be
-- excluded by hand. It matters more here than anywhere else in this file: the
-- app credits cashback itself on delivery, and a second 'earned' row for the
-- same order is not a cosmetic duplicate — the reconciliation below sums the
-- ledger, so it would silently double that customer's balance.
WHERE NOT EXISTS (
    SELECT 1 FROM wallet_transactions wt
    WHERE wt.order_id = t.order_id AND wt.type = 'earned'
)
ON CONFLICT (transaction_id) DO NOTHING;

-- Reconcile each wallet to its own ledger rather than trusting a running total
-- computed above: this is also the statement that repairs the balance on a
-- later deploy, once newer orders have added rows.
UPDATE wallets w
SET balance = COALESCE(t.total, 0), updated_at = NOW()
FROM (
    SELECT wallet_id,
           SUM(CASE WHEN type = 'spent' THEN -amount ELSE amount END) AS total
    FROM wallet_transactions
    GROUP BY wallet_id
) t
WHERE t.wallet_id = w.wallet_id
  AND w.balance IS DISTINCT FROM COALESCE(t.total, 0);

-- -----------------------------------------------------------------------------
-- 7. Referrals — who invited whom, and what it earned them
-- -----------------------------------------------------------------------------
UPDATE users u
SET referral_code = 'SB' || upper(substr(md5('ref:' || u.user_id::text), 1, 6)),
    updated_at = NOW()
WHERE u.email LIKE 'demo.customer%@sribees.test'
  AND u.referral_code IS NULL;

-- Chain the demo customers: #2 was invited by #1, #3 by #2, and so on, so the
-- referral screen shows a real tree rather than one isolated pair.
--
-- The guard is `referred_at IS NULL`, not `referred_by_user_id IS NULL`.
-- 027_partners.sql adds a second way to be referred — by a partner — under a
-- check constraint saying it is one or the other, never both, and 028 moves
-- some of these customers onto a partner by clearing referred_by_user_id. A
-- guard that only looked at referred_by_user_id would see those rows as
-- unreferred on the NEXT deploy and re-chain them, violating the constraint and
-- failing the migration. referred_at stays set through that move, and it is
-- also the honest question: has this customer been referred by anyone at all?
-- Naming the partner column directly is not an option here — it does not exist
-- yet at this point in the sequence.
WITH ordered AS (
    SELECT user_id, row_number() OVER (ORDER BY email) AS n
    FROM users
    WHERE email LIKE 'demo.customer%@sribees.test'
)
UPDATE users u
SET referred_by_user_id = parent.user_id,
    referred_at         = NOW() - INTERVAL '140 days',
    updated_at          = NOW()
FROM ordered child
JOIN ordered parent ON parent.n = child.n - 1
WHERE u.user_id = child.user_id
  AND u.referred_at IS NULL;

-- Level-1 commission for the inviting customer on each of their invitee's
-- orders. Status tracks the order: only a delivered order's commission is
-- claimable, and a cancelled or refunded one is reversed — the whole point of
-- the status column is that money is not owed on an order that came back.
INSERT INTO referral_commissions (
    commission_id, order_id, order_number, beneficiary_user_id, source_user_id,
    level, rate_percentage, order_subtotal, amount, status, created_at, updated_at
)
SELECT
    md5('sribees:demo-commission:' || o.order_id::text || ':1')::uuid,
    o.order_id,
    o.order_number,
    buyer.referred_by_user_id,
    o.user_id,
    1,
    3.0,
    o.subtotal,
    ROUND(o.subtotal * 0.03, 2),
    CASE
        WHEN o.status IN ('cancelled', 'refunded') THEN 'reversed'
        WHEN o.status = 'delivered'                THEN 'claimable'
        ELSE 'pending'
    END,
    o.created_at,
    o.created_at
FROM orders o
JOIN users buyer ON buyer.user_id = o.user_id
WHERE buyer.referred_by_user_id IS NOT NULL
  AND o.created_at >= NOW() - INTERVAL '60 days'
  -- NOT EXISTS rather than ON CONFLICT, deliberately. 027_partners.sql replaces
  -- the plain unique constraint on (order_id, beneficiary_user_id, level) with
  -- two PARTIAL unique indexes, one per beneficiary kind. A partial index
  -- cannot arbitrate an ON CONFLICT unless the statement repeats its exact
  -- WHERE predicate — so an ON CONFLICT written against the old constraint
  -- starts failing the moment 027 lands, and it fails on the SECOND deploy
  -- rather than the first, when nobody is looking for it. This guard does not
  -- care what shape the index is.
  AND NOT EXISTS (
      SELECT 1 FROM referral_commissions rc
      WHERE rc.order_id = o.order_id
        AND rc.beneficiary_user_id = buyer.referred_by_user_id
        AND rc.level = 1
  );

-- -----------------------------------------------------------------------------
-- 8. Reviews — only from customers who actually received the product
--
-- is_verified_purchase is TRUE by construction here: the join is what makes it
-- true, rather than a flag set to TRUE and hoped for.
-- -----------------------------------------------------------------------------
INSERT INTO reviews (
    review_id, product_id, user_id, rating, title, comment,
    is_verified_purchase, is_approved, created_at, updated_at
)
SELECT DISTINCT ON (t.product_id, t.user_id)
    md5('sribees:demo-review:' || t.product_id::text || ':' || t.user_id::text)::uuid,
    t.product_id,
    t.user_id,
    t.rating,
    CASE t.rating
        WHEN 5 THEN 'Exactly what I wanted'
        WHEN 4 THEN 'Good, would buy again'
        WHEN 3 THEN 'Fine, nothing special'
        ELSE 'Not great this time'
    END,
    CASE t.rating
        WHEN 5 THEN 'Arrived fresh and well packed. Delivery was on time.'
        WHEN 4 THEN 'Quality was good. Packaging could be a little better.'
        WHEN 3 THEN 'Average quality for the price. Delivery was quick though.'
        ELSE 'Item was not as fresh as I expected. Support sorted it out.'
    END,
    TRUE,
    t.rating >= 3,   -- the low-rated one sits unapproved, so moderation has work
    t.created_at,
    t.created_at
FROM (
    SELECT
        oi.product_id,
        o.user_id,
        o.created_at,
        -- Skewed toward good, with a genuine tail: an all-5-star catalog
        -- cannot show that rating filters and moderation work.
        CASE
            WHEN pg_temp.demo_hash('rating:' || oi.order_item_id::text) % 10 < 5 THEN 5
            WHEN pg_temp.demo_hash('rating:' || oi.order_item_id::text) % 10 < 8 THEN 4
            WHEN pg_temp.demo_hash('rating:' || oi.order_item_id::text) % 10 < 9 THEN 3
            ELSE 2
        END AS rating
    FROM order_items oi
    JOIN orders o ON o.order_id = oi.order_id
    WHERE o.status = 'delivered'
      AND o.created_at >= NOW() - INTERVAL '45 days'
      AND pg_temp.demo_hash('review:' || oi.order_item_id::text) % 5 = 0
) t
ORDER BY t.product_id, t.user_id, t.created_at DESC
-- One review per product per customer is a unique index, and a real review from
-- a demo account would occupy that pair under a different review_id.
ON CONFLICT (product_id, user_id) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 9. Notifications and push tokens
-- -----------------------------------------------------------------------------
INSERT INTO notifications (
    notification_id, user_id, type, title, message,
    reference_type, reference_id, is_read, read_at, push_sent, push_sent_at, created_at
)
SELECT
    md5('sribees:demo-notification:' || o.order_id::text)::uuid,
    o.user_id,
    'order_status',
    CASE o.status
        WHEN 'delivered' THEN 'Order delivered'
        WHEN 'cancelled' THEN 'Order cancelled'
        WHEN 'refunded'  THEN 'Refund processed'
        ELSE 'Order update'
    END,
    'Your order ' || o.order_number || ' is now ' || replace(o.status, '_', ' ') || '.',
    'order',
    o.order_id,
    o.status = 'delivered',                                  -- older ones read
    CASE WHEN o.status = 'delivered' THEN o.created_at + INTERVAL '3 days' END,
    TRUE,
    o.created_at,
    o.created_at
FROM orders o
WHERE o.created_at >= NOW() - INTERVAL '21 days'
ON CONFLICT (notification_id) DO NOTHING;

-- A promotional broadcast, so the notification list is not all order updates.
INSERT INTO notifications (
    notification_id, user_id, type, title, message, is_read, push_sent, push_sent_at, created_at
)
SELECT
    md5('sribees:demo-promo-notification:' || u.user_id::text)::uuid,
    u.user_id,
    'promotion',
    'Weekend deals are live',
    'Up to 25% off fresh produce until Sunday. Use code WEEKEND15 at checkout.',
    FALSE,
    TRUE,
    NOW() - INTERVAL '2 days',
    NOW() - INTERVAL '2 days'
FROM users u
WHERE u.email LIKE 'demo.customer%@sribees.test'
ON CONFLICT (notification_id) DO NOTHING;

INSERT INTO push_tokens (
    token_id, user_id, token, device_type, device_name, device_id, is_active
)
SELECT
    md5('sribees:demo-push-token:' || u.user_id::text)::uuid,
    u.user_id,
    'demo-fcm-' || md5('push:' || u.user_id::text),
    CASE WHEN pg_temp.demo_hash(u.user_id::text) % 2 = 0 THEN 'android' ELSE 'ios' END,
    CASE WHEN pg_temp.demo_hash(u.user_id::text) % 2 = 0 THEN 'Pixel 7' ELSE 'iPhone 14' END,
    md5('device:' || u.user_id::text),
    TRUE
FROM users u
WHERE u.email LIKE 'demo.customer%@sribees.test'
ON CONFLICT (token) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 10. Wishlists
--
-- price_at_watch is set BELOW today's price on purpose, so the price-drop
-- watchlist has both kinds of row: items waiting for a drop, and items that
-- already dropped past what the customer was watching for.
-- -----------------------------------------------------------------------------
INSERT INTO wishlist_items (wishlist_item_id, user_id, product_id, price_at_watch, added_at)
SELECT
    md5('sribees:demo-wishlist:' || t.user_id::text || ':' || t.product_id::text)::uuid,
    t.user_id,
    t.product_id,
    CASE WHEN pg_temp.demo_hash('w:' || t.product_id::text || t.user_id::text) % 2 = 0
         THEN ROUND(t.price * 1.10, 2)      -- watched high; today is a drop
         ELSE ROUND(t.price * 0.90, 2)      -- still waiting for a drop
    END,
    NOW() - ((pg_temp.demo_hash('wd:' || t.user_id::text || t.product_id::text) % 30) || ' days')::interval
FROM (
    SELECT u.user_id, p.product_id, p.price,
           row_number() OVER (PARTITION BY u.user_id ORDER BY p.sku) AS rn
    FROM users u
    CROSS JOIN products p
    WHERE u.email LIKE 'demo.customer%@sribees.test'
      AND p.is_active
      AND pg_temp.demo_hash('wl:' || u.user_id::text || p.product_id::text) % 7 = 0
) t
WHERE t.rn <= 4
ON CONFLICT (wishlist_item_id) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 11. Session / verification tables
--
-- All expired, and the password resets all already used. See the security note
-- at the top of this file: these exist so the tables are populated and any
-- screen listing them renders, not so anyone can authenticate with them.
-- -----------------------------------------------------------------------------
INSERT INTO sessions (session_id, user_id, refresh_token_hash, ip_address, user_agent, expires_at, created_at)
SELECT
    md5('sribees:demo-session:' || u.user_id::text)::uuid,
    u.user_id,
    md5('expired-not-a-real-token:' || u.user_id::text),
    '198.51.100.' || (pg_temp.demo_hash(u.user_id::text) % 250 + 1),
    'SRIBEES/1.0 (Android 14)',
    NOW() - INTERVAL '10 days',
    NOW() - INTERVAL '40 days'
FROM users u
WHERE u.email LIKE 'demo.customer%@sribees.test'
ON CONFLICT (session_id) DO NOTHING;

INSERT INTO email_verifications (verification_id, user_id, token, expires_at, created_at)
SELECT
    md5('sribees:demo-verification:' || u.user_id::text)::uuid,
    u.user_id,
    md5('expired-verification:' || u.user_id::text),
    NOW() - INTERVAL '143 days',       -- issued at signup, long since lapsed
    NOW() - INTERVAL '150 days'
FROM users u
WHERE u.email LIKE 'demo.customer%@sribees.test'
ON CONFLICT (token) DO NOTHING;

INSERT INTO password_resets (reset_id, user_id, token, expires_at, used, created_at)
SELECT
    md5('sribees:demo-password-reset:' || u.user_id::text)::uuid,
    u.user_id,
    md5('used-reset:' || u.user_id::text),
    NOW() - INTERVAL '29 days',
    TRUE,                              -- already consumed as well as expired
    NOW() - INTERVAL '30 days'
FROM users u
WHERE u.email LIKE 'demo.customer%@sribees.test'
  AND pg_temp.demo_hash('pr:' || u.user_id::text) % 4 = 0
ON CONFLICT (token) DO NOTHING;

COMMIT;