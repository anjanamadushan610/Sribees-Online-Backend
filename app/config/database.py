"""
SRIBEESonline FastAPI Backend - Database Configuration

Async PostgreSQL connection using SQLAlchemy 2.0 with asyncpg.
"""
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool

from app.config.settings import settings
from app.utils.logger import logger

# Create async engine
engine = create_async_engine(
    settings.async_database_url,
    echo=settings.database_echo,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_timeout=settings.database_pool_timeout,
    pool_pre_ping=True,  # Enable connection health checks
)

# Create async session factory
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# Base class for declarative models
Base = declarative_base()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency that provides an async database session.

    Yields:
        AsyncSession: Database session for the request

    Usage:
        @router.get("/items")
        async def get_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager for database sessions outside of request context.

    Usage:
        async with get_db_context() as db:
            result = await db.execute(query)
    """
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """
    Initialize database connection and create all tables.

    Seeds nothing. The demo branches and admin accounts that used to be
    created here on every boot were removed on 2026-11-07 at the owner's
    request: real branches and staff are made from the admin dashboard.
    """
    try:
        # Import all models to ensure they are registered with SQLAlchemy Base metadata
        import app.models  # noqa

        async with engine.begin() as conn:
            from sqlalchemy import text

            # Acquire Postgres advisory transaction lock to serialize init across all concurrent workers
            await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('sribees_init_db_lock'))"))

            # Ensure admin_role_enum type exists safely (idempotent across multi-worker boot)
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'admin_role_enum') THEN
                        CREATE TYPE admin_role_enum AS ENUM (
                            'SUPER_ADMIN', 'BRANCH_MANAGER', 'MARKETING_MANAGER',
                            'INVENTORY_MANAGER', 'CUSTOMER_SUPPORT', 'STAFF'
                        );
                    END IF;
                EXCEPTION
                    WHEN duplicate_object THEN NULL;
                    WHEN unique_violation THEN NULL;
                END
                $$;
            """))

            # Create all tables if they do not exist
            await conn.run_sync(Base.metadata.create_all)

        logger.info("✅ PostgreSQL connected and tables initialized successfully")

    except Exception as e:
        logger.error(f"❌ PostgreSQL connection/initialization failed: {e}")
        raise


async def close_db() -> None:
    """
    Close database connections.

    Called during application shutdown.
    """
    await engine.dispose()
    logger.info("PostgreSQL connections closed")


# For testing - use NullPool to avoid connection issues
def get_test_engine():
    """Create a test engine with NullPool for testing."""
    return create_async_engine(
        settings.async_database_url,
        echo=True,
        poolclass=NullPool,
    )
