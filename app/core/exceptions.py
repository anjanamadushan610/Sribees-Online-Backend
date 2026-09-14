"""
SRIBEESonline FastAPI Backend - Custom Exceptions

Application-specific exceptions with proper HTTP error handling.
"""
from typing import Any, Dict, Optional

from fastapi import HTTPException, status


class AppException(HTTPException):
    """
    Base application exception.

    Extends HTTPException with additional context.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.message = message
        self.error_code = error_code
        self.details = details or {}

        detail = {
            "success": False,
            "error": {
                "message": message,
                "code": error_code,
                **self.details,
            }
        }

        super().__init__(status_code=status_code, detail=detail)


# ============================================================================
# Authentication Exceptions
# ============================================================================

class AuthenticationError(AppException):
    """Raised when authentication fails."""

    def __init__(
        self,
        message: str = "Authentication failed",
        error_code: str = "AUTH_FAILED",
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message=message,
            error_code=error_code,
            details=details,
        )


class InvalidCredentialsError(AuthenticationError):
    """Raised when login credentials are invalid."""

    def __init__(self, message: str = "Invalid email or password"):
        super().__init__(
            message=message,
            error_code="INVALID_CREDENTIALS",
        )


class TokenExpiredError(AuthenticationError):
    """Raised when JWT token has expired."""

    def __init__(self, message: str = "Token has expired"):
        super().__init__(
            message=message,
            error_code="TOKEN_EXPIRED",
        )


class InvalidTokenError(AuthenticationError):
    """Raised when JWT token is invalid."""

    def __init__(self, message: str = "Invalid token"):
        super().__init__(
            message=message,
            error_code="INVALID_TOKEN",
        )


class UnverifiedEmailError(AppException):
    """Raised when user's email is not verified."""

    def __init__(self, message: str = "Please verify your email address before logging in"):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            message=message,
            error_code="EMAIL_NOT_VERIFIED",
        )


# ============================================================================
# Authorization Exceptions
# ============================================================================

class AuthorizationError(AppException):
    """Raised when user lacks permission for an action."""

    def __init__(
        self,
        message: str = "You don't have permission to perform this action",
        error_code: str = "FORBIDDEN",
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            message=message,
            error_code=error_code,
            details=details,
        )


class InsufficientPermissionsError(AuthorizationError):
    """Raised when user doesn't have required role/permission."""

    def __init__(
        self,
        required_roles: Optional[list] = None,
        message: str = "Insufficient permissions",
    ):
        details = {}
        if required_roles:
            details["required_roles"] = required_roles

        super().__init__(
            message=message,
            error_code="INSUFFICIENT_PERMISSIONS",
            details=details,
        )


class BranchAccessDeniedError(AuthorizationError):
    """Raised when admin tries to access resources outside their branch."""

    def __init__(self, message: str = "Access denied for this branch"):
        super().__init__(
            message=message,
            error_code="BRANCH_ACCESS_DENIED",
        )


# ============================================================================
# Resource Exceptions
# ============================================================================

class NotFoundError(AppException):
    """Raised when a requested resource is not found."""

    def __init__(
        self,
        resource: str = "Resource",
        identifier: Optional[str] = None,
        message: Optional[str] = None,
    ):
        if message is None:
            if identifier:
                message = f"{resource} with ID '{identifier}' not found"
            else:
                message = f"{resource} not found"

        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            message=message,
            error_code="NOT_FOUND",
            details={"resource": resource},
        )


class UserNotFoundError(NotFoundError):
    """Raised when a user is not found."""

    def __init__(self, identifier: Optional[str] = None):
        super().__init__(resource="User", identifier=identifier)


class ProductNotFoundError(NotFoundError):
    """Raised when a product is not found."""

    def __init__(self, identifier: Optional[str] = None):
        super().__init__(resource="Product", identifier=identifier)


class OrderNotFoundError(NotFoundError):
    """Raised when an order is not found."""

    def __init__(self, identifier: Optional[str] = None):
        super().__init__(resource="Order", identifier=identifier)


class CategoryNotFoundError(NotFoundError):
    """Raised when a category is not found."""

    def __init__(self, identifier: Optional[str] = None):
        super().__init__(resource="Category", identifier=identifier)


class LocationNotServedError(AppException):
    """Raised when a valid location is not yet mapped to any active branch."""

    def __init__(
        self,
        postal_city: Optional[str] = None,
        district: Optional[str] = None,
        province: Optional[str] = None,
    ):
        parts = [v for v in (postal_city, district, province) if v]
        location_label = ", ".join(parts) if parts else "selected location"

        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            message=(
                f"The location '{location_label}' is not currently served by any branch. "
                "Please choose a different address or contact support."
            ),
            error_code="LOCATION_NOT_SERVED",
            details={
                "postal_city": postal_city,
                # Legacy key: installed app builds read it (see branch_service).
                "post_office": postal_city,
                "district": district,
                "province": province,
            },
        )


# Backward-compatible alias
BranchNotServedError = LocationNotServedError


# ============================================================================
# Validation Exceptions
# ============================================================================

class ValidationError(AppException):
    """Raised when input validation fails."""

    def __init__(
        self,
        message: str = "Validation error",
        errors: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            message=message,
            error_code="VALIDATION_ERROR",
            details={"errors": errors} if errors else {},
        )


class DuplicateError(AppException):
    """Raised when attempting to create a duplicate resource."""

    def __init__(
        self,
        field: str = "resource",
        message: Optional[str] = None,
    ):
        if message is None:
            message = f"A record with this {field} already exists"

        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            message=message,
            error_code="DUPLICATE_ERROR",
            details={"field": field},
        )


class EmailAlreadyExistsError(DuplicateError):
    """Raised when email is already registered."""

    def __init__(self):
        super().__init__(
            field="email",
            message="Email already registered",
        )


# ============================================================================
# Business Logic Exceptions
# ============================================================================

class BusinessError(AppException):
    """Raised for business logic violations."""

    def __init__(
        self,
        message: str,
        error_code: str = "BUSINESS_ERROR",
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            message=message,
            error_code=error_code,
            details=details,
        )


class InsufficientStockError(BusinessError):
    """Raised when product stock is insufficient."""

    def __init__(
        self,
        product_name: str,
        requested: int,
        available: int,
    ):
        super().__init__(
            message=f"Insufficient stock for '{product_name}'. Requested: {requested}, Available: {available}",
            error_code="INSUFFICIENT_STOCK",
            details={
                "product_name": product_name,
                "requested": requested,
                "available": available,
            },
        )


class OrderCancellationError(BusinessError):
    """Raised when order cannot be cancelled."""

    def __init__(self, reason: str = "Order cannot be cancelled"):
        super().__init__(
            message=reason,
            error_code="ORDER_CANCELLATION_FAILED",
        )


class PaymentError(BusinessError):
    """Raised when payment processing fails."""

    def __init__(
        self,
        message: str = "Payment processing failed",
        provider_error: Optional[str] = None,
    ):
        details = {}
        if provider_error:
            details["provider_error"] = provider_error

        super().__init__(
            message=message,
            error_code="PAYMENT_FAILED",
            details=details,
        )


class InvalidCouponError(BusinessError):
    """Raised when coupon is invalid or expired."""

    def __init__(self, message: str = "Invalid or expired coupon"):
        super().__init__(
            message=message,
            error_code="INVALID_COUPON",
        )


# ============================================================================
# Rate Limiting Exceptions
# ============================================================================

class RateLimitExceededError(AppException):
    """Raised when rate limit is exceeded."""

    def __init__(
        self,
        message: str = "Too many requests. Please try again later.",
        retry_after: Optional[int] = None,
    ):
        details = {}
        if retry_after:
            details["retry_after"] = retry_after

        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            message=message,
            error_code="RATE_LIMIT_EXCEEDED",
            details=details,
        )


# ============================================================================
# Server Exceptions
# ============================================================================

class InternalServerError(AppException):
    """Raised for unexpected server errors."""

    def __init__(
        self,
        message: str = "An unexpected error occurred",
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=message,
            error_code="INTERNAL_ERROR",
            details=details,
        )


class ServiceUnavailableError(AppException):
    """Raised when a required service is unavailable."""

    def __init__(
        self,
        service: str = "Service",
        message: Optional[str] = None,
    ):
        if message is None:
            message = f"{service} is temporarily unavailable"

        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            message=message,
            error_code="SERVICE_UNAVAILABLE",
            details={"service": service},
        )


class DatabaseError(ServiceUnavailableError):
    """Raised when database operation fails."""

    def __init__(self, message: str = "Database error occurred"):
        super().__init__(service="Database", message=message)


class RedisError(ServiceUnavailableError):
    """Raised when Redis operation fails."""

    def __init__(self, message: str = "Cache service error occurred"):
        super().__init__(service="Cache", message=message)
