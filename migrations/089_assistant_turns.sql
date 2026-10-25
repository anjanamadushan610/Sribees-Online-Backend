-- One row per assistant chat turn: which layer answered, with what plan, how
-- fast and at what model cost. The hybrid assistant's learner mines it to
-- move more messages onto the free fast path (docs/assistant/HYBRID_ASSISTANT.md).
--
-- Privacy: no user id, name, phone, address or order number is stored. The
-- text is the *folded search key* of the message after scrubbing (see
-- app/services/hybrid/recorder.py); people are a salted hash, used only to
-- notice "the same person asked again right away". Safe to run any number of
-- times.

CREATE TABLE IF NOT EXISTS assistant_turns (
    turn_id        UUID PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    branch_id      UUID NULL,
    person_hash    VARCHAR(32) NULL,
    language       VARCHAR(2) NULL,
    text_key       TEXT NOT NULL,
    signature      VARCHAR(200) NULL,
    layer          VARCHAR(12) NOT NULL,
    plan           JSONB NOT NULL DEFAULT '{}'::jsonb,
    shadow_plan    JSONB NULL,
    model_trace    JSONB NULL,
    reply_kind     VARCHAR(40) NULL,
    latency_ms     INTEGER NULL,
    model_calls    SMALLINT NOT NULL DEFAULT 0,
    prompt_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    outcome        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_assistant_turns_created ON assistant_turns (created_at);
CREATE INDEX IF NOT EXISTS ix_assistant_turns_signature ON assistant_turns (signature);
CREATE INDEX IF NOT EXISTS ix_assistant_turns_layer ON assistant_turns (layer, created_at);
