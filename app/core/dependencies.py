"""
SRIBEESonline FastAPI Backend - Common Dependencies

Reusable FastAPI dependencies for authentication, database, and utilities.
"""
from typing import Annotated, Optional
from uuid import UUID

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis
from app.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    InsufficientPermissionsError,
    InvalidTokenError,
    TokenExpiredError,
    UnverifiedEmailError,
)
from app.core.security import verify_token

# Security scheme for JWT Bearer authentication
security = HTTPBearer(auto_error=False)


# ============================================================================
# Authentication Dependencies
# ============================================================================

async def get_current_user(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(security)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """
    Get the currently authenticated user from JWT token.

    Args:
        credentials: Bearer token from Authorization header
        db: Database session

    Returns:
        dict: User data from database

    Raises:
        AuthenticationError: If no token provided
        InvalidTokenError: If token is invalid
        TokenExpiredError: If token has expired
    """
    if credentials is None:
        raise AuthenticationError(message="Authentication required")

    token = credentials.credentials
    payload = verify_token(token, token_type="access")

    if payload is None:
        raise InvalidTokenError()

    # Realm separation: an admin or partner token must never authenticate as a
    # customer. The user lookup below would fail anyway, but relying on "that
    # id happens not to exist in this table" is not a security boundary.
    if payload.get("is_admin") or payload.get("is_partner"):
        raise InvalidTokenError(message="Wrong token type for this endpoint")

    user_id = payload.get("sub")
    if not user_id:
        raise InvalidTokenError()

    # Import here to avoid circular imports
    from app.models.user import User

    result = await db.execute(
        select(User).where(User.user_id == UUID(user_id))
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise InvalidTokenError(message="User not found")

    if user.is_blocked:
        raise AuthorizationError(
            message=f"Your account has been suspended: {user.blocked_reason or 'No reason provided'}",
            error_code="ACCOUNT_BLOCKED"
        )

    if not user.is_active:
        raise AuthorizationError(
            message="Your account is inactive.",
            error_code="ACCOUNT_INACTIVE"
        )

    if not user.is_verified:
        raise UnverifiedEmailError()

    return user


async def get_current_user_optional(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(security)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Optional[dict]:
    """
    Get the currently authenticated user if token provided, otherwise None.

    Useful for endpoints that work for both authenticated and anonymous users.

    Args:
        credentials: Bearer token from Authorization header (optional)
        db: Database session

    Returns:
        User data if authenticated, None otherwise
    """
    if credentials is None:
        return None

    try:
        return await get_current_user(credentials, db)
    except (AuthenticationError, InvalidTokenError, TokenExpiredError):
        return None


async def get_current_admin(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(security)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """
    Get the currently authenticated admin user.

    Args:
        credentials: Bearer token from Authorization header
        db: Database session

    Returns:
        dict: Admin user data

    Raises:
        AuthenticationError: If no token or not an admin
    """
    if credentials is None:
        raise AuthenticationError(message="Admin authentication required")

    token = credentials.credentials
    payload = verify_token(token, token_type="access")

    if payload is None:
        raise InvalidTokenError()

    admin_id = payload.get("sub")
    if not admin_id:
        raise InvalidTokenError()

    # Check if it's an admin token. A partner token carries is_partner and no
    # is_admin, so it is refused here — the professional referral team has no
    # route into the admin surface even if a role check elsewhere is wrong.
    if payload.get("is_partner"):
        raise AuthenticationError(message="Admin access required")

    is_admin = payload.get("is_admin", False)
    if not is_admin:
        raise AuthenticationError(message="Admin access required")

    # Import here to avoid circular imports
    from app.models.admin import Admin

    result = await db.execute(
        select(Admin).where(Admin.admin_id == UUID(admin_id))
    )
    admin = result.scalar_one_or_none()

    if admin is None:
        raise InvalidTokenError(message="Admin not found")

    if not admin.is_active:
        raise AuthenticationError(message="Admin account is deactivated")

    return admin


async def get_current_partner(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(security)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """
    Authenticate a professional referral partner.

    Partners are a separate identity store from admins and customers, and this
    is the only dependency that accepts their tokens. It requires the
    ``is_partner`` claim positively — a token without it is refused even if it
    is otherwise valid, so an admin or customer token cannot reach /partner/*
    any more than a partner token can reach /admin/*.
    """
    if credentials is None:
        raise AuthenticationError(message="Partner authentication required")

    payload = verify_token(credentials.credentials, token_type="access")
    if payload is None:
        raise InvalidTokenError()

    if not payload.get("is_partner"):
        raise AuthenticationError(message="Partner access required")

    partner_id = payload.get("sub")
    if not partner_id:
        raise InvalidTokenError()

    from app.models.partner import Partner

    result = await db.execute(
        select(Partner).where(Partner.partner_id == UUID(partner_id))
    )
    partner = result.scalar_one_or_none()

    if partner is None:
        raise InvalidTokenError(message="Partner not found")

    if not partner.is_active:
        raise AuthenticationError(message="Partner account is deactivated")

    return partner


async def require_super_admin(
    admin = Depends(get_current_admin),
) -> dict:
    """
    Require the current user to be a Super Admin.
    """
    from app.models.admin import AdminRole

    if admin.role != AdminRole.SUPER_ADMIN:
        raise InsufficientPermissionsError(message="Super Admin access required")

    return admin


# ============================================================================
# RBAC Dependencies
# ============================================================================

def require_roles(*allowed_roles: str):
    """
    Factory function to create a role-checking dependency.

    Args:
        *allowed_roles: Role names that are allowed access

    Returns:
        Dependency function that validates admin role

    Usage:
        @router.get("/admin-only")
        async def admin_endpoint(
            admin: AdminUser = Depends(require_roles("super_admin", "branch_manager"))
        ):
            ...
    """
    async def role_checker(
        admin: Annotated[dict, Depends(get_current_admin)],
    ) -> dict:
        role_str = admin.role.value if hasattr(admin.role, "value") else str(admin.role)
        if role_str not in allowed_roles:
            raise InsufficientPermissionsError(
                required_roles=list(allowed_roles),
                message=f"This action requires one of these roles: {', '.join(allowed_roles)}",
            )
        return admin

    return role_checker


# Pre-defined role dependencies
RequireSuperAdmin = Depends(require_roles("super_admin"))
RequireBranchManager = Depends(require_roles("super_admin", "branch_manager"))
RequireMarketingManager = Depends(require_roles("super_admin", "branch_manager", "marketing_manager"))
RequireInventory = Depends(require_roles("super_admin", "branch_manager", "inventory_manager"))


async def require_base_role_admin(
    admin: Annotated[dict, Depends(get_current_admin)],
) -> dict:
    """
    Require one of the 5 original base roles — NOT a delegated staff account.

    Staff accounts can never create or manage further staff (hierarchy is
    capped at one level): gate every staff/role-management endpoint with
    this, not with a role-string allow-list, so the restriction can't be
    quietly widened by adding "staff" to some future require_roles() call.
    """
    from app.models.admin import AdminRole

    if admin.role == AdminRole.STAFF:
        raise InsufficientPermissionsError(
            message="Staff accounts cannot create or manage other staff accounts.",
        )
    return admin


def require_permission(resource: str, action: str):
    """
    Factory for a dependency that checks a concrete (resource, action)
    permission against the admin's DB-derived effective permission set
    (their role's row in role_permissions) — the granular counterpart to
    require_roles(), which only checks the coarse role name.

    Usage:
        @router.get("/widgets")
        async def list_widgets(
            admin: AdminUser = Depends(require_permission("products", "read"))
        ):
            ...
    """
    async def permission_checker(
        admin: Annotated[dict, Depends(get_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> dict:
        from app.services.permission_service import PermissionService

        effective = await PermissionService.get_effective_permissions(db, admin)
        if (resource, action) not in effective:
            raise InsufficientPermissionsError(
                message=f"This action requires the '{resource}:{action}' permission.",
            )
        return admin

    return permission_checker


# ============================================================================
# Branch Isolation
# ============================================================================

# Roles that see every branch's data rather than being pinned to one.
# Super Admin manages the whole network; Customer Support is a helpdesk role
# that has to be able to look up any customer's order regardless of which
# branch fulfilled it — a support agent isn't "assigned to" one store the way
# a Branch Manager, Marketing Manager or Inventory Manager is.
UNSCOPED_ROLES = {"super_admin", "customer_support"}


class BranchScope:
    """
    Resolved branch-visibility scope for an authenticated admin.

    - ``is_super_admin`` reflects the literal role — use it only for
      super-admin-specific powers (e.g. "may force a branch_id on write").
    - ``is_unscoped`` is the broader "sees every branch" characteristic
      (Super Admin *and* Customer Support); use this for read/visibility
      decisions. Everyone else is pinned to their assigned ``branch_id``.
    """

    def __init__(self, is_super_admin: bool, is_unscoped: bool, branch_id: Optional[UUID]):
        self.is_super_admin = is_super_admin
        self.is_unscoped = is_unscoped
        self.branch_id = branch_id

    def resolve(self, requested_branch_id: Optional[UUID] = None) -> Optional[UUID]:
        """
        Return the branch_id a query should filter on (None = all branches).

        Unscoped admins may optionally narrow to ``requested_branch_id``;
        scoped admins always resolve to their own branch regardless of the
        request.
        """
        if self.is_unscoped:
            return requested_branch_id
        return self.branch_id


async def inject_branch_filter(
    admin: Annotated[dict, Depends(get_current_admin)],
) -> BranchScope:
    """
    Branch-isolation dependency.

    Yields a :class:`BranchScope` describing what the current admin may see.
    Scoped admins (everyone outside ``UNSCOPED_ROLES``) without an assigned
    branch are rejected — they have no valid scope to operate within.
    """
    role = admin.role.value if hasattr(admin.role, "value") else admin.role
    is_super = role == "super_admin"
    is_unscoped = role in UNSCOPED_ROLES

    if not is_unscoped and admin.branch_id is None:
        raise InsufficientPermissionsError(
            message="Your admin account is not assigned to a branch.",
        )

    return BranchScope(is_super_admin=is_super, is_unscoped=is_unscoped, branch_id=admin.branch_id)


# ============================================================================
# Pagination Dependencies
# ============================================================================

class PaginationParams:
    """Pagination parameters dependency."""

    def __init__(
        self,
        page: int = 1,
        limit: int = 20,
        sort_by: Optional[str] = None,
        sort_order: str = "desc",
    ):
        self.page = max(1, page)
        self.limit = min(100, max(1, limit))  # Max 100 items per page
        self.offset = (self.page - 1) * self.limit
        self.sort_by = sort_by
        self.sort_order = sort_order.lower() if sort_order in ["asc", "desc"] else "desc"


def get_pagination(
    page: int = 1,
    limit: int = 20,
    sort_by: Optional[str] = None,
    sort_order: str = "desc",
) -> PaginationParams:
    """
    Get pagination parameters from query string.

    Args:
        page: Page number (1-indexed)
        limit: Items per page (max 100)
        sort_by: Field to sort by
        sort_order: Sort direction (asc/desc)

    Returns:
        PaginationParams: Validated pagination parameters
    """
    return PaginationParams(page, limit, sort_by, sort_order)


# ============================================================================
# Request Context Dependencies
# ============================================================================

def get_client_ip(request: Request) -> str:
    """
    Get client IP address from request.

    Handles X-Forwarded-For header for proxied requests.

    Args:
        request: FastAPI request object

    Returns:
        str: Client IP address
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def get_user_agent(
    user_agent: Annotated[Optional[str], Header(alias="User-Agent")] = None,
) -> Optional[str]:
    """
    Get User-Agent header from request.

    Args:
        user_agent: User-Agent header value

    Returns:
        str: User agent string or None
    """
    return user_agent


# ============================================================================
# Type Aliases for Dependencies
# ============================================================================

# Database session dependency
DBSession = Annotated[AsyncSession, Depends(get_db)]

# Redis client dependency
RedisClient = Annotated[Redis, Depends(get_redis)]

# Current user dependencies
CurrentUser = Annotated[dict, Depends(get_current_user)]
CurrentUserOptional = Annotated[Optional[dict], Depends(get_current_user_optional)]

# Current admin dependency
CurrentAdmin = Annotated[dict, Depends(get_current_admin)]

# Current partner dependency (professional referral team)
CurrentPartner = Annotated[dict, Depends(get_current_partner)]

# Pagination dependency
Pagination = Annotated[PaginationParams, Depends(get_pagination)]
