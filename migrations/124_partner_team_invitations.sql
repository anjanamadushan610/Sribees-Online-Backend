CREATE TABLE IF NOT EXISTS partner_team_invitations (
    invitation_id UUID PRIMARY KEY,
    master_id UUID NOT NULL REFERENCES partners(partner_id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    channel VARCHAR(5) NOT NULL CHECK (channel IN ('email', 'sms')),
    contact_stamp VARCHAR(64) NOT NULL,
    contact_hint VARCHAR(254) NOT NULL,
    delivery_state VARCHAR(8) NOT NULL CHECK (delivery_state IN ('sending', 'sent', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_partner_team_invitations_master_id ON partner_team_invitations(master_id);
