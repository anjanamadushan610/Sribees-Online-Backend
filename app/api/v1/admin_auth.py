"""
Admin Auth API Endpoints
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_admin, require_super_admin
from app.schemas.admin_auth import (
    AdminAuthResponse,
    AdminListResponse,
    AdminLoginRequest,
    AdminProfileResponse,
    AdminRefreshRequest,
    AdminResponse,
    ChangePasswordRequest,
    CreateAdminRequest,
    UpdateProfileRequest,
)
from app.schemas.auth import MessageResponse
from app.services.admin_auth_service import AdminAuthService

router = APIRouter()


@router.post(
    "/login",
    response_model=AdminAuthResponse,
    summary="Admin login",
    description="Authenticate admin user and return JWT tokens.",
)
async def admin_login(
    data: AdminLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> AdminAuthResponse:
    """
    Authenticate admin with email and password.
    """
    try:
        return await AdminAuthService.login(data.email, data.password, db)
    except ValueError:
        # Use a generic message to avoid leaking whether the email exists.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
        )


@router.post(
    "/refresh",
    response_model=AdminAuthResponse,
    summary="Refresh admin tokens",
    description="Rotate admin access/refresh tokens using a valid refresh token.",
)
async def admin_refresh(
    data: AdminRefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> AdminAuthResponse:
    """
    Refresh admin JWT tokens.

    Returns a new access/refresh pair and the admin profile. The old
    refresh token is invalidated (rotation).
    """
    try:
        return await AdminAuthService.refresh(data.refresh_token, db)
    except ValueError:
        # Generic message; details are logged server-side.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
        )


@router.get(
    "/profile",
    response_model=AdminProfileResponse,
    summary="Get admin profile",
    description="Get current admin's profile.",
)
async def get_admin_profile(
    current_admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminProfileResponse:
    """
    Get current admin's profile.
    """
    try:
        return await AdminAuthService.get_profile(current_admin.admin_id, db)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


@router.put(
    "/profile",
    response_model=AdminProfileResponse,
    summary="Update own profile",
    description="Update the current admin's display name.",
)
async def update_admin_profile(
    data: UpdateProfileRequest,
    current_admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminProfileResponse:
    """
    Update the current admin's own profile (display name only).
    """
    try:
        return await AdminAuthService.update_profile(current_admin.admin_id, data.full_name, db)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


@router.post(
    "/change-password",
    response_model=MessageResponse,
    summary="Change own password",
    description="Change the current admin's password (requires the current password).",
)
async def change_admin_password(
    data: ChangePasswordRequest,
    current_admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """
    Change the current admin's own password. Signs the account out of every
    other session on success.
    """
    try:
        result = await AdminAuthService.change_password(
            current_admin.admin_id, data.current_password, data.new_password, db
        )
        return MessageResponse(**result)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/users",
    response_model=AdminResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create admin user",
    description="Create a new admin user (Super Admin only).",
)
async def create_admin(
    data: CreateAdminRequest,
    current_admin = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminResponse:
    """
    Create a new admin user.

    Only Super Admins can create new admin users.
    """
    try:
        return await AdminAuthService.create_admin(data, current_admin.admin_id, db)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get(
    "/users",
    response_model=AdminListResponse,
    summary="List admin users",
    description="List all admin users (Super Admin only).",
)
async def list_admins(
    current_admin = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminListResponse:
    """
    List all admin users.

    Only Super Admins can list admin users.
    """
    return await AdminAuthService.list_admins(db)


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Admin logout",
    description="Logout admin user.",
)
async def admin_logout(
    current_admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """
    Logout current admin.
    """
    result = await AdminAuthService.logout(current_admin.admin_id, db)
    return MessageResponse(**result)
