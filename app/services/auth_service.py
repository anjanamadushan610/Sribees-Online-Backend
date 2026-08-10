"""
SRIBEESonline FastAPI Backend - Auth Service

Business logic for user authentication.
"""
import secrets
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    InvalidTokenError,
    TokenExpiredError,
    UnverifiedEmailError,
)
from app.core.security import (
    create_token_pair,
    hash_password,
    verify_password,
    verify_token,
)
from app.models.user import EmailVerification, PasswordReset, Session, User
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    MessageResponse,
    RefreshTokenResponse,
    RegisterRequest,
    RegisterResponse,
    TokensResponse,
    UserResponse,
)
from app.utils.logger import logger


class AuthService:
    """Authentication service with user management operations."""

    # ========================================================================
    # Registration
    # ========================================================================

    @staticmethod
    async def register(data: RegisterRequest, db: AsyncSession) -> RegisterResponse:
        """
        Register a new user.

        Args:
            data: Registration request data
            db: Database session

        Returns:
            RegisterResponse with created user

        Raises:
            EmailAlreadyExistsError: If email is already registered
        """
        # Check if email already exists
        result = await db.execute(
            select(User).where(User.email == data.email.lower())
        )
        existing_user = result.scalar_one_or_none()

        if existing_user:
            raise EmailAlreadyExistsError()

        # Hash password
        password_hash = hash_password(data.password)

        # Create user
        user = User(
            user_id=uuid4(),
            email=data.email.lower(),
            password_hash=password_hash,
            full_name=data.full_name.strip(),
            is_verified=False,
        )

        db.add(user)
        await db.flush()  # Get user_id

        # Apply the invite code, if one was supplied. Registration is the right
        # moment for this — the binding is write-once, so doing it here means it
        # can never be re-pointed later.
        #
        # A bad code must not fail the signup: the user has already given us
        # their details, and losing the account over a mistyped code is a worse
        # outcome than losing the attribution.
        if data.referral_code:
            from app.services.referral_service import (
                ReferralError,
                ReferralService,
            )

            try:
                await ReferralService.bind_referrer(
                    db, user, data.referral_code, commit=False
                )
            except ReferralError as exc:
                logger.info(
                    f"Referral code not applied for {user.email}: {exc.code}"
                )

        # Create email verification token
        verification_token = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(hours=24)

        verification = EmailVerification(
            user_id=user.user_id,
            token=verification_token,
            expires_at=expires_at,
        )

        db.add(verification)
        await db.commit()
        await db.refresh(user)

        # TODO: Send verification email
        logger.info(f"Verification token for {user.email}: {verification_token}")

        return RegisterResponse(
            success=True,
            message="Registration successful. Please check your email to verify your account.",
            user=UserResponse(
                user_id=user.user_id,
                email=user.email,
                full_name=user.full_name,
                phone=user.phone,
                profile_picture_url=user.profile_picture_url,
                is_verified=user.is_verified,
                is_phone_verified=user.is_phone_verified,
                two_factor_enabled=user.two_factor_enabled,
                created_at=user.created_at,
                updated_at=user.updated_at,
                last_login=user.last_login,
            ),
        )

    # ========================================================================
    # Login
    # ========================================================================

    @staticmethod
    async def login(data: LoginRequest, db: AsyncSession) -> AuthResponse:
        """
        Authenticate user and return tokens.

        Args:
            data: Login request data
            db: Database session

        Returns:
            AuthResponse with user and tokens

        Raises:
            InvalidCredentialsError: If credentials are invalid
            UnverifiedEmailError: If email is not verified
        """
        # Find user by email
        result = await db.execute(
            select(User).where(User.email == data.email.lower())
        )
        user = result.scalar_one_or_none()

        if not user:
            raise InvalidCredentialsError()

        # Verify password
        if not verify_password(data.password, user.password_hash):
            raise InvalidCredentialsError()

        # Check email verification
        if not user.is_verified:
            raise UnverifiedEmailError()

        # Create session
        session_id = uuid4()
        session_expiry = 30 if data.remember_me else 7  # days
        expires_at = datetime.utcnow() + timedelta(days=session_expiry)

        session = Session(
            session_id=session_id,
            user_id=user.user_id,
            refresh_token_hash="placeholder",  # Will be updated after token generation
            expires_at=expires_at,
        )

        db.add(session)

        # Update last login
        await db.execute(
            update(User)
            .where(User.user_id == user.user_id)
            .values(last_login=datetime.utcnow())
        )

        await db.commit()
        await db.refresh(user)

        # Generate tokens
        tokens = create_token_pair(
            user_id=user.user_id,
            email=user.email,
            session_id=session_id,
            remember_me=data.remember_me,
        )

        logger.info(f"User logged in: {user.email}")

        return AuthResponse(
            success=True,
            message="Login successful",
            user=UserResponse(
                user_id=user.user_id,
                email=user.email,
                full_name=user.full_name,
                phone=user.phone,
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
    # Phone-first registration / login
    # ========================================================================

    @staticmethod
    async def login_or_register_by_phone(phone: str, db: AsyncSession) -> AuthResponse:
        """
        Find-or-create a user by phone number and log them in.

        A verified OTP is the only gate here — the phone number is the
        account's identity key, so the first successful verification silently
        creates the account (name is empty until the user sets it via
        profile) and every verification after that is just a login.

        Args:
            phone: Phone number, already OTP-verified by the caller
            db: Database session

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

        session = Session(
            session_id=session_id,
            user_id=user.user_id,
            refresh_token_hash="placeholder",  # Will be updated after token generation
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
    # Email Verification
    # ========================================================================

    @staticmethod
    async def verify_email(token: str, db: AsyncSession) -> MessageResponse:
        """
        Verify user's email address.

        Args:
            token: Verification token
            db: Database session

        Returns:
            MessageResponse confirming verification

        Raises:
            InvalidTokenError: If token is invalid or expired
        """
        # Find verification record
        result = await db.execute(
            select(EmailVerification).where(EmailVerification.token == token)
        )
        verification = result.scalar_one_or_none()

        if not verification:
            raise InvalidTokenError(message="Invalid or expired verification token")

        # Check expiration
        if datetime.utcnow() > verification.expires_at:
            await db.execute(
                delete(EmailVerification).where(EmailVerification.token == token)
            )
            await db.commit()
            raise TokenExpiredError(message="Verification token has expired")

        # Update user verification status
        await db.execute(
            update(User)
            .where(User.user_id == verification.user_id)
            .values(is_verified=True, updated_at=datetime.utcnow())
        )

        # Delete verification token
        await db.execute(
            delete(EmailVerification).where(EmailVerification.user_id == verification.user_id)
        )

        await db.commit()

        logger.info(f"Email verified for user_id: {verification.user_id}")

        return MessageResponse(
            success=True,
            message="Email verified successfully. You can now log in.",
        )

    @staticmethod
    async def resend_verification_email(email: str, db: AsyncSession) -> MessageResponse:
        """
        Resend verification email.

        Args:
            email: User's email address
            db: Database session

        Returns:
            MessageResponse (always success for security)
        """
        result = await db.execute(
            select(User).where(User.email == email.lower())
        )
        user = result.scalar_one_or_none()

        if user and not user.is_verified:
            # Delete existing verification tokens
            await db.execute(
                delete(EmailVerification).where(EmailVerification.user_id == user.user_id)
            )

            # Create new verification token
            verification_token = secrets.token_urlsafe(32)
            expires_at = datetime.utcnow() + timedelta(hours=24)

            verification = EmailVerification(
                user_id=user.user_id,
                token=verification_token,
                expires_at=expires_at,
            )

            db.add(verification)
            await db.commit()

            # TODO: Send verification email
            logger.info(f"Verification token for {user.email}: {verification_token}")

        return MessageResponse(
            success=True,
            message="If this email is registered, a verification link has been sent.",
        )

    # ========================================================================
    # Password Reset
    # ========================================================================

    @staticmethod
    async def forgot_password(email: str, db: AsyncSession) -> MessageResponse:
        """
        Request password reset.

        Args:
            email: User's email address
            db: Database session

        Returns:
            MessageResponse (always success for security)
        """
        result = await db.execute(
            select(User).where(User.email == email.lower())
        )
        user = result.scalar_one_or_none()

        if user:
            # Delete existing reset tokens
            await db.execute(
                delete(PasswordReset).where(PasswordReset.user_id == user.user_id)
            )

            # Create new reset token
            reset_token = secrets.token_urlsafe(32)
            expires_at = datetime.utcnow() + timedelta(hours=1)

            reset = PasswordReset(
                user_id=user.user_id,
                token=reset_token,
                expires_at=expires_at,
            )

            db.add(reset)
            await db.commit()

            # TODO: Send reset email
            logger.info(f"Password reset token for {user.email}: {reset_token}")

        return MessageResponse(
            success=True,
            message="If this email is registered, a password reset link has been sent.",
        )

    @staticmethod
    async def reset_password(token: str, new_password: str, db: AsyncSession) -> MessageResponse:
        """
        Reset user's password.

        Args:
            token: Password reset token
            new_password: New password
            db: Database session

        Returns:
            MessageResponse confirming reset

        Raises:
            InvalidTokenError: If token is invalid or expired
        """
        # Find reset record
        result = await db.execute(
            select(PasswordReset).where(
                PasswordReset.token == token,
                PasswordReset.used == False,
            )
        )
        reset = result.scalar_one_or_none()

        if not reset:
            raise InvalidTokenError(message="Invalid or expired reset token")

        # Check expiration
        if datetime.utcnow() > reset.expires_at:
            await db.execute(
                delete(PasswordReset).where(PasswordReset.token == token)
            )
            await db.commit()
            raise TokenExpiredError(message="Reset token has expired")

        # Hash new password
        password_hash = hash_password(new_password)

        # Update user password
        await db.execute(
            update(User)
            .where(User.user_id == reset.user_id)
            .values(password_hash=password_hash, updated_at=datetime.utcnow())
        )

        # Mark token as used
        await db.execute(
            update(PasswordReset)
            .where(PasswordReset.token == token)
            .values(used=True)
        )

        # Invalidate all user sessions
        await db.execute(
            delete(Session).where(Session.user_id == reset.user_id)
        )

        await db.commit()

        logger.info(f"Password reset for user_id: {reset.user_id}")

        return MessageResponse(
            success=True,
            message="Password reset successfully. Please log in with your new password.",
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
        Logout user by invalidating session.

        Args:
            current_user: Currently authenticated user
            db: Database session

        Returns:
            MessageResponse confirming logout
        """
        # Delete all sessions for user (or just current session)
        await db.execute(
            delete(Session).where(Session.user_id == current_user.user_id)
        )
        await db.commit()

        logger.info(f"User logged out: {current_user.email}")

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

        logger.info(f"User logged out from all devices: {current_user.email}")

        return MessageResponse(
            success=True,
            message="Logged out from all devices",
        )

    @staticmethod
    async def change_password(
        current_user,
        current_password: str,
        new_password: str,
        db: AsyncSession
    ) -> MessageResponse:
        """
        Change user's password.

        Args:
            current_user: Currently authenticated user
            current_password: Current password for verification
            new_password: New password to set
            db: Database session

        Returns:
            MessageResponse confirming password change
        """
        # Get user with password
        result = await db.execute(
            select(User).where(User.user_id == current_user.user_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            raise InvalidCredentialsError()

        # Verify current password
        if not verify_password(current_password, user.password_hash):
            raise InvalidCredentialsError(message="Current password is incorrect")

        # Hash and set new password
        user.password_hash = hash_password(new_password)
        user.updated_at = datetime.utcnow()

        # Invalidate all sessions for security
        await db.execute(
            delete(Session).where(Session.user_id == user.user_id)
        )

        await db.commit()

        logger.info(f"Password changed for user: {user.email}")

        return MessageResponse(
            success=True,
            message="Password changed successfully. Please login again.",
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
        if data.phone is not None:
            user.phone = data.phone
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

        session_list = [
            SessionResponse(
                session_id=str(s.session_id),
                device=s.device_info,
                ip_address=s.ip_address,
                user_agent=s.user_agent,
                created_at=s.created_at,
                last_active=s.last_active,
                is_current=False,  # Could track current session
            )
            for s in sessions
        ]

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
