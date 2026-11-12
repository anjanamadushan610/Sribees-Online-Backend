-- First-order nudge (owner, 2026-11-12): one push, 48 h after sign-up, to a
-- customer who has not ordered yet. docs/plans/first-order-nudge.md.
-- Additive and replay-safe: every deploy re-applies every file.
--
-- first_order_nudges: one row per customer, ever. The row is written before the
-- push, so a retried sweep or a second API worker never sends twice.
--
-- first_order_nudge_state: one row, written by the worker the first time it
-- runs. Only customers who sign up after `since` are nudged ("new customers
-- only"). The worker writes it, not this file, because the app boots
-- (create_all) before the migrations run on a deploy.

CREATE TABLE IF NOT EXISTS first_order_nudges (
    user_id  UUID PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    variant  VARCHAR(20) NOT NULL,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS first_order_nudge_state (
    id     SMALLINT PRIMARY KEY CHECK (id = 1),
    since  TIMESTAMPTZ NOT NULL
);
