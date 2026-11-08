-- No existing customer or order tables are changed.
CREATE TABLE IF NOT EXISTS whatsapp_conversations (
    phone VARCHAR(20) PRIMARY KEY,
    language VARCHAR(2) NOT NULL DEFAULT 'en',
    awaiting_details BOOLEAN NOT NULL DEFAULT FALSE,
    ticket_id UUID REFERENCES support_tickets(ticket_id) ON DELETE SET NULL,
    last_message_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS whatsapp_replies (
    reply_id UUID PRIMARY KEY,
    message_id VARCHAR(255) NOT NULL UNIQUE,
    phone VARCHAR(20) NOT NULL,
    payload JSONB,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_whatsapp_replies_status ON whatsapp_replies(status);
