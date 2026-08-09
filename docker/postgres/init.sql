-- =============================================================================
-- SRIBEESonline Database Initialization Script
-- Runs once, when the Postgres container's data directory is first created.
--
-- Schema itself is NOT created here: app/config/database.py's init_db() runs
-- SQLAlchemy's Base.metadata.create_all() against the ORM models on every
-- backend startup, then fastapi_backend/migrations/*.sql layers on the
-- additive changes. This file only needs to prepare the extensions those
-- rely on — everything below the old fence used to hand-write a whole
-- second copy of the schema under a `sribees` schema and repoint the
-- database's default search_path at it, which raced the real `public`
-- schema tables and broke `create_all()` on a fresh volume (a table like
-- `branches` would already exist here, minus the columns the ORM expects,
-- and FK creation on dependent tables like `banners` would fail outright).
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS vector;
