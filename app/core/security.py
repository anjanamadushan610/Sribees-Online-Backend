"""
SRIBEESonline FastAPI Backend - Security Utilities

JWT token management and password hashing using argon2.
"""
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID

import jwt
from passlib.context import CryptContext

from app.config.settings import settings

# Password hashing context using Argon2
pwd_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
    argon2__memory_cost=65536,
    argon2__time_cost=3,
    argon2__parallelism=4,
)


# ============================================================================
# Password Hashing
# ============================================================================

def hash_password(password: str) -> str:
    """
    Hash a password using Argon2.

    Args:
        password: Plain text password

    Returns:
        str: Hashed password
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against a hash.

    Args:
        plain_password: Plain text password to verify
        hashed_password: Stored password hash

    Returns:
        bool: True if password matches, False otherwise
    """
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception:
        return False


# ============================================================================
# JWT Token Management
# ============================================================================

def create_access_token(
    data: dict[str, Any],
    expires_delta: Optional[timedelta] = None
) -> str:
    """
    Create a JWT access token.

    Args:
        data: Payload data to encode in token
        expires_delta: Optional custom expiration time

    Returns:
        str: Encoded JWT token
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            minutes=settings.jwt_access_token_expire_minutes
        )

    to_encode.update({
        "exp": expire,
        "iat": datetime.utcnow(),
        "type": "access",
    })

    return jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm
    )


def create_refresh_token(
    data: dict[str, Any],
    expires_delta: Optional[timedelta] = None,
    remember_me: bool = False
) -> str:
    """
    Create a JWT refresh token.

    Args:
        data: Payload data to encode in token
        expires_delta: Optional custom expiration time
        remember_me: If True, use extended expiration (30 days)

    Returns:
        str: Encoded JWT refresh token
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    elif remember_me:
        expire = datetime.utcnow() + timedelta(days=30)
    else:
        expire = datetime.utcnow() + timedelta(
            days=settings.jwt_refresh_token_expire_days
        )

    to_encode.update({
        "exp": expire,
        "iat": datetime.utcnow(),
        "type": "refresh",
    })

    return jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm
    )


def create_token_pair(
    user_id: str | UUID,
    email: str,
    session_id: str | UUID,
    remember_me: bool = False,
    is_admin: bool = False,
    role: Optional[str] = None,
) -> dict[str, str]:
    """
    Create both access and refresh tokens.

    Args:
        user_id: User's unique identifier
        email: User's email address
        session_id: Session identifier
        remember_me: If True, extend refresh token expiration
        is_admin: If True, mark as admin token
        role: Admin role if applicable

    Returns:
        dict: Contains 'access_token' and 'refresh_token'
    """
    payload = {
        "sub": str(user_id),
        "email": email,
        "session_id": str(session_id),
    }

    if is_admin:
        payload["is_admin"] = True
        if role:
            payload["role"] = role

    return {
        "access_token": create_access_token(payload),
        "refresh_token": create_refresh_token(payload, remember_me=remember_me),
    }


def decode_token(token: str) -> Optional[dict[str, Any]]:
    """
    Decode and validate a JWT token.

    Args:
        token: JWT token to decode

    Returns:
        dict: Decoded token payload, or None if invalid
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm]
        )
        return payload
    except jwt.PyJWTError:
        return None


def verify_token(token: str, token_type: str = "access") -> Optional[dict[str, Any]]:
    """
    Verify a JWT token and check its type.

    Args:
        token: JWT token to verify
        token_type: Expected token type ('access' or 'refresh')

    Returns:
        dict: Decoded payload if valid, None otherwise
    """
    payload = decode_token(token)

    if payload is None:
        return None

    if payload.get("type") != token_type:
        return None

    return payload


# ============================================================================
# Token Extraction
# ============================================================================

def extract_token_from_header(authorization: str) -> Optional[str]:
    """
    Extract bearer token from Authorization header.

    Args:
        authorization: Authorization header value (e.g., "Bearer xxx")

    Returns:
        str: Token if valid bearer format, None otherwise
    """
    if not authorization:
        return None

    parts = authorization.split()

    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    return parts[1]
