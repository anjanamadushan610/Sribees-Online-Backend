"""
SRIBEESonline FastAPI Backend - Auth Endpoints

Authentication routes for customer users.
"""
from fastapi import APIRouter, Depends, status
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import RedisKeys, RedisTTL, get_redis
from app.core.dependencies import CurrentUser
from app.models.user import User
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    ProfileResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    RegisterRequest,
    RegisterResponse,
    RequestOTPResponse,
    RequestPhoneOTPRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    SessionsListResponse,
    UpdateProfileRequest,
    VerifyEmailRequest,
    VerifyOTPRequest,
    VerifyOTPResponse,
    VerifyPhoneOTPRequest,
)
from app.services.auth_service import AuthService
from app.services.otp_service import OTPService

router = APIRouter()


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register new user",
    description="Create a new user account. A verification email will be sent.",
)
async def register(
    data: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> RegisterResponse:
    """
    Register a new user account.

    - **email**: Valid email address (must be unique)
    - **password**: Minimum 8 characters with uppercase, lowercase, and digit
    - **full_name**: User's display name

    Returns the created user (unverified) and sends a verification email.
    """
    return await AuthService.register(data, db)


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="User login",
    description="Authenticate user and return JWT tokens.",
)
async def login(
    data: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """
    Authenticate user with email and password.

    - **email**: Registered email address
    - **password**: User's password
    - **remember_me**: If true, extends refresh token to 30 days

    Returns user data and JWT access/refresh tokens.
    """
    return await AuthService.login(data, db)


@router.post(
    "/verify-email",
    response_model=MessageResponse,
    summary="Verify email address",
    description="Verify user's email address using token from email.",
)
async def verify_email(
    data: VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """
    Verify email address using verification token.

    - **token**: Verification token received via email

    Marks the user's email as verified.
    """
    return await AuthService.verify_email(data.token, db)


@router.post(
    "/resend-verification",
    response_model=MessageResponse,
    summary="Resend verification email",
    description="Resend email verification link.",
)
async def resend_verification(
    data: ResendVerificationRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """
    Resend email verification link.

    - **email**: Email address to send verification link

    Always returns success for security (doesn't reveal if email exists).
    """
    return await AuthService.resend_verification_email(data.email, db)


@router.post(
    "/request-otp",
    response_model=RequestOTPResponse,
    summary="Request a phone verification OTP",
    description=(
        "Generates a 6-digit code for the authenticated user's phone, stores it "
        "in Redis for 3 minutes, and dispatches it (mock SMS → server log)."
    ),
)
async def request_otp(
    current_user: CurrentUser,
    redis: Redis = Depends(get_redis),
) -> RequestOTPResponse:
    """Generate + 'send' a phone-verification OTP for the current user."""
    await OTPService.request_otp(
        redis,
        key=RedisKeys.phone_otp(str(current_user.user_id)),
        phone=current_user.phone or "",
    )
    return RequestOTPResponse(
        message="Verification code sent",
        expires_in_seconds=RedisTTL.PHONE_OTP,
    )


@router.post(
    "/verify-otp",
    response_model=VerifyOTPResponse,
    summary="Verify a phone OTP",
    description=(
        "Validates the 6-digit code against Redis. On success, marks the user's "
        "phone as verified."
    ),
)
async def verify_otp(
    data: VerifyOTPRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> VerifyOTPResponse:
    """Verify the OTP and set is_phone_verified=True on success."""
    from fastapi import HTTPException

    ok = await OTPService.verify_otp(
        redis, RedisKeys.phone_otp(str(current_user.user_id)), data.code
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification code.",
        )

    await db.execute(
        update(User)
        .where(User.user_id == current_user.user_id)
        .values(is_phone_verified=True)
    )
    await db.commit()

    return VerifyOTPResponse(is_phone_verified=True, message="Phone number verified")


@router.post(
    "/phone/request-otp",
    response_model=RequestOTPResponse,
    summary="Request a phone registration OTP",
    description=(
        "Generates a 6-digit code for the given phone number and dispatches "
        "it (mock SMS → server log). No account or login required — this is "
        "the first step of phone-first sign-up."
    ),
)
async def request_otp_by_phone(
    data: RequestPhoneOTPRequest,
    redis: Redis = Depends(get_redis),
) -> RequestOTPResponse:
    """Generate + 'send' an OTP for a phone number that has no account yet."""
    await OTPService.request_otp(
        redis,
        key=RedisKeys.phone_registration_otp(data.phone),
        phone=data.phone,
    )
    return RequestOTPResponse(
        message="Verification code sent",
        expires_in_seconds=RedisTTL.PHONE_OTP,
    )


@router.post(
    "/phone/verify-otp",
    response_model=AuthResponse,
    summary="Verify a phone registration OTP and log in / register",
    description=(
        "Validates the 6-digit code against Redis. On success, finds or "
        "creates the account for this phone number (phone is the unique "
        "key — name is filled in later via profile) and returns a session."
    ),
)
async def verify_otp_by_phone(
    data: VerifyPhoneOTPRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AuthResponse:
    """Verify the OTP, then find-or-create the account by phone and log in."""
    from fastapi import HTTPException

    ok = await OTPService.verify_otp(
        redis, RedisKeys.phone_registration_otp(data.phone), data.code
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification code.",
        )

    return await AuthService.login_or_register_by_phone(data.phone, db)


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    summary="Request password reset",
    description="Send password reset link to email.",
)
async def forgot_password(
    data: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """
    Request password reset.

    - **email**: Email address for password reset

    Always returns success for security (doesn't reveal if email exists).
    """
    return await AuthService.forgot_password(data.email, db)


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    summary="Reset password",
    description="Reset password using token from email.",
)
async def reset_password(
    data: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """
    Reset password using reset token.

    - **token**: Password reset token from email
    - **password**: New password (min 8 characters)

    Updates user's password and invalidates the reset token.
    """
    return await AuthService.reset_password(data.token, data.password, db)


@router.post(
    "/refresh",
    response_model=RefreshTokenResponse,
    summary="Refresh access token",
    description="Get new access token using refresh token.",
)
@router.post(
    "/refresh-token",
    response_model=RefreshTokenResponse,
    summary="Refresh access token (legacy path)",
    description="Alias of /refresh kept for backwards compatibility.",
)
async def refresh_token(
    data: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
) -> RefreshTokenResponse:
    """
    Refresh JWT access token.

    - **refresh_token**: Valid refresh token

    Returns new access and refresh tokens nested under `tokens`, in the same
    structure as the login/register responses.
    """
    return await AuthService.refresh_token(data.refresh_token, db)


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="User logout",
    description="Invalidate current session.",
)
async def logout(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """
    Logout current user.

    Requires authentication. Invalidates the current session.
    """
    return await AuthService.logout(current_user, db)


@router.post(
    "/logout-all",
    response_model=MessageResponse,
    summary="Logout all sessions",
    description="Invalidate all user sessions.",
)
async def logout_all(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """
    Logout from all devices.

    Requires authentication. Invalidates all active sessions.
    """
    return await AuthService.logout_all(current_user, db)


@router.post(
    "/change-password",
    response_model=MessageResponse,
    summary="Change password",
    description="Change user's password.",
)
async def change_password(
    data: ChangePasswordRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """
    Change user's password.

    Requires current password verification.
    """
    return await AuthService.change_password(
        current_user,
        data.current_password,
        data.new_password,
        db
    )


@router.get(
    "/me",
    response_model=ProfileResponse,
    summary="Get current user profile",
    description="Get authenticated user's profile.",
)
async def get_profile(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProfileResponse:
    """
    Get current user's profile.

    Returns full user profile data.
    """
    return await AuthService.get_profile(current_user, db)


@router.patch(
    "/me",
    response_model=ProfileResponse,
    summary="Update current user profile",
    description="Partially update the authenticated user's profile.",
)
async def update_profile(
    data: UpdateProfileRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProfileResponse:
    """
    Update current user's profile.

    - **full_name**: Single full-name field (matches `users.full_name`)
    - **phone**: Phone number
    - **profile_picture_url**: Avatar URL

    Only the fields provided are updated. Returns the updated profile.
    """
    return await AuthService.update_profile(current_user, data, db)


@router.get(
    "/sessions",
    response_model=SessionsListResponse,
    summary="Get active sessions",
    description="List all active sessions for the user.",
)
async def get_sessions(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SessionsListResponse:
    """
    Get all active sessions.

    Returns list of all active sessions for the current user.
    """
    return await AuthService.get_sessions(current_user, db)


@router.delete(
    "/sessions/{session_id}",
    response_model=MessageResponse,
    summary="Revoke session",
    description="Revoke a specific session.",
)
async def revoke_session(
    session_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """
    Revoke a specific session.

    Cannot revoke the current session (use logout instead).
    """
    return await AuthService.revoke_session(current_user, session_id, db)
