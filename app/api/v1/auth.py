"""
SRIBEESonline FastAPI Backend - Auth Endpoints

Authentication routes for customer users.

Customers sign in with a phone OTP, Google or Apple -- there is no email and
password sign-in, email verification or password reset. Staff and partners
have their own password logins (admin_auth.py, partner.py).
"""
from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import RedisKeys, get_redis
from app.core.dependencies import CurrentUser
from app.models.user import User
from app.schemas.auth import (
    AuthResponse,
    ConfirmAccountDeletionRequest,
    MessageResponse,
    ProfileResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    RequestOTPResponse,
    RequestPhoneOTPRequest,
    SessionsListResponse,
    SocialLoginRequest,
    UpdateProfileRequest,
    VerifyOTPRequest,
    VerifyOTPResponse,
    VerifyPhoneOTPRequest,
)
from app.services.auth_service import AuthService
from app.services.otp_service import OTPService, invalid_code_error
from app.utils.i18n import resolve_language

router = APIRouter()


def _lang(request: Request) -> str:
    return resolve_language(request.headers.get("accept-language"))


def _client_ip(request: Request) -> str:
    from app.core.dependencies import get_client_ip

    return get_client_ip(request)


async def sms_daily_limit(db: AsyncSession) -> int:
    """The Super Admin's daily total for texts (SMS page)."""
    from app.services.app_settings_service import AppSettingsService

    return await AppSettingsService.get_sms_daily_limit(db)


@router.post(
    "/request-otp",
    response_model=RequestOTPResponse,
    summary="Request a phone verification OTP",
    description=(
        "Texts a code to the authenticated user's phone (valid 3 minutes). "
        "The response says how many digits it has and when a resend unlocks."
    ),
)
async def request_otp(
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> RequestOTPResponse:
    """Send a phone-verification OTP to the current user's number."""
    from fastapi import HTTPException

    if not current_user.phone:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add a phone number first.",
        )
    challenge = await OTPService.issue(
        redis,
        RedisKeys.phone_otp(str(current_user.user_id)),
        current_user.phone,
        client_ip=_client_ip(request),
        lang=_lang(request),
        daily_limit=await sms_daily_limit(db),
    )
    return RequestOTPResponse.from_challenge(challenge)


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
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> VerifyOTPResponse:
    """Verify the OTP and set is_phone_verified=True on success."""
    ok = await OTPService.verify_otp(
        redis,
        RedisKeys.phone_otp(str(current_user.user_id)),
        data.code,
        phone=current_user.phone,
    )
    if not ok:
        raise invalid_code_error(_lang(request))

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
        "Texts a code to the given phone number. No account or login "
        "required -- the first step of phone sign-in. Limited per number, "
        "per address and per day (429 / 503 carry retry_after and a message "
        "in the Accept-Language language)."
    ),
)
async def request_otp_by_phone(
    data: RequestPhoneOTPRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> RequestOTPResponse:
    """Send an OTP to a phone number that may have no account yet."""
    challenge = await OTPService.issue(
        redis,
        RedisKeys.phone_registration_otp(data.phone),
        data.phone,
        client_ip=_client_ip(request),
        lang=_lang(request),
        daily_limit=await sms_daily_limit(db),
    )
    return RequestOTPResponse.from_challenge(challenge)


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
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AuthResponse:
    """Verify the OTP, then find-or-create the account by phone and log in."""
    ok = await OTPService.verify_otp(
        redis, RedisKeys.phone_registration_otp(data.phone), data.code, phone=data.phone
    )
    if not ok:
        raise invalid_code_error(_lang(request))

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    device_name = request.headers.get("x-device-name")

    return await AuthService.login_or_register_by_phone(
        data.phone,
        db,
        ip_address=ip_address,
        user_agent=user_agent,
        device_name=device_name,
    )


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
    "/logout-others",
    response_model=MessageResponse,
    summary="Logout all other sessions",
    description="Invalidate all user sessions except the current one.",
)
async def logout_others(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """
    Logout from all other devices.

    Requires authentication. Invalidates all active sessions except the current one.
    """
    return await AuthService.logout_others(current_user, db)


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


# ============================================================================
# Account closure (required by Google Play for any app with sign-up)
# ============================================================================


@router.post(
    "/account/deletion/request-otp",
    response_model=RequestOTPResponse,
    summary="Send a code to confirm closing this account",
    description=(
        "Texts a code to one of the numbers on the account. Closing "
        "an account is irreversible for the person doing it, so it is gated "
        "on proving control of the phone rather than on a password somebody "
        "may have left signed in on a shared handset."
    ),
)
async def request_account_deletion_otp(
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> RequestOTPResponse:
    from fastapi import HTTPException

    # Either number on the account. A customer who changed their main number
    # and cannot receive on it must still be able to close the account, and
    # the alternate is one they already proved at some point.
    phone = current_user.phone or current_user.alternate_phone
    if not phone:
        # A Google or Apple sign-up that never added a number. They have no
        # second factor to prove, so this has to be a support conversation
        # rather than a silently weaker path.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Add a phone number in Settings before closing your account, "
                "or contact support."
            ),
        )

    challenge = await OTPService.issue(
        redis,
        RedisKeys.account_deletion_otp(str(current_user.user_id)),
        phone,
        client_ip=_client_ip(request),
        lang=_lang(request),
        daily_limit=await sms_daily_limit(db),
    )
    return RequestOTPResponse.from_challenge(challenge)


@router.post(
    "/account/deletion/confirm",
    response_model=MessageResponse,
    summary="Close this account",
    description=(
        "Verifies the code and closes the account. The login, the phone "
        "numbers, the email and every push token go. Order history, the "
        "wallet ledger and the NIC stay: those are financial records with "
        "obligations attached, and they belong to the transaction rather "
        "than to the account."
    ),
)
async def confirm_account_deletion(
    data: ConfirmAccountDeletionRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> MessageResponse:
    from fastapi import HTTPException

    from app.services.account_deletion_service import (
        AccountDeletionError,
        close_account,
    )

    ok = await OTPService.verify_otp(
        redis,
        RedisKeys.account_deletion_otp(str(current_user.user_id)),
        data.code,
        phone=current_user.phone or current_user.alternate_phone,
    )
    if not ok:
        raise invalid_code_error(_lang(request))

    try:
        await close_account(db, current_user.user_id, reason=data.reason)
    except AccountDeletionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return MessageResponse(message="Your account has been closed.")


# ============================================================================
# Google / Apple sign-in
# ============================================================================


@router.post(
    "/social/login",
    response_model=AuthResponse,
    summary="Sign in with Google or Apple",
    description=(
        "Takes a Firebase ID token from a Google or Apple sign-in, verifies "
        "it server-side, and returns a session. Creates the account on first "
        "use. "
        "A social sign-in skips the phone STEP, not the phone: the profile "
        "gate still collects the phone number and the NIC before an order can "
        "be placed, and the NIC is still unique and still write-once."
    ),
)
async def social_login(
    data: SocialLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    from fastapi import HTTPException

    from app.services.social_auth_service import SocialAuthError, sign_in

    try:
        user, created = await sign_in(db, data.id_token)
    except SocialAuthError as exc:
        # 401, not 400: the token is a credential, and a rejected credential
        # is an authentication failure however it was malformed.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    return await AuthService.issue_session(
        user,
        db,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        device_name=request.headers.get("x-device-name"),
        message="Welcome to Sribees Online" if created else "Login successful",
    )
