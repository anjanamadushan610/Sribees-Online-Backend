-- =============================================================================
-- Migration 038: audit_logs table
--
-- AuditService (app/services/audit_service.py) has existed for a while but
-- was never wired into any endpoint, so its AuditLog model has never had a
-- migration — it only ever existed via SQLAlchemy create_all() on a fresh
-- dev DB. Migration 037's delegated-staff endpoints are the first real
-- caller (role/staff creation is logged via AuditService.log_admin_action),
-- so this needs to actually exist on pre-existing databases.
--
-- Schema is also created by SQLAlchemy Base.metadata.create_all() on fresh
-- DBs; run this against pre-existing databases. Safe to re-run.
-- =============================================================================

CREATE TABLE IF NOT EXISTS audit_logs (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at         TIMESTAMP NOT NULL DEFAULT NOW(),
    action             VARCHAR(100) NOT NULL,
    severity           VARCHAR(20) NOT NULL DEFAULT 'info',
    actor_type         VARCHAR(20) NOT NULL,
    actor_id           UUID,
    actor_email        VARCHAR(255),
    actor_ip           VARCHAR(45),
    actor_user_agent   VARCHAR(500),
    target_type        VARCHAR(50),
    target_id          UUID,
    description        TEXT,
    metadata           JSONB,
    request_id         VARCHAR(36),
    endpoint           VARCHAR(255),
    method             VARCHAR(10)
);

CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at);
CREATE INDEX IF NOT EXISTS ix_audit_logs_action ON audit_logs (action);
CREATE INDEX IF NOT EXISTS ix_audit_logs_actor_id ON audit_logs (actor_id);
CREATE INDEX IF NOT EXISTS ix_audit_logs_target_id ON audit_logs (target_id);
CREATE INDEX IF NOT EXISTS ix_audit_logs_action_created ON audit_logs (action, created_at);
CREATE INDEX IF NOT EXISTS ix_audit_logs_actor_created ON audit_logs (actor_id, created_at);
CREATE INDEX IF NOT EXISTS ix_audit_logs_target_created ON audit_logs (target_type, target_id, created_at);
