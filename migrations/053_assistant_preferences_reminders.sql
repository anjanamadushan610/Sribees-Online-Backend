BEGIN;
CREATE TABLE IF NOT EXISTS assistant_profiles (
    user_id uuid PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    preferences jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS assistant_reminders (
    reminder_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    product_id uuid NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    product_name varchar(255) NOT NULL,
    interval_days integer NOT NULL CHECK (interval_days BETWEEN 1 AND 90),
    language varchar(2) NOT NULL DEFAULT 'si',
    active boolean NOT NULL DEFAULT true,
    next_due_at timestamptz NOT NULL,
    last_sent_at timestamptz
);
CREATE INDEX IF NOT EXISTS ix_assistant_reminders_due ON assistant_reminders(active, next_due_at);
CREATE INDEX IF NOT EXISTS ix_assistant_reminders_user ON assistant_reminders(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_assistant_reminders_active_product
    ON assistant_reminders(user_id, product_id) WHERE active;
CREATE TABLE IF NOT EXISTS assistant_receipts (
    token uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
COMMIT;
