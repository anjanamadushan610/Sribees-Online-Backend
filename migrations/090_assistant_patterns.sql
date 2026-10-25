-- What the hybrid assistant has learned from the model (docs/assistant/HYBRID_ASSISTANT.md, P2).
--
-- One row per learned message: the scrubbed, folded text key and the plan
-- the model consistently chose for it. A pattern starts in 'shadow' (its plan
-- is compared with the model's on live turns), becomes 'active' (answers by
-- itself, free) once it agrees often enough, and goes back to 'shadow' if
-- canary re-checks start disagreeing. 'disabled' is set by an admin and is
-- never changed automatically. No personal data: the key is the same
-- scrubbed form assistant_turns keeps. Safe to run any number of times.

CREATE TABLE IF NOT EXISTS assistant_patterns (
    pattern_id       UUID PRIMARY KEY,
    text_key         TEXT NOT NULL UNIQUE,
    plan             JSONB NOT NULL,
    status           VARCHAR(12) NOT NULL DEFAULT 'shadow',
    source           VARCHAR(20) NOT NULL DEFAULT 'model',
    examples         INTEGER NOT NULL DEFAULT 0,
    shadow_agree     INTEGER NOT NULL DEFAULT 0,
    shadow_disagree  INTEGER NOT NULL DEFAULT 0,
    canary_agree     INTEGER NOT NULL DEFAULT 0,
    canary_disagree  INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    promoted_at      TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS ix_assistant_patterns_status ON assistant_patterns (status);
