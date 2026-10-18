-- 079_user_phone_change_history.sql
-- Audit log and rate limiting tracking for customer primary phone number updates.

CREATE TABLE IF NOT EXISTS user_phone_change_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    old_phone VARCHAR(20),
    new_phone VARCHAR(20) NOT NULL,
    ip_address VARCHAR(45),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_phone_change_user_time ON user_phone_change_history(user_id, changed_at DESC);
