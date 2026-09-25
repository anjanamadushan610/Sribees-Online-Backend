"""
Migration Runner for 011_create_post_office_branch_mapping.sql

Executes the SQL migration against the configured PostgreSQL database.

Usage:
    cd fastapi_backend
    python -m migrations.run_migration_011
"""
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from app.config.database import engine
from app.utils.logger import logger

MIGRATION_FILE = Path(__file__).parent / "011_create_post_office_branch_mapping.sql"


async def run_migration() -> None:
    """Execute the SQL migration file."""
    logger.info("Starting migration 011: Post Office Branch Mapping + Marketing Manager role")

    sql_content = MIGRATION_FILE.read_text(encoding="utf-8")

    async with engine.begin() as conn:
        # Split on COMMIT/BEGIN to handle the transaction ourselves
        # (SQLAlchemy async uses its own transaction via engine.begin())
        # Strip the explicit BEGIN/COMMIT since engine.begin() wraps in a txn
        clean_sql = sql_content.replace("BEGIN;", "").replace("COMMIT;", "").strip()

        # Execute each statement block
        # The DO $$ blocks and CREATE statements need to be executed individually
        statements = _split_sql_statements(clean_sql)

        for i, stmt in enumerate(statements, 1):
            stmt = stmt.strip()
            if not stmt or stmt.startswith("--"):
                continue
            try:
                await conn.execute(text(stmt))
                logger.info(f"  Statement {i} executed successfully")
            except Exception as e:
                logger.error(f"  Statement {i} failed: {e}")
                raise

    logger.info("Migration 011 completed successfully")


def _split_sql_statements(sql: str) -> list[str]:
    """
    Split SQL into executable statement blocks.

    Handles DO $$ ... $$ blocks, CREATE TABLE, CREATE INDEX,
    CREATE OR REPLACE FUNCTION, CREATE TRIGGER, and COMMENT ON statements.
    """
    statements: list[str] = []
    current = []
    in_dollar_block = False

    for line in sql.split("\n"):
        stripped = line.strip()

        # Track $$ block boundaries (DO $$, $$ LANGUAGE, etc.)
        dollar_count = stripped.count("$$")
        if dollar_count % 2 == 1:
            in_dollar_block = not in_dollar_block

        current.append(line)

        # End of statement: semicolon at end of line AND not inside $$ block
        if stripped.endswith(";") and not in_dollar_block:
            stmt = "\n".join(current).strip()
            if stmt and not all(ln.strip().startswith("--") or not ln.strip() for ln in current):
                statements.append(stmt)
            current = []

    # Catch any trailing content
    if current:
        stmt = "\n".join(current).strip()
        if stmt and not all(ln.strip().startswith("--") or not ln.strip() for ln in current):
            statements.append(stmt)

    return statements


if __name__ == "__main__":
    asyncio.run(run_migration())
