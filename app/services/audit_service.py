"""
SRIBEESonline - Audit Logging Service

Comprehensive audit trail for security-sensitive operations.
Logs user actions, admin operations, and system events.
"""
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy import Column, DateTime, Index, String, Text, and_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import Base


class AuditAction(str, Enum):
    """Audit action types."""
    # Authentication
    LOGIN = "auth.login"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"
    PASSWORD_CHANGE = "auth.password_change"
    PASSWORD_RESET = "auth.password_reset"
    TOKEN_REFRESH = "auth.token_refresh"

    # User Management
    USER_CREATE = "user.create"
    USER_UPDATE = "user.update"
    USER_DELETE = "user.delete"
    USER_SUSPEND = "user.suspend"
    USER_ACTIVATE = "user.activate"

    # Admin Operations
    ADMIN_LOGIN = "admin.login"
    ADMIN_LOGIN_FAILED = "admin.login_failed"
    ADMIN_CREATE = "admin.create"
    ADMIN_UPDATE = "admin.update"
    ADMIN_DELETE = "admin.delete"
    ADMIN_PERMISSION_CHANGE = "admin.permission_change"

    # Orders
    ORDER_CREATE = "order.create"
    ORDER_UPDATE = "order.update"
    ORDER_CANCEL = "order.cancel"
    ORDER_REFUND = "order.refund"
    ORDER_STATUS_CHANGE = "order.status_change"

    # Products
    PRODUCT_CREATE = "product.create"
    PRODUCT_UPDATE = "product.update"
    PRODUCT_DELETE = "product.delete"
    PRODUCT_PRICE_CHANGE = "product.price_change"
    PRODUCT_STOCK_CHANGE = "product.stock_change"

    # Payments
    PAYMENT_INITIATE = "payment.initiate"
    PAYMENT_SUCCESS = "payment.success"
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_REFUND = "payment.refund"

    # Security Events
    SECURITY_RATE_LIMIT = "security.rate_limit"
    SECURITY_INVALID_TOKEN = "security.invalid_token"
    SECURITY_PERMISSION_DENIED = "security.permission_denied"
    SECURITY_SUSPICIOUS_ACTIVITY = "security.suspicious_activity"

    # System
    SYSTEM_CONFIG_CHANGE = "system.config_change"
    SYSTEM_BACKUP = "system.backup"
    SYSTEM_MAINTENANCE = "system.maintenance"


class AuditSeverity(str, Enum):
    """Audit event severity levels."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AuditLog(Base):
    """Audit log database model."""

    __tablename__ = "audit_logs"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)

    # Timestamp
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Action info
    action = Column(String(100), nullable=False, index=True)
    severity = Column(String(20), default="info", nullable=False)

    # Actor (who performed the action)
    actor_type = Column(String(20), nullable=False)  # user, admin, system
    actor_id = Column(PGUUID(as_uuid=True), nullable=True, index=True)
    actor_email = Column(String(255), nullable=True)
    actor_ip = Column(String(45), nullable=True)  # IPv6 max length
    actor_user_agent = Column(String(500), nullable=True)

    # Target (what was affected)
    target_type = Column(String(50), nullable=True)  # user, order, product, etc.
    target_id = Column(PGUUID(as_uuid=True), nullable=True, index=True)

    # Details
    description = Column(Text, nullable=True)
    # Python attribute can't be named `metadata` — that name is reserved by
    # SQLAlchemy's declarative API (every mapped class inherits Base.metadata,
    # a MetaData instance); the DB column itself is still named "metadata".
    event_metadata = Column("metadata", JSONB, nullable=True)

    # Request context
    request_id = Column(String(36), nullable=True)
    endpoint = Column(String(255), nullable=True)
    method = Column(String(10), nullable=True)

    # Indexes for common queries
    __table_args__ = (
        Index("ix_audit_logs_action_created", "action", "created_at"),
        Index("ix_audit_logs_actor_created", "actor_id", "created_at"),
        Index("ix_audit_logs_target_created", "target_type", "target_id", "created_at"),
    )


class AuditService:
    """Service for audit logging operations."""

    @staticmethod
    async def log(
        db: AsyncSession,
        action: AuditAction,
        actor_type: str = "system",
        actor_id: Optional[UUID] = None,
        actor_email: Optional[str] = None,
        actor_ip: Optional[str] = None,
        actor_user_agent: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[UUID] = None,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        severity: AuditSeverity = AuditSeverity.INFO,
        request_id: Optional[str] = None,
        endpoint: Optional[str] = None,
        method: Optional[str] = None,
    ) -> AuditLog:
        """
        Create an audit log entry.

        Args:
            db: Database session
            action: The action being logged
            actor_type: Type of actor (user, admin, system)
            actor_id: ID of the actor
            actor_email: Email of the actor
            actor_ip: IP address of the actor
            actor_user_agent: User agent string
            target_type: Type of target resource
            target_id: ID of the target resource
            description: Human-readable description
            metadata: Additional context data
            severity: Event severity level
            request_id: Request correlation ID
            endpoint: API endpoint
            method: HTTP method

        Returns:
            Created AuditLog entry
        """
        # Sanitize metadata - remove sensitive fields
        if metadata:
            sanitized_metadata = {
                k: v for k, v in metadata.items()
                if k.lower() not in {"password", "token", "secret", "api_key", "credit_card"}
            }
        else:
            sanitized_metadata = None

        audit_log = AuditLog(
            action=action.value,
            severity=severity.value,
            actor_type=actor_type,
            actor_id=actor_id,
            actor_email=actor_email,
            actor_ip=actor_ip,
            actor_user_agent=actor_user_agent,
            target_type=target_type,
            target_id=target_id,
            description=description,
            event_metadata=sanitized_metadata,
            request_id=request_id,
            endpoint=endpoint,
            method=method,
        )

        db.add(audit_log)
        await db.commit()
        await db.refresh(audit_log)

        # Also log to application logger for immediate visibility
        log_msg = f"AUDIT: {action.value} | actor={actor_type}:{actor_id} | target={target_type}:{target_id}"
        if severity == AuditSeverity.CRITICAL:
            logger.critical(log_msg)
        elif severity == AuditSeverity.ERROR:
            logger.error(log_msg)
        elif severity == AuditSeverity.WARNING:
            logger.warning(log_msg)
        else:
            logger.info(log_msg)

        return audit_log

    @staticmethod
    async def log_auth_event(
        db: AsyncSession,
        action: AuditAction,
        user_id: Optional[UUID],
        email: str,
        ip_address: str,
        user_agent: Optional[str] = None,
        success: bool = True,
        failure_reason: Optional[str] = None,
    ) -> AuditLog:
        """Log authentication events."""
        severity = AuditSeverity.INFO if success else AuditSeverity.WARNING
        description = f"{'Successful' if success else 'Failed'} {action.value.split('.')[-1]} for {email}"

        metadata = {"success": success}
        if failure_reason:
            metadata["failure_reason"] = failure_reason

        return await AuditService.log(
            db=db,
            action=action,
            actor_type="user" if "admin" not in action.value else "admin",
            actor_id=user_id,
            actor_email=email,
            actor_ip=ip_address,
            actor_user_agent=user_agent,
            description=description,
            metadata=metadata,
            severity=severity,
        )

    @staticmethod
    async def log_admin_action(
        db: AsyncSession,
        action: AuditAction,
        admin_id: UUID,
        admin_email: str,
        target_type: str,
        target_id: Optional[UUID],
        description: str,
        ip_address: Optional[str] = None,
        changes: Optional[Dict[str, Any]] = None,
    ) -> AuditLog:
        """Log admin operations."""
        metadata = {}
        if changes:
            # Log before/after for changes
            metadata["changes"] = changes

        return await AuditService.log(
            db=db,
            action=action,
            actor_type="admin",
            actor_id=admin_id,
            actor_email=admin_email,
            actor_ip=ip_address,
            target_type=target_type,
            target_id=target_id,
            description=description,
            metadata=metadata,
            severity=AuditSeverity.INFO,
        )

    @staticmethod
    async def log_security_event(
        db: AsyncSession,
        action: AuditAction,
        ip_address: str,
        description: str,
        user_id: Optional[UUID] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AuditLog:
        """Log security-related events."""
        return await AuditService.log(
            db=db,
            action=action,
            actor_type="system",
            actor_id=user_id,
            actor_ip=ip_address,
            description=description,
            metadata=metadata,
            severity=AuditSeverity.WARNING,
        )

    @staticmethod
    async def get_logs(
        db: AsyncSession,
        action: Optional[AuditAction] = None,
        actor_id: Optional[UUID] = None,
        target_type: Optional[str] = None,
        target_id: Optional[UUID] = None,
        severity: Optional[AuditSeverity] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[AuditLog]:
        """Query audit logs with filters."""
        query = select(AuditLog)

        conditions = []

        if action:
            conditions.append(AuditLog.action == action.value)
        if actor_id:
            conditions.append(AuditLog.actor_id == actor_id)
        if target_type:
            conditions.append(AuditLog.target_type == target_type)
        if target_id:
            conditions.append(AuditLog.target_id == target_id)
        if severity:
            conditions.append(AuditLog.severity == severity.value)
        if start_date:
            conditions.append(AuditLog.created_at >= start_date)
        if end_date:
            conditions.append(AuditLog.created_at <= end_date)

        if conditions:
            query = query.where(and_(*conditions))

        query = query.order_by(AuditLog.created_at.desc())
        query = query.limit(limit).offset(offset)

        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def get_user_activity(
        db: AsyncSession,
        user_id: UUID,
        days: int = 30,
        limit: int = 100,
    ) -> List[AuditLog]:
        """Get recent activity for a specific user."""
        start_date = datetime.utcnow() - timedelta(days=days)

        query = select(AuditLog).where(
            and_(
                AuditLog.actor_id == user_id,
                AuditLog.created_at >= start_date,
            )
        ).order_by(AuditLog.created_at.desc()).limit(limit)

        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def get_security_events(
        db: AsyncSession,
        hours: int = 24,
        limit: int = 100,
    ) -> List[AuditLog]:
        """Get recent security events."""
        start_date = datetime.utcnow() - timedelta(hours=hours)

        security_actions = [
            AuditAction.LOGIN_FAILED.value,
            AuditAction.ADMIN_LOGIN_FAILED.value,
            AuditAction.SECURITY_RATE_LIMIT.value,
            AuditAction.SECURITY_INVALID_TOKEN.value,
            AuditAction.SECURITY_PERMISSION_DENIED.value,
            AuditAction.SECURITY_SUSPICIOUS_ACTIVITY.value,
        ]

        query = select(AuditLog).where(
            and_(
                AuditLog.action.in_(security_actions),
                AuditLog.created_at >= start_date,
            )
        ).order_by(AuditLog.created_at.desc()).limit(limit)

        result = await db.execute(query)
        return result.scalars().all()
