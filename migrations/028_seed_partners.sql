-- =============================================================================
-- 028_seed_partners.sql — Demo partners for the professional referral team.
--
-- Separate from 026 because `partners` does not exist until 027 creates it, and
-- migrations run in filename order. Seeding it from 026 would fail on a fresh
-- database with "relation partners does not exist".
--
-- Seeds a two-level recruitment tree, which is the only shape that exercises
-- the feature: one recruiter with agents beneath them, so a customer order can
-- pay level 1 to the referring agent and level 2 to whoever recruited them. A
-- flat list of unrelated partners would leave the level-2 path untested.
--
-- Runs on EVERY deploy, so it is idempotent. Existence is checked by hand
-- rather than with ON CONFLICT: `uq_partners_email` is an index on LOWER(email)
-- and `uq_partners_referral_code` is partial, and neither can arbitrate an
-- ON CONFLICT unless the statement restates the expression or the predicate
-- exactly — a guard that breaks quietly the next time either index is reshaped.
--
-- Development / QA database only. Password for every partner: Partner@123
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. The recruiter, then the agents beneath them
--
-- Inserted in two statements, not one: a partner's parent must already exist
-- for the foreign key to resolve, and a single VALUES list has no ordering
-- guarantee that would make that true.
--
-- The hash is argon2id with app/core/security.py's exact parameters, so these
-- accounts verify through the real login path.
-- -----------------------------------------------------------------------------
INSERT INTO partners (partner_id, email, password_hash, full_name, phone, referral_code, parent_partner_id, is_active)
SELECT
    md5('sribees:demo-partner:partner.lead@sribeesonline.lk')::uuid,
    'partner.lead@sribeesonline.lk',
    '$argon2id$v=19$m=65536,t=3,p=4$SinlfK8VYiylVEqJ8Z5zTg$u2R5kgBcxPF99nkMZcQ+pHDxCP4B8JKbBnXGn/a9vRQ',
    'Chandima Wijetunga',
    '+94771200001',
    'SBPLEAD01',
    NULL,
    TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM partners WHERE LOWER(email) = 'partner.lead@sribeesonline.lk'
);

INSERT INTO partners (partner_id, email, password_hash, full_name, phone, referral_code, parent_partner_id, is_active)
SELECT
    md5('sribees:demo-partner:' || v.email)::uuid,
    v.email,
    '$argon2id$v=19$m=65536,t=3,p=4$SinlfK8VYiylVEqJ8Z5zTg$u2R5kgBcxPF99nkMZcQ+pHDxCP4B8JKbBnXGn/a9vRQ',
    v.full_name,
    v.phone,
    v.referral_code,
    lead.partner_id,
    v.is_active
FROM (VALUES
    ('partner.agent1@sribeesonline.lk', 'Sajith Karunanayake', '+94771200002', 'SBAGENT01', TRUE),
    ('partner.agent2@sribeesonline.lk', 'Ruwanthi Alahakoon', '+94771200003', 'SBAGENT02', TRUE),
    ('partner.agent3@sribeesonline.lk', 'Manoj Wickramasinghe', '+94771200004', 'SBAGENT03', TRUE),
    -- One deactivated agent, so a screen that lists partners can be judged on
    -- whether it distinguishes active from inactive at all.
    ('partner.agent4@sribeesonline.lk', 'Yasodha Pieris',      '+94771200005', 'SBAGENT04', FALSE)
) AS v(email, full_name, phone, referral_code, is_active)
CROSS JOIN (
    SELECT partner_id FROM partners WHERE LOWER(email) = 'partner.lead@sribeesonline.lk'
) AS lead
WHERE NOT EXISTS (
    SELECT 1 FROM partners p WHERE LOWER(p.email) = v.email
);

-- -----------------------------------------------------------------------------
-- 2. Give the active agents some customers to have referred
--
-- Takes the LAST few demo customers, which 025 seeds as the recent joiners.
-- Reassigning long-standing customers would rewrite history the referral ledger
-- has already been computed against.
--
-- referred_by_user_id is cleared on the same row, which matters on a database
-- seeded by an earlier revision where the customer chain was written first: the
-- schema treats a customer as referred by a partner OR by another customer, and
-- leaving both set would claim two different people earned level-1 commission
-- on the same order.
-- -----------------------------------------------------------------------------
UPDATE users u
SET referred_by_partner_id = t.partner_id,
    referred_by_user_id    = NULL,
    referred_at            = COALESCE(u.referred_at, NOW() - INTERVAL '20 days'),
    updated_at             = NOW()
FROM (
    SELECT c.user_id, p.partner_id
    FROM (
        SELECT user_id, row_number() OVER (ORDER BY email DESC) AS rn
        FROM users
        WHERE email LIKE 'demo.customer%@sribees.test'
    ) c
    JOIN (
        SELECT partner_id, row_number() OVER (ORDER BY email) AS rn
        FROM partners
        WHERE LOWER(email) LIKE 'partner.agent%@sribeesonline.lk'
          AND is_active
    ) p ON p.rn = ((c.rn - 1) % 3) + 1
    WHERE c.rn <= 6
) t
WHERE u.user_id = t.user_id
  AND u.referred_by_partner_id IS NULL;

-- -----------------------------------------------------------------------------
-- 3. Commission for those partner-referred orders
--
-- Level 1 to the referring agent, level 2 to whoever recruited them. Status
-- tracks the order for the same reason it does in 026: nothing is owed on an
-- order that was cancelled or refunded.
--
-- The unique indexes here are partial (one per beneficiary kind), so existence
-- is again checked by hand rather than through ON CONFLICT.
-- -----------------------------------------------------------------------------
INSERT INTO referral_commissions (
    commission_id, order_id, order_number, beneficiary_user_id, beneficiary_partner_id,
    source_user_id, level, rate_percentage, order_subtotal, amount, status, created_at, updated_at
)
SELECT
    md5('sribees:demo-partner-commission:' || o.order_id::text || ':' || t.level)::uuid,
    o.order_id,
    o.order_number,
    NULL,
    t.partner_id,
    o.user_id,
    t.level,
    t.rate,
    o.subtotal,
    ROUND(o.subtotal * t.rate / 100, 2),
    CASE
        WHEN o.status IN ('cancelled', 'refunded') THEN 'reversed'
        WHEN o.status = 'delivered'                THEN 'claimable'
        ELSE 'pending'
    END,
    o.created_at,
    o.created_at
FROM orders o
JOIN users buyer ON buyer.user_id = o.user_id
CROSS JOIN LATERAL (
    -- The agent earns 3% directly; whoever recruited the agent earns 1% at the
    -- second tier. A partner with no recruiter simply produces no level-2 row.
    SELECT agent.partner_id, 1 AS level, 3.0 AS rate
    FROM partners agent
    WHERE agent.partner_id = buyer.referred_by_partner_id
    UNION ALL
    SELECT agent.parent_partner_id, 2, 1.0
    FROM partners agent
    WHERE agent.partner_id = buyer.referred_by_partner_id
      AND agent.parent_partner_id IS NOT NULL
) t
WHERE buyer.referred_by_partner_id IS NOT NULL
  AND o.created_at >= NOW() - INTERVAL '60 days'
  AND NOT EXISTS (
      SELECT 1 FROM referral_commissions rc
      WHERE rc.order_id = o.order_id
        AND rc.beneficiary_partner_id = t.partner_id
        AND rc.level = t.level
  );

-- Safety net for databases seeded by an earlier revision, where 026 wrote the
-- customer chain before partners existed: a customer who has since been moved
-- to a partner would otherwise carry commissions owed to two different people
-- for the same order.
DELETE FROM referral_commissions rc
USING users u
WHERE rc.source_user_id = u.user_id
  AND rc.beneficiary_user_id IS NOT NULL
  AND u.referred_by_partner_id IS NOT NULL;

COMMIT;
