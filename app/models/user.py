"""
SRIBEESonline FastAPI Backend - User Model

SQLAlchemy model for the users table.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import relationship

from app.config.database import Base


class User(Base):
    """
    User model representing customer accounts.

    Maps to the 'users' table in PostgreSQL.
    """

    __tablename__ = "users"

    # Primary key
    user_id: UUID = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        nullable=False,
    )

    # Account info
    # Nullable: phone-registered accounts (Module — phone-first sign-up) have
    # neither an email nor a password until the user later adds them.
    email: Optional[str] = Column(
        String(255),
        unique=True,
        nullable=True,
        index=True,
    )
    password_hash: Optional[str] = Column(
        String(255),
        nullable=True,
    )
    full_name: str = Column(
        String(100),
        nullable=False,
    )
    # Unique: for phone-registered accounts this is the account's identity
    # key. Stays nullable — Postgres allows multiple NULLs in a unique
    # column, so email-only accounts without a phone are unaffected.
    phone: Optional[str] = Column(
        String(20),
        unique=True,
        nullable=True,
        index=True,
    )
    profile_picture_url: Optional[str] = Column(
        Text,
        nullable=True,
    )

    # Verification status
    is_verified: bool = Column(
        Boolean,
        default=False,
        nullable=False,
    )
    is_phone_verified: bool = Column(
        Boolean,
        default=False,
        nullable=False,
    )

    # Referrals
    referral_code: Optional[str] = Column(
        String(12),
        unique=True,
        nullable=True,
        index=True,
        comment="This user's own invite code. Generated on first request.",
    )
    referred_by_user_id: Optional[UUID] = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment=(
            "Who invited this user. WRITE ONCE — never update after it is set, "
            "or the commission ledger stops being auditable."
        ),
    )
    referred_by_partner_id: Optional[UUID] = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("partners.partner_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment=(
            "Professional partner who signed this customer up. WRITE ONCE, and "
            "mutually exclusive with referred_by_user_id."
        ),
    )
    referred_at: Optional[datetime] = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Two-factor authentication
    two_factor_enabled: bool = Column(
        Boolean,
        default=False,
        nullable=False,
    )
    two_factor_secret: Optional[str] = Column(
        String(255),
        nullable=True,
    )

    # Timestamps
    created_at: datetime = Column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: datetime = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    last_login: Optional[datetime] = Column(
        DateTime,
        nullable=True,
    )

    # User role for authorization
    role: str = Column(
        String(20),
        default="customer",  # customer, admin, manager
        nullable=False,
    )
    is_active: bool = Column(
        Boolean,
        default=True,
        nullable=False,
    )
    is_blocked: bool = Column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    blocked_reason: Optional[str] = Column(
        Text,
        nullable=True,
    )
    first_name: Optional[str] = Column(
        String(50),
        nullable=True,
    )
    last_name: Optional[str] = Column(
        String(50),
        nullable=True,
    )

    # Relationships
    addresses = relationship("Address", back_populates="user", cascade="all, delete-orphan")
    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="user")
    reviews = relationship("Review", back_populates="user")
    wishlist_items = relationship("WishlistItem", back_populates="user", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")
    push_tokens = relationship("PushToken", back_populates="user", cascade="all, delete-orphan")
    wallet = relationship(
        "Wallet",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User(user_id={self.user_id}, email={self.email})>"

    def to_dict(self, include_sensitive: bool = False) -> dict:
        """
        Convert user to dictionary.

        Args:
            include_sensitive: Include sensitive fields like password_hash

        Returns:
            dict: User data
        """
        data = {
            "userId": str(self.user_id),
            "email": self.email,
            "fullName": self.full_name,
            "phone": self.phone,
            "profilePictureUrl": self.profile_picture_url,
            "isVerified": self.is_verified,
            "twoFactorEnabled": self.two_factor_enabled,
            "isBlocked": self.is_blocked,
            "blockedReason": self.blocked_reason,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "lastLogin": self.last_login.isoformat() if self.last_login else None,
        }

        if include_sensitive:
            data["passwordHash"] = self.password_hash
            data["twoFactorSecret"] = self.two_factor_secret

        return data


class EmailVerification(Base):
    """
    Email verification token model.

    Maps to the 'email_verifications' table in PostgreSQL.
    """

    __tablename__ = "email_verifications"

    verification_id: UUID = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        nullable=False,
    )
    user_id: UUID = Column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    token: str = Column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )
    expires_at: datetime = Column(
        DateTime,
        nullable=False,
    )
    created_at: datetime = Column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<EmailVerification(verification_id={self.verification_id}, user_id={self.user_id})>"


class PasswordReset(Base):
    """
    Password reset token model.

    Maps to the 'password_resets' table in PostgreSQL.
    """

    __tablename__ = "password_resets"

    reset_id: UUID = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        nullable=False,
    )
    user_id: UUID = Column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    token: str = Column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )
    expires_at: datetime = Column(
        DateTime,
        nullable=False,
    )
    used: bool = Column(
        Boolean,
        default=False,
        nullable=False,
    )
    created_at: datetime = Column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<PasswordReset(reset_id={self.reset_id}, user_id={self.user_id})>"


class Session(Base):
    """
    User session model.

    Maps to the 'sessions' table in PostgreSQL.
    Serves as fallback when Redis is unavailable.
    """

    __tablename__ = "sessions"

    session_id: UUID = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        nullable=False,
    )
    user_id: UUID = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.user_id"),
        nullable=False,
        index=True,
    )
    refresh_token_hash: str = Column(
        String(255),
        nullable=False,
    )
    ip_address: Optional[str] = Column(
        String(45),  # Support IPv6
        nullable=True,
    )
    user_agent: Optional[str] = Column(
        Text,
        nullable=True,
    )
    expires_at: datetime = Column(
        DateTime,
        nullable=False,
    )
    created_at: datetime = Column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )

    # Relationship
    user = relationship("User", back_populates="sessions")

    def __repr__(self) -> str:
        return f"<Session(session_id={self.session_id}, user_id={self.user_id})>"


class Address(Base):
    """
    User address model.

    Maps to the 'addresses' table in PostgreSQL.
    """

    __tablename__ = "addresses"

    address_id: UUID = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        nullable=False,
    )
    user_id: UUID = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.user_id"),
        nullable=False,
        index=True,
    )
    # Contact/label metadata for the saved address (nullable — the delivery
    # branch is resolved from post_office, not from these).
    title: Optional[str] = Column(
        String(100),
        nullable=True,
    )
    recipient_name: Optional[str] = Column(
        String(150),
        nullable=True,
    )
    phone: Optional[str] = Column(
        String(30),
        nullable=True,
    )
    address_line1: str = Column(
        String(255),
        nullable=False,
    )
    address_line2: Optional[str] = Column(
        String(255),
        nullable=True,
    )
    post_office: str = Column(
        String(100),
        nullable=False,
    )
    district: str = Column(
        String(100),
        nullable=False,
    )
    district: str = Column(
        String(100),
        nullable=False,
    )
    postal_code: str = Column(
        String(20),
        nullable=False,
    )
    province: str = Column(
        String(100),
        nullable=False,
    )
    is_default: bool = Column(
        Boolean,
        default=False,
        nullable=False,
    )
    # Cached SribeesExpress city_id for this address (contract section 5a --
    # "cache the result ... only when an address is first saved or changed").
    # Every rate quote and booking is keyed on this, so an unresolved address
    # cannot be quoted by the courier at all -- checkout falls back to the
    # flat platform fee. courier_resolve_reason is set only when resolution
    # came back resolved: false, and says which of the four reasons it was.
    courier_city_id: Optional[int] = Column(Integer, nullable=True)
    courier_city_name: Optional[str] = Column(String(120), nullable=True)
    courier_resolve_reason: Optional[str] = Column(String(40), nullable=True)
    courier_resolved_at: Optional[datetime] = Column(DateTime(timezone=True), nullable=True)
    created_at: datetime = Column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: datetime = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationship
    user = relationship("User", back_populates="addresses")

    def __repr__(self) -> str:
        return f"<Address(address_id={self.address_id}, user_id={self.user_id})>"

    def to_dict(self) -> dict:
        """Convert address to dictionary."""
        return {
            "addressId": str(self.address_id),
            "userId": str(self.user_id),
            "addressLine1": self.address_line1,
            "addressLine2": self.address_line2,
            "postOffice": self.post_office,
            "district": self.district,
            "postalCode": self.postal_code,
            "province": self.province,
            "isDefault": self.is_default,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }
