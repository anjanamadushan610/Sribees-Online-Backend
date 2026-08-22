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
    Initialize database connection, create all tables,
    and seed a default super admin user if not already present.
    """
    try:
        # Import all models to ensure they are registered with SQLAlchemy Base metadata
        import app.models  # noqa

        async with engine.begin() as conn:
            # Test connection using text() for raw SQL
            from sqlalchemy import text
            await conn.execute(text("SELECT 1"))

            # Create all tables if they do not exist
            await conn.run_sync(Base.metadata.create_all)

        logger.info("✅ PostgreSQL connected and tables initialized successfully")

        await _seed_admin_accounts()

    except Exception as e:
        logger.error(f"❌ PostgreSQL connection/initialization failed: {e}")
        raise


# Branches the demo admins are scoped to. Seeded first, because a branch
# manager without a branch_id can see nothing.
_SEED_BRANCHES = [
    # (code, name,            district,  province)
    ("CMB", "Colombo Main",   "Colombo", "Western"),
    ("KDY", "Kandy Central",  "Kandy",   "Central"),
    ("GLE", "Galle Fort",     "Galle",   "Southern"),
]

# The accounts documented in admin/ADMIN_CREDENTIALS.md.
#
# NOTE ON ROLES: staff1.colombo is seeded as CUSTOMER_SUPPORT, a real base
# role — not the delegated AdminRole.STAFF (role='staff') introduced by
# migration 037, which requires a role_id pointing at a custom role a
# base-role admin created at runtime and can't be pre-seeded here.
_SEED_ADMINS = [
    # (email,                             full_name,               role,                            branch_code)
    ("superadmin@sribeesonline.lk",      "Super Admin",            "SUPER_ADMIN",       None),
    ("manager.colombo@sribeesonline.lk", "Colombo Branch Manager", "BRANCH_MANAGER",    "CMB"),
    ("manager.kandy@sribeesonline.lk",   "Kandy Branch Manager",   "BRANCH_MANAGER",    "KDY"),
    ("manager.galle@sribeesonline.lk",   "Galle Branch Manager",   "BRANCH_MANAGER",    "GLE"),
    ("staff1.colombo@sribeesonline.lk",  "Colombo Staff",          "CUSTOMER_SUPPORT",  "CMB"),
    ("inventory@sribeesonline.lk",       "Inventory Manager",      "INVENTORY_MANAGER", None),
    ("support@sribeesonline.lk",         "Customer Support",       "CUSTOMER_SUPPORT",  None),
]

_SEED_PASSWORD = "Admin@123"


async def _seed_admin_accounts() -> None:
    """
    Seed the documented demo branches and admin accounts.

    Idempotent: each row is created only when absent, so this is safe to run on
    every boot and will not overwrite a password that has since been changed.

    Hashing goes through the app's own hash_password (Argon2) rather than
    embedding precomputed hashes in a SQL migration, so the code that writes a
    hash is always the code that verifies it.
    """
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.models.admin import Admin, AdminRole
    from app.models.branch import Branch

    async with get_db_context() as db:
        branch_ids: dict[str, object] = {}
        created_branches = 0

        for code, name, district, province in _SEED_BRANCHES:
            existing = (
                await db.execute(select(Branch).where(Branch.code == code))
            ).scalar_one_or_none()
            if existing is None:
                existing = Branch(
                    name=name, code=code, district=district,
                    province=province, is_active=True,
                )
                db.add(existing)
                await db.flush()  # assign branch_id before the admins reference it
                created_branches += 1
            branch_ids[code] = existing.branch_id

        created_admins = []
        for email, full_name, role_name, branch_code in _SEED_ADMINS:
            existing = (
                await db.execute(select(Admin).where(Admin.email == email))
            ).scalar_one_or_none()
            if existing is not None:
                continue
            db.add(Admin(
                email=email,
                password_hash=hash_password(_SEED_PASSWORD),
                full_name=full_name,
                role=getattr(AdminRole, role_name),
                branch_id=branch_ids.get(branch_code) if branch_code else None,
                is_active=True,
            ))
            created_admins.append(email)

        if created_branches or created_admins:
            await db.commit()
            logger.info(
                "✅ Seeded %d branch(es) and %d admin account(s): %s",
                created_branches, len(created_admins), ", ".join(created_admins) or "none",
            )
        else:
            logger.info("Admin accounts already present — nothing to seed.")


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
