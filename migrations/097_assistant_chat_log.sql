-- Every customer chat turn in full, with what the customer then did. Kept with
-- no time limit: the owner treats this as a lasting business record. A closed
-- account's rows are deleted with it (account_deletion_service).
BEGIN;
CREATE TABLE IF NOT EXISTS assistant_chat_log (
 log_id UUID PRIMARY KEY,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 user_id UUID REFERENCES users(user_id) ON DELETE SET NULL,
 session_id VARCHAR(200) NOT NULL,
 branch_id UUID,
 language VARCHAR(2),
 answered_by VARCHAR(12),
 message TEXT NOT NULL,
 reply TEXT NOT NULL,
 had_image BOOLEAN NOT NULL DEFAULT FALSE,
 product_ids JSONB NOT NULL DEFAULT '[]',
 added_to_cart JSONB NOT NULL DEFAULT '[]',
 ordered JSONB NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_assistant_chat_log_user ON assistant_chat_log (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_assistant_chat_log_created ON assistant_chat_log (created_at DESC);
COMMIT;
