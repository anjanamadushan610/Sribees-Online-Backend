"""
Admin Auth Service
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    create_token_pair,
    hash_password,
    verify_password,
    verify_token,
)
from app.models.admin import Admin, AdminSession
from app.models.role import Role
from app.schemas.admin_auth import (
    AdminAuthData,
    AdminAuthResponse,
    AdminProfileResponse,
    AdminResponse,
)
from app.services.permission_service import PermissionService
from app.utils.logger import logger


async def _to_admin_response(admin: Admin, db: AsyncSession) -> AdminResponse:
    """Build an AdminResponse including the admin's current role display
    name and effective permission set, resolved fresh from the DB."""
    role_name = None
    if admin.role_id is not None:
        role_name = (
            await db.execute(select(Role.name).where(Role.role_id == admin.role_id))
        ).scalar_one_or_none()

    permissions = await PermissionService.get_effective_permissions(db, admin)

    return AdminResponse(
        admin_id=admin.admin_id,
        email=admin.email,
        full_name=admin.full_name,
        role=admin.role,
        role_name=role_name,
        branch_id=admin.branch_id,
        is_active=admin.is_active,
        last_login=admin.last_login,
        created_at=admin.created_at,
        permissions=[{"resource": r, "action": a} for r, a in sorted(permissions)],
    )


class AdminAuthService:
    """Admin authentication service."""

    @staticmethod
    async def login(email: str, password: str, db: AsyncSession) -> AdminAuthResponse:
        """
        Authenticate admin user.
        """
        # Find admin by email
        clean_email = email.strip().lower()
        result = await db.execute(
            select(Admin).where(Admin.email == clean_email)
        )
        admin = result.scalar_one_or_none()

        if not admin:
            raise ValueError("Invalid email or password")

        if not admin.is_active:
            raise ValueError("Account is disabled")

        # Verify password
        if not verify_password(password, admin.password_hash):
            raise ValueError("Invalid email or password")

        # Create session backed by the JWT refresh token
        session_id = uuid4()
        expires_at = datetime.utcnow() + timedelta(days=7)

        tokens = create_token_pair(
            user_id=admin.admin_id,
            email=admin.email,
            session_id=session_id,
            is_admin=True,
            role=admin.role.value,
        )

        session = AdminSession(
            session_id=session_id,
            admin_id=admin.admin_id,
            refresh_token_hash=hash_password(tokens["refresh_token"]),
            expires_at=expires_at,
        )
        db.add(session)

        # Update last login
        admin.last_login = datetime.utcnow()

        await db.commit()
        await db.refresh(admin)

        logger.info(f"Admin logged in: {admin.email}")

        return AdminAuthResponse(
            success=True,
            message="Login successful",
            data=AdminAuthData(
                admin=await _to_admin_response(admin, db),
                token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
            )
        )

    @staticmethod
    async def refresh(refresh_token: str, db: AsyncSession) -> AdminAuthResponse:
        """
        Rotate admin tokens using a valid refresh token.

        Verifies the JWT refresh token, matches it against the stored
        session, then issues a new access/refresh pair and updates the
        session so the old refresh token can no longer be used.
        """
        payload = verify_token(refresh_token, token_type="refresh")
        if not payload or not payload.get("is_admin"):
            raise ValueError("Invalid refresh token")

        admin_id = payload.get("sub")
        session_id = payload.get("session_id")
        if not admin_id or not session_id:
            raise ValueError("Invalid refresh token")

        result = await db.execute(
            select(AdminSession).where(
                AdminSession.session_id == UUID(session_id),
                AdminSession.admin_id == UUID(admin_id),
            )
        )
        session = result.scalar_one_or_none()
        if not session:
            raise ValueError("Session not found")

        # expires_at may come back tz-aware (timestamptz column)
        expires_at = session.expires_at
        if expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
        if expires_at < datetime.utcnow():
            await db.delete(session)
            await db.commit()
            raise ValueError("Session expired")

        if not verify_password(refresh_token, session.refresh_token_hash):
            raise ValueError("Invalid refresh token")

        result = await db.execute(
            select(Admin).where(Admin.admin_id == UUID(admin_id))
        )
        admin = result.scalar_one_or_none()
        if not admin or not admin.is_active:
            raise ValueError("Account is disabled")

        # Rotate: new pair bound to the same session, old token invalidated
        tokens = create_token_pair(
            user_id=admin.admin_id,
            email=admin.email,
            session_id=session.session_id,
            is_admin=True,
            role=admin.role.value,
        )
        session.refresh_token_hash = hash_password(tokens["refresh_token"])
        session.expires_at = datetime.utcnow() + timedelta(days=7)

        await db.commit()

        logger.info(f"Admin token refreshed: {admin.email}")

        return AdminAuthResponse(
            success=True,
            message="Token refreshed successfully",
            data=AdminAuthData(
                admin=await _to_admin_response(admin, db),
                token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
            )
        )

    @staticmethod
    async def get_profile(admin_id: UUID, db: AsyncSession) -> AdminProfileResponse:
        """
        Get admin profile.
        """
        result = await db.execute(
            select(Admin).where(Admin.admin_id == admin_id)
        )
        admin = result.scalar_one_or_none()

        if not admin:
            raise ValueError("Admin not found")

        return AdminProfileResponse(success=True, admin=await _to_admin_response(admin, db))

    @staticmethod
    async def update_profile(admin_id: UUID, full_name: str, db: AsyncSession) -> AdminProfileResponse:
        """
        Self-service profile update. Only the display name is editable here —
        email is the login identity and role/branch are Super-Admin-managed
        (see admin_users.py).
        """
        result = await db.execute(select(Admin).where(Admin.admin_id == admin_id))
        admin = result.scalar_one_or_none()
        if not admin:
            raise ValueError("Admin not found")

        admin.full_name = full_name.strip()
        await db.commit()
        await db.refresh(admin)

        logger.info(f"Admin profile updated: {admin.email}")

        return AdminProfileResponse(success=True, admin=await _to_admin_response(admin, db))

    @staticmethod
    async def change_password(
        admin_id: UUID, current_password: str, new_password: str, db: AsyncSession
    ) -> dict:
        """
        Self-service password change. Requires the current password (unlike
        the Super-Admin override in admin_users.py, which does not). Also
        clears every other session for this admin, forcing re-login
        everywhere else the account was signed in.
        """
        result = await db.execute(select(Admin).where(Admin.admin_id == admin_id))
        admin = result.scalar_one_or_none()
        if not admin:
            raise ValueError("Admin not found")

        if not verify_password(current_password, admin.password_hash):
            raise ValueError("Current password is incorrect")

        admin.password_hash = hash_password(new_password)
        await db.execute(delete(AdminSession).where(AdminSession.admin_id == admin_id))
        await db.commit()

        logger.info(f"Admin password changed: {admin.email}")

        return {"success": True, "message": "Password changed successfully"}

    @staticmethod
    async def logout(admin_id: UUID, db: AsyncSession) -> dict:
        """
        Logout admin by invalidating sessions.
        """
        await db.execute(
            delete(AdminSession).where(AdminSession.admin_id == admin_id)
        )
        await db.commit()

        return {"success": True, "message": "Logged out successfully"}
