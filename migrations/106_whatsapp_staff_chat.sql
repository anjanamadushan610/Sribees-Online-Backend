-- Preserve all existing tickets and durable queue entries.
ALTER TABLE whatsapp_conversations ADD COLUMN IF NOT EXISTS staff_active BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE whatsapp_replies ADD COLUMN IF NOT EXISTS inbound_text TEXT;
ALTER TABLE whatsapp_replies ADD COLUMN IF NOT EXISTS inbound_type VARCHAR(30);
ALTER TABLE whatsapp_replies ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ;
ALTER TABLE whatsapp_replies ADD COLUMN IF NOT EXISTS ticket_id UUID REFERENCES support_tickets(ticket_id) ON DELETE SET NULL;
ALTER TABLE whatsapp_replies ADD COLUMN IF NOT EXISTS admin_id UUID REFERENCES admins(admin_id) ON DELETE SET NULL;
ALTER TABLE whatsapp_replies ADD COLUMN IF NOT EXISTS is_staff BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE whatsapp_replies ADD COLUMN IF NOT EXISTS provider_message_id VARCHAR(255);
ALTER TABLE whatsapp_replies ADD COLUMN IF NOT EXISTS error_code VARCHAR(30);
CREATE INDEX IF NOT EXISTS ix_whatsapp_replies_ticket_id ON whatsapp_replies(ticket_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_whatsapp_replies_provider_message_id ON whatsapp_replies(provider_message_id);
