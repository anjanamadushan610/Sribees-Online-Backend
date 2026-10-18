"""
SRIBEESonline FastAPI Backend - Auth Service

Business logic for user authentication.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.redis import RedisKeys, RedisTTL
from app.core.exceptions import (
    DuplicateError,
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    InvalidTokenError,
    TokenExpiredError,
    ValidationError,
)
from app.core.security import (
    create_token_pair,
    verify_token,
)
from app.models.user import Session, User, UserPhoneChangeHistory
from app.schemas.auth import (
    AuthResponse,
    MessageResponse,
    RefreshTokenResponse,
    TokensResponse,
    UserResponse,
)
from app.services.account_deletion_service import live_nic_holder
from app.utils.logger import logger


class AuthService:
    """Authentication service with user management operations."""

    # ========================================================================
    # Phone-first registration / login
    # ========================================================================

    @staticmethod
    @staticmethod
    async def issue_session(
        user,
        db: AsyncSession,
        *,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        device_name: Optional[str] = None,
        message: str = "Login successful",
    ) -> AuthResponse:
        """
        Issue a session for a user who has already been authenticated.

        Extracted so the social sign-in path cannot drift from the phone one.
        A second copy of session creation is a second place for an expiry, an
        ip record or a token pair to be got subtly wrong.
        """
        session_id = uuid4()
        session = Session(
            session_id=session_id,
            user_id=user.user_id,
            refresh_token_hash="placeholder",
            ip_address=ip_address,
            user_agent=device_name or user_agent,
            expires_at=datetime.utcnow() + timedelta(days=7),
        )
        db.add(session)
        user.last_login = datetime.utcnow()
        await db.commit()
        await db.refresh(user)

        tokens = create_token_pair(
            user_id=user.user_id,
            email=user.email or "",
            session_id=session_id,
        )
        return AuthResponse(
            success=True,
            message=message,
            user=UserResponse(
                user_id=user.user_id,
                email=user.email,
                full_name=user.full_name,
                phone=user.phone,
                nic=user.nic,
                alternate_phone=user.alternate_phone,
                profile_picture_url=user.profile_picture_url,
                is_verified=user.is_verified,
                is_phone_verified=user.is_phone_verified,
                two_factor_enabled=user.two_factor_enabled,
                created_at=user.created_at,
                updated_at=user.updated_at,
                last_login=user.last_login,
            ),
            tokens=TokensResponse(
                access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
            ),
        )

    async def login_or_register_by_phone(
        phone: str,
        db: AsyncSession,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        device_name: Optional[str] = None,
    ) -> AuthResponse:
        """
        Find-or-create a user by phone number and log them in.

        A verified OTP is the only gate here — the phone number is the
        account's identity key, so the first successful verification silently
        creates the account (name is empty until the user sets it via
        profile) and every verification after that is just a login.

        Args:
            phone: Phone number, already OTP-verified by the caller
            db: Database session
            ip_address: Client IP address
            user_agent: Client User-Agent header
            device_name: Resolved device model name (e.g. Samsung Galaxy A05)

        Returns:
            AuthResponse with the (possibly new) user and fresh tokens
        """
        result = await db.execute(select(User).where(User.phone == phone))
        user = result.scalar_one_or_none()

        if not user:
            user = User(
                user_id=uuid4(),
                phone=phone,
                full_name="",
                is_verified=True,
                is_phone_verified=True,
            )
            db.add(user)
            await db.flush()
            logger.info(f"New user registered by phone: {phone}")

        session_id = uuid4()
        expires_at = datetime.utcnow() + timedelta(days=7)

        stored_user_agent = device_name or user_agent

        session = Session(
            session_id=session_id,
            user_id=user.user_id,
            refresh_token_hash="placeholder",  # Will be updated after token generation
            ip_address=ip_address,
            user_agent=stored_user_agent,
            expires_at=expires_at,
        )
        db.add(session)

        user.last_login = datetime.utcnow()

        await db.commit()
        await db.refresh(user)

        tokens = create_token_pair(
            user_id=user.user_id,
            email=user.email or "",
            session_id=session_id,
        )

        logger.info(f"User logged in by phone: {phone}")

        return AuthResponse(
            success=True,
            message="Login successful",
            user=UserResponse(
                user_id=user.user_id,
                email=user.email,
                full_name=user.full_name,
                phone=user.phone,
                nic=user.nic,
                alternate_phone=user.alternate_phone,
                profile_picture_url=user.profile_picture_url,
                is_verified=user.is_verified,
                is_phone_verified=user.is_phone_verified,
                two_factor_enabled=user.two_factor_enabled,
                created_at=user.created_at,
                updated_at=user.updated_at,
                last_login=user.last_login,
            ),
            tokens=TokensResponse(
                access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
            ),
        )

    # ========================================================================
    # Token Refresh
    # ========================================================================

    @staticmethod
    async def refresh_token(refresh_token: str, db: AsyncSession) -> RefreshTokenResponse:
        """
        Refresh access token.

        Args:
            refresh_token: JWT refresh token
            db: Database session

        Returns:
            RefreshTokenResponse with new tokens

        Raises:
            InvalidTokenError: If token is invalid
        """
        # Verify refresh token
        payload = verify_token(refresh_token, token_type="refresh")

        if not payload:
            raise InvalidTokenError(message="Invalid refresh token")

        user_id = payload.get("sub")
        session_id = payload.get("session_id")

        if not user_id or not session_id:
            raise InvalidTokenError(message="Invalid token payload")

        # Verify session exists
        result = await db.execute(
            select(Session).where(
                Session.session_id == UUID(session_id),
                Session.user_id == UUID(user_id),
            )
        )
        session = result.scalar_one_or_none()

        if not session:
            raise InvalidTokenError(message="Session not found")

        if datetime.utcnow() > session.expires_at:
            await db.execute(
                delete(Session).where(Session.session_id == session.session_id)
            )
            await db.commit()
            raise TokenExpiredError(message="Session has expired")

        # Get user
        result = await db.execute(
            select(User).where(User.user_id == UUID(user_id))
        )
        user = result.scalar_one_or_none()

        if not user:
            raise InvalidTokenError(message="User not found")

        # Generate new tokens
        tokens = create_token_pair(
            user_id=user.user_id,
            email=user.email,
            session_id=session.session_id,
        )

        return RefreshTokenResponse(
            success=True,
            message="Token refreshed successfully",
            tokens=TokensResponse(
                access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
            ),
        )

    # ========================================================================
    # Logout
    # ========================================================================

    @staticmethod
    async def logout(current_user, db: AsyncSession) -> MessageResponse:
        """
        Logout user by invalidating only the current session.

        Args:
            current_user: Currently authenticated user
            db: Database session

        Returns:
            MessageResponse confirming logout
        """
        current_session_id = str(getattr(current_user, "_current_session_id", "") or "")
        if current_session_id:
            try:
                session_uuid = UUID(current_session_id)
                await db.execute(
                    delete(Session).where(
                        Session.session_id == session_uuid,
                        Session.user_id == current_user.user_id,
                    )
                )
            except ValueError:
                pass
        else:
            # Fallback if no specific session_id in token
            await db.execute(
                delete(Session).where(Session.user_id == current_user.user_id)
            )
        await db.commit()

        logger.info(f"User logged out: {current_user.email or current_user.phone}")

        return MessageResponse(
            success=True,
            message="Logged out successfully",
        )

    @staticmethod
    async def logout_all(current_user, db: AsyncSession) -> MessageResponse:
        """
        Logout from all sessions.

        Args:
            current_user: Currently authenticated user
            db: Database session

        Returns:
            MessageResponse confirming logout from all devices
        """
        await db.execute(
            delete(Session).where(Session.user_id == current_user.user_id)
        )
        await db.commit()

        logger.info(f"User logged out from all devices: {current_user.email or current_user.phone}")

        return MessageResponse(
            success=True,
            message="Logged out from all devices",
        )

    @staticmethod
    async def logout_others(current_user, db: AsyncSession) -> MessageResponse:
        """
        Logout from all other sessions except the current one.

        Args:
            current_user: Currently authenticated user
            db: Database session

        Returns:
            MessageResponse confirming logout from other devices
        """
        current_session_id = str(getattr(current_user, "_current_session_id", "") or "")
        query = delete(Session).where(Session.user_id == current_user.user_id)
        if current_session_id:
            try:
                session_uuid = UUID(current_session_id)
                query = query.where(Session.session_id != session_uuid)
            except ValueError:
                pass
        await db.execute(query)
        await db.commit()

        logger.info(f"User logged out from other devices: {current_user.email or current_user.phone}")

        return MessageResponse(
            success=True,
            message="Logged out from other devices",
        )

    @staticmethod
    async def get_profile(current_user, db: AsyncSession):
        """
        Get user's profile.

        Args:
            current_user: Currently authenticated user
            db: Database session

        Returns:
            ProfileResponse with user data
        """
        from app.schemas.auth import ProfileResponse, UserResponse

        result = await db.execute(
            select(User).where(User.user_id == current_user.user_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            raise InvalidCredentialsError()

        return ProfileResponse(
            success=True,
            user=UserResponse(
                user_id=user.user_id,
                email=user.email,
                full_name=user.full_name,
                phone=user.phone,
                nic=user.nic,
                alternate_phone=user.alternate_phone,
                profile_picture_url=user.profile_picture_url,
                is_verified=user.is_verified,
                is_phone_verified=user.is_phone_verified,
                two_factor_enabled=user.two_factor_enabled,
                created_at=user.created_at,
                updated_at=user.updated_at,
                last_login=user.last_login,
            ),
        )

    @staticmethod
    async def update_profile(current_user, data, db: AsyncSession):
        """
        Update user's profile (partial update).

        Accepts a single `full_name` field for the user's name, matching the
        `users.full_name` column. Only fields present in the request are
        updated.

        Args:
            current_user: Currently authenticated user
            data: UpdateProfileRequest with fields to update
            db: Database session

        Returns:
            ProfileResponse with the updated user data
        """
        from app.schemas.auth import ProfileResponse, UserResponse

        result = await db.execute(
            select(User).where(User.user_id == current_user.user_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            raise InvalidCredentialsError()

        if data.full_name is not None:
            user.full_name = data.full_name
        if data.email is not None and data.email != user.email:
            existing = await db.execute(
                select(User).where(User.email == data.email, User.user_id != user.user_id)
            )
            if existing.scalar_one_or_none() is not None:
                raise EmailAlreadyExistsError()
            user.email = data.email
        if data.phone is not None and data.phone != user.phone:
            raise ValidationError(
                message="Primary phone number must be updated using the phone verification flow.",
                errors={"phone": "Requires OTP verification"},
            )
        if data.nic is not None and data.nic != user.nic:
            # Write-once. The NIC is what this business recognises a person
            # by, so letting an account rewrite it at will would let one
            # customer quietly become another; corrections go through support.
            if user.nic:
                raise ValidationError(
                    message=(
                        "Your NIC cannot be changed here. "
                        "Contact support if it needs correcting."
                    ),
                    errors={"nic": "NIC is already set"},
                )
            # Only a LIVE account blocks it: someone who closed their account
            # may register again with the same NIC (the closed row keeps it).
            existing = await db.execute(
                live_nic_holder(data.nic, other_than=user.user_id)
            )
            if existing.scalar_one_or_none() is not None:
                raise DuplicateError(
                    field="nic",
                    message="This NIC is already registered to another account",
                )
            user.nic = data.nic
        if data.alternate_phone is not None:
            second = data.alternate_phone.strip() or None
            if second and second == (user.phone or ""):
                raise ValidationError(
                    message=(
                        "The second number must be different from your "
                        "main number"
                    ),
                    errors={"alternate_phone": "Duplicate of the main number"},
                )
            user.alternate_phone = second
        if data.profile_picture_url is not None:
            user.profile_picture_url = data.profile_picture_url

        await db.commit()
        await db.refresh(user)

        return ProfileResponse(
            success=True,
            user=UserResponse(
                user_id=user.user_id,
                email=user.email,
                full_name=user.full_name,
                phone=user.phone,
                nic=user.nic,
                alternate_phone=user.alternate_phone,
                profile_picture_url=user.profile_picture_url,
                is_verified=user.is_verified,
                is_phone_verified=user.is_phone_verified,
                two_factor_enabled=user.two_factor_enabled,
                created_at=user.created_at,
                updated_at=user.updated_at,
                last_login=user.last_login,
            ),
        )

    @staticmethod
    async def get_sessions(current_user, db: AsyncSession):
        """
        Get all active sessions for user.

        Args:
            current_user: Currently authenticated user
            db: Database session

        Returns:
            SessionsListResponse with list of sessions
        """
        from app.schemas.auth import SessionResponse, SessionsListResponse

        result = await db.execute(
            select(Session)
            .where(Session.user_id == current_user.user_id)
            .where(Session.expires_at > datetime.utcnow())
            .order_by(Session.created_at.desc())
        )
        sessions = result.scalars().all()

        current_session_id = str(getattr(current_user, "_current_session_id", "") or "")

        session_list = [
            SessionResponse(
                sessionId=str(s.session_id),
                device=_parse_device_name(s.user_agent),
                ipAddress=s.ip_address,
                userAgent=s.user_agent,
                createdAt=s.created_at or datetime.utcnow(),
                lastActive=s.created_at or datetime.utcnow(),
                isCurrent=(str(s.session_id) == current_session_id) if current_session_id else False,
            )
            for s in sessions
        ]

        if not session_list:
            session_list = [
                SessionResponse(
                    sessionId=current_session_id or str(uuid4()),
                    device="Mobile Device",
                    ipAddress=None,
                    userAgent=None,
                    createdAt=getattr(current_user, "created_at", None) or datetime.utcnow(),
                    lastActive=datetime.utcnow(),
                    isCurrent=True,
                )
            ]
        elif not any(s.is_current for s in session_list):
            session_list[0].is_current = True

        return SessionsListResponse(
            success=True,
            sessions=session_list,
        )

    @staticmethod
    async def revoke_session(current_user, session_id: str, db: AsyncSession) -> MessageResponse:
        """
        Revoke a specific session.

        Args:
            current_user: Currently authenticated user
            session_id: Session ID to revoke
            db: Database session

        Returns:
            MessageResponse confirming session revocation
        """
        current_session_id = str(getattr(current_user, "_current_session_id", "") or "")
        if current_session_id and str(session_id) == current_session_id:
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot revoke the current session. Use logout instead.",
            )

        result = await db.execute(
            delete(Session)
            .where(Session.session_id == UUID(session_id))
            .where(Session.user_id == current_user.user_id)
        )
        await db.commit()

        if result.rowcount == 0:
            raise InvalidTokenError(message="Session not found")

        return MessageResponse(
            success=True,
            message="Session revoked successfully",
        )

    # ========================================================================
    # Primary Phone Number Change & Verification (with Quota Enforcement)
    # ========================================================================

    @staticmethod
    async def get_phone_change_status(user_id: UUID, db: AsyncSession) -> dict:
        """Calculate remaining daily (2) and monthly (4) primary phone change quotas."""
        now = datetime.now(timezone.utc)
        since_day = now - timedelta(hours=24)
        since_month = now - timedelta(days=30)

        daily_res = await db.execute(
            select(func.count(UserPhoneChangeHistory.id)).where(
                UserPhoneChangeHistory.user_id == user_id,
                UserPhoneChangeHistory.changed_at >= since_day,
            )
        )
        daily_count = daily_res.scalar() or 0

        monthly_res = await db.execute(
            select(func.count(UserPhoneChangeHistory.id)).where(
                UserPhoneChangeHistory.user_id == user_id,
                UserPhoneChangeHistory.changed_at >= since_month,
            )
        )
        monthly_count = monthly_res.scalar() or 0

        daily_limit = 2
        monthly_limit = 4
        daily_remaining = max(0, daily_limit - daily_count)
        monthly_remaining = max(0, monthly_limit - monthly_count)

        return {
            "daily_remaining": daily_remaining,
            "monthly_remaining": monthly_remaining,
            "daily_limit": daily_limit,
            "monthly_limit": monthly_limit,
            "can_change": daily_remaining > 0 and monthly_remaining > 0,
        }

    @staticmethod
    async def request_phone_change(
        current_user: User,
        new_phone_raw: str,
        client_ip: str,
        lang: str,
        db: AsyncSession,
        redis,
    ):
        """Validate quotas and uniqueness, then send OTP for primary phone change."""
        from app.services.app_settings_service import AppSettingsService
        from app.services.otp_service import OTPService
        from app.utils.identity import normalize_mobile

        try:
            new_phone = normalize_mobile(new_phone_raw)
        except ValueError as e:
            raise ValidationError(message=str(e), errors={"new_phone": str(e)})

        if current_user.phone and new_phone == current_user.phone:
            raise ValidationError(
                message="This is already your current phone number",
                errors={"new_phone": "Same as current"},
            )

        # Collision check
        existing = await db.execute(
            select(User).where(User.phone == new_phone, User.user_id != current_user.user_id)
        )
        if existing.scalar_one_or_none() is not None:
            raise DuplicateError(
                field="phone",
                message="This phone number is already registered to another account",
            )

        # Quota check (applies if user already has a phone; initial setup for Google OAuth users is allowed)
        if current_user.phone:
            quota = await AuthService.get_phone_change_status(current_user.user_id, db)
            if not quota["can_change"]:
                if quota["daily_remaining"] <= 0:
                    raise ValidationError(
                        message="Daily phone change limit reached (2/2). Please try again tomorrow.",
                        errors={"daily": "Limit reached"},
                    )
                raise ValidationError(
                    message="Monthly phone change limit reached (4/4). Please try again next month.",
                    errors={"monthly": "Limit reached"},
                )

        daily_sms_limit = await AppSettingsService.get_sms_daily_limit(db)
        key = RedisKeys.phone_change_otp(str(current_user.user_id))
        challenge = await OTPService.issue(
            redis,
            key,
            new_phone,
            client_ip=client_ip,
            lang=lang,
            daily_limit=daily_sms_limit,
        )

        # Cache target phone for this challenge (valid for 3 minutes)
        if redis:
            await redis.set(
                f"otp:phone_change:{current_user.user_id}:target_phone",
                new_phone,
                ex=RedisTTL.PHONE_OTP,
            )

        return challenge

    @staticmethod
    async def verify_phone_change(
        current_user: User,
        new_phone_raw: str,
        otp: str,
        client_ip: str,
        db: AsyncSession,
        redis,
    ):
        """Verify OTP and update the user's primary phone number with audit logging."""
        from app.schemas.auth import ProfileResponse
        from app.services.otp_service import OTPService, invalid_code_error
        from app.utils.identity import normalize_mobile

        try:
            new_phone = normalize_mobile(new_phone_raw)
        except ValueError as e:
            raise ValidationError(message=str(e), errors={"new_phone": str(e)})

        if redis:
            target_phone = await redis.get(f"otp:phone_change:{current_user.user_id}:target_phone")
            if target_phone and target_phone != new_phone:
                raise ValidationError(
                    message="Phone number does not match the verification request",
                    errors={"new_phone": "Mismatch with requested number"},
                )

        key = RedisKeys.phone_change_otp(str(current_user.user_id))
        ok = await OTPService.verify_otp(redis, key, otp, phone=new_phone)
        if not ok:
            raise invalid_code_error(lang="en")

        # Concurrency safety re-check
        existing = await db.execute(
            select(User).where(User.phone == new_phone, User.user_id != current_user.user_id)
        )
        if existing.scalar_one_or_none() is not None:
            raise DuplicateError(
                field="phone",
                message="This phone number is already registered to another account",
            )

        # Re-fetch user
        res = await db.execute(select(User).where(User.user_id == current_user.user_id))
        user = res.scalar_one()

        old_phone = user.phone
        user.phone = new_phone
        user.is_phone_verified = True

        history_entry = UserPhoneChangeHistory(
            user_id=user.user_id,
            old_phone=old_phone,
            new_phone=new_phone,
            ip_address=client_ip,
        )
        db.add(history_entry)
        await db.commit()
        await db.refresh(user)

        if redis:
            await redis.delete(f"otp:phone_change:{current_user.user_id}:target_phone")

        return ProfileResponse(
            success=True,
            user=UserResponse.from_orm(user),
        )



def _parse_device_name(ua_or_device: Optional[str]) -> str:
    if not ua_or_device:
        return "Mobile Device"
    s = ua_or_device.strip()
    if "sribeesmobile" in s.lower() and "(" in s and ")" in s:
        start = s.find("(") + 1
        end = s.rfind(")")
        extracted = s[start:end].strip()
        if extracted:
            return extracted
    if not s.lower().startswith("mozilla/") and not s.lower().startswith("dart/"):
        return s

    ua = s.lower()
    if "android" in ua:
        if "samsung" in ua or "sm-" in ua:
            return "Samsung Galaxy"
        elif "xiaomi" in ua or "redmi" in ua:
            return "Xiaomi Redmi"
        elif "pixel" in ua:
            return "Google Pixel"
        elif "huawei" in ua:
            return "Huawei Device"
        elif "oppo" in ua:
            return "Oppo Device"
        elif "vivo" in ua:
            return "Vivo Device"
        return "Android Device"
    if "iphone" in ua:
        return "iPhone"
    if "ipad" in ua:
        return "iPad"
    if "windows" in ua:
        return "Windows PC"
    if "macintosh" in ua or "mac os" in ua:
        return "Mac"
    if "linux" in ua:
        return "Linux"
    return "Mobile App"
