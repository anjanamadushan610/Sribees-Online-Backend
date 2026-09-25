-- =============================================================================
-- Migration 072: branch briefings for the AI shopping assistant.
--
-- A Branch Manager tells the assistant something about their branch for a
-- window of time -- "floods in Welipanna, no deliveries through there today",
-- "Avurudu festival at the branch on 10 April, everyone welcome" -- and the
-- assistant takes it into account when it answers that branch's customers.
--
-- source_text is what the manager uploaded (a .txt/.md file or pasted text),
-- kept for the record. digest is the short, fact-only version the assistant
-- actually reads on every turn -- prepared by the model at upload time, then
-- reviewed and possibly edited by the manager -- so a long file costs its
-- tokens once, not on every customer message.
--
-- mode: 'relevant' -> mentioned only when it bears on what the customer asked
--                     (a delivery disruption, when they ask about an order);
--       'announce' -> told once to each customer, alongside the answer to
--                     whatever they asked (an event, an invitation).
--
-- branch_id NULL means every branch (Super Admin only).
--
-- Idempotent -- the deploy applies this file again on each release.
-- =============================================================================

CREATE TABLE IF NOT EXISTS assistant_briefings (
    briefing_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id        UUID REFERENCES branches(branch_id) ON DELETE CASCADE,
    title            VARCHAR(120) NOT NULL,
    source_filename  VARCHAR(255),
    source_text      TEXT NOT NULL,
    digest           TEXT NOT NULL,
    digest_source    VARCHAR(10) NOT NULL DEFAULT 'ai',
    mode             VARCHAR(10) NOT NULL DEFAULT 'relevant',
    starts_at        TIMESTAMPTZ NOT NULL,
    ends_at          TIMESTAMPTZ NOT NULL,
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_by       UUID REFERENCES admins(admin_id) ON DELETE SET NULL,
    updated_by       UUID REFERENCES admins(admin_id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_assistant_briefings_window CHECK (ends_at > starts_at),
    CONSTRAINT ck_assistant_briefings_mode CHECK (mode IN ('relevant', 'announce')),
    CONSTRAINT ck_assistant_briefings_digest_source
        CHECK (digest_source IN ('ai', 'manager', 'fallback'))
);

CREATE INDEX IF NOT EXISTS ix_assistant_briefings_branch_window
    ON assistant_briefings (branch_id, ends_at, starts_at);

-- Permission to manage them: Branch Managers for their own branch, Super
-- Admin for any. Delegatable, so a manager can hand it to a staff role.
INSERT INTO permissions (resource, action)
VALUES
    ('briefings', 'read'),
    ('briefings', 'create'),
    ('briefings', 'update'),
    ('briefings', 'delete')
ON CONFLICT (resource, action) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM roles r
CROSS JOIN permissions p
WHERE r.system_key IN ('super_admin', 'branch_manager')
  AND p.resource = 'briefings'
ON CONFLICT DO NOTHING;
