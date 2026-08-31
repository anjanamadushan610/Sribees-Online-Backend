"""
Notification Service

Push notifications using Firebase Cloud Messaging (FCM).
"""
from datetime import datetime
from typing import List, Optional, Tuple
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification, PushToken
from app.schemas.notification import NotificationTypeEnum
from app.services.fcm_service import FCMService


class NotificationService:
    """Service class for notification operations."""

    # ========================================================================
    # Notification CRUD
    # ========================================================================

    @staticmethod
    async def create(
        db: AsyncSession,
        user_id: UUID,
        type: NotificationTypeEnum,
        title: str,
        message: str,
        reference_type: Optional[str] = None,
        reference_id: Optional[UUID] = None,
        data: Optional[dict] = None,
        send_push: bool = True
    ) -> Notification:
        """Create a notification."""
        notification = Notification(
            user_id=user_id,
            type=type.value,
            title=title,
            message=message,
            reference_type=reference_type,
            reference_id=reference_id,
            data=data
        )

        db.add(notification)
        await db.commit()
        await db.refresh(notification)

        # Send push notification
        if send_push:
            await NotificationService.send_push_notification(
                db, user_id, title, message, data
            )

        logger.info(f"Notification created: {notification.notification_id} for user {user_id}")

        return notification

    @staticmethod
    async def get_by_user(
        db: AsyncSession,
        user_id: UUID,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False
    ) -> Tuple[List[Notification], int]:
        """Get user's notifications."""
        query = select(Notification).where(Notification.user_id == user_id)

        if unread_only:
            query = query.where(Notification.is_read == False)

        # Count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar()

        # Get notifications
        query = query.order_by(Notification.created_at.desc()).limit(limit).offset(offset)
        result = await db.execute(query)
        notifications = result.scalars().all()

        return notifications, total

    @staticmethod
    async def get_unread_count(db: AsyncSession, user_id: UUID) -> int:
        """Get unread notification count."""
        result = await db.execute(
            select(func.count(Notification.notification_id))
            .where(and_(
                Notification.user_id == user_id,
                Notification.is_read == False
            ))
        )
        return result.scalar() or 0

    @staticmethod
    async def mark_as_read(
        db: AsyncSession,
        notification_id: UUID,
        user_id: UUID
    ) -> Optional[Notification]:
        """Mark notification as read."""
        result = await db.execute(
            select(Notification).where(and_(
                Notification.notification_id == notification_id,
                Notification.user_id == user_id
            ))
        )
        notification = result.scalar_one_or_none()

        if notification and not notification.is_read:
            notification.is_read = True
            notification.read_at = datetime.utcnow()
            await db.commit()
            await db.refresh(notification)

        return notification

    @staticmethod
    async def mark_all_as_read(db: AsyncSession, user_id: UUID) -> int:
        """Mark all notifications as read."""
        result = await db.execute(
            update(Notification)
            .where(and_(
                Notification.user_id == user_id,
                Notification.is_read == False
            ))
            .values(is_read=True, read_at=datetime.utcnow())
        )
        await db.commit()
        return result.rowcount

    @staticmethod
    async def delete(
        db: AsyncSession,
        notification_id: UUID,
        user_id: UUID
    ) -> bool:
        """Delete a notification."""
        result = await db.execute(
            select(Notification).where(and_(
                Notification.notification_id == notification_id,
                Notification.user_id == user_id
            ))
        )
        notification = result.scalar_one_or_none()

        if notification:
            await db.delete(notification)
            await db.commit()
            return True

        return False

    @staticmethod
    async def delete_all(db: AsyncSession, user_id: UUID) -> int:
        """Delete all notifications for user."""
        result = await db.execute(
            select(Notification).where(Notification.user_id == user_id)
        )
        notifications = result.scalars().all()
        count = len(notifications)

        for n in notifications:
            await db.delete(n)

        await db.commit()
        return count

    # ========================================================================
    # Push Token Management
    # ========================================================================

    @staticmethod
    async def register_push_token(
        db: AsyncSession,
        user_id: UUID,
        token: str,
        device_type: Optional[str] = None,
        device_name: Optional[str] = None
    ) -> PushToken:
        """Register or update push notification token."""
        # Check if token exists
        result = await db.execute(
            select(PushToken).where(PushToken.token == token)
        )
        existing = result.scalar_one_or_none()

        if existing:
            # Update existing token
            existing.user_id = user_id
            existing.device_type = device_type
            existing.device_name = device_name
            existing.is_active = True
            await db.commit()
            await db.refresh(existing)
            return existing

        # Create new token
        push_token = PushToken(
            user_id=user_id,
            token=token,
            device_type=device_type,
            device_name=device_name
        )

        db.add(push_token)
        await db.commit()
        await db.refresh(push_token)

        return push_token

    @staticmethod
    async def upsert_push_token(
        db: AsyncSession,
        user_id: UUID,
        token: str,
        platform: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> PushToken:
        """
        Register or refresh an FCM device token, keyed by (user_id, device_id).

        Handles FCM token rotation cleanly:
          - If the device already has a row, its token is updated in place.
          - If the incoming token is already held by a *different* row (unique
            on ``token``), that stale row is removed first to avoid a conflict.
          - Otherwise a known token is reassigned to this user/device, or a new
            row is inserted.

        Always ends with ``is_active = True``.
        """
        # Prefer the device's existing row so a rotated token updates in place.
        device_row = None
        if device_id:
            result = await db.execute(
                select(PushToken).where(and_(
                    PushToken.user_id == user_id,
                    PushToken.device_id == device_id,
                ))
            )
            device_row = result.scalar_one_or_none()

        # Any row already holding this exact token (``token`` is globally unique).
        result = await db.execute(select(PushToken).where(PushToken.token == token))
        token_row = result.scalar_one_or_none()

        if device_row is not None:
            # Device known → refresh its token. Drop any other row that already
            # holds the new token to respect the unique(token) constraint.
            if token_row is not None and token_row.token_id != device_row.token_id:
                await db.delete(token_row)
                await db.flush()
            device_row.token = token
            if platform:
                device_row.device_type = platform
            device_row.is_active = True
            row = device_row
        elif token_row is not None:
            # Token known but no device row → reassign to this user/device.
            token_row.user_id = user_id
            if platform:
                token_row.device_type = platform
            if device_id:
                token_row.device_id = device_id
            token_row.is_active = True
            row = token_row
        else:
            row = PushToken(
                user_id=user_id,
                token=token,
                device_type=platform,
                device_id=device_id,
                is_active=True,
            )
            db.add(row)

        await db.commit()
        await db.refresh(row)
        logger.info(f"Push token upserted for user {user_id} (device {device_id})")
        return row

    @staticmethod
    async def unregister_push_token(
        db: AsyncSession,
        user_id: UUID,
        token: str
    ) -> bool:
        """Unregister/deactivate push token."""
        result = await db.execute(
            select(PushToken).where(and_(
                PushToken.user_id == user_id,
                PushToken.token == token
            ))
        )
        push_token = result.scalar_one_or_none()

        if push_token:
            push_token.is_active = False
            await db.commit()
            return True

        return False

    @staticmethod
    async def get_user_tokens(db: AsyncSession, user_id: UUID) -> List[PushToken]:
        """Get active push tokens for user."""
        result = await db.execute(
            select(PushToken).where(and_(
                PushToken.user_id == user_id,
                PushToken.is_active == True
            ))
        )
        return result.scalars().all()

    # ========================================================================
    # Push Notification Sending (Firebase Cloud Messaging)
    # ========================================================================

    @staticmethod
    async def send_push_notification(
        db: AsyncSession,
        user_id: UUID,
        title: str,
        body: str,
        data: Optional[dict] = None
    ) -> bool:
        """Send push notification via Firebase Cloud Messaging."""
        tokens = await NotificationService.get_user_tokens(db, user_id)

        if not tokens:
            logger.debug(f"No push tokens for user {user_id}")
            return False

        # Convert data to string values (FCM requirement)
        str_data = None
        if data:
            str_data = {k: str(v) for k, v in data.items()}

        # Get token strings
        token_strings = [t.token for t in tokens]

        # Send via FCM
        if len(token_strings) == 1:
            # Single token
            success = await FCMService.send_to_token(
                token=token_strings[0],
                title=title,
                body=body,
                data=str_data
            )
            if success:
                logger.info(f"Push notification sent to user {user_id}")
            return success
        else:
            # Multiple tokens
            result = await FCMService.send_to_tokens(
                tokens=token_strings,
                title=title,
                body=body,
                data=str_data
            )

            # Deactivate failed tokens (unregistered devices)
            for failed_token in result.get("failed_tokens", []):
                await NotificationService._deactivate_token(db, failed_token)

            success = result.get("success_count", 0) > 0
            if success:
                logger.info(
                    f"Push notification sent to user {user_id}: "
                    f"{result['success_count']}/{len(token_strings)} devices"
                )
            return success

    @staticmethod
    async def _deactivate_token(db: AsyncSession, token: str) -> None:
        """Deactivate a push token that's no longer valid."""
        try:
            await db.execute(
                update(PushToken)
                .where(PushToken.token == token)
                .values(is_active=False)
            )
            await db.commit()
            logger.debug(f"Deactivated invalid push token: {token[:20]}...")
        except Exception as e:
            logger.error(f"Failed to deactivate token: {e}")

    # ========================================================================
    # Notification Templates
    # ========================================================================

    @staticmethod
    async def notify_order_status(
        db: AsyncSession,
        user_id: UUID,
        order_number: str,
        status: str
    ) -> Notification:
        """Send order status notification."""
        status_messages = {
            "confirmed": "Your order has been confirmed",
            "processing": "Your order is being processed",
            "shipped": "Your order has been shipped",
            "out_for_delivery": "Your order is out for delivery",
            "delivered": "Your order has been delivered",
            "cancelled": "Your order has been cancelled"
        }

        message = status_messages.get(status, f"Order status updated to {status}")

        return await NotificationService.create(
            db,
            user_id=user_id,
            type=NotificationTypeEnum.ORDER_STATUS,
            title=f"Order #{order_number}",
            message=message,
            reference_type="order",
            data={"order_number": order_number, "status": status}
        )

    @staticmethod
    async def notify_payment_success(
        db: AsyncSession,
        user_id: UUID,
        order_number: str,
        amount: float
    ) -> Notification:
        """Send payment success notification."""
        return await NotificationService.create(
            db,
            user_id=user_id,
            type=NotificationTypeEnum.PAYMENT,
            title="Payment Successful",
            message=f"Payment of ₹{amount:.2f} received for order #{order_number}",
            reference_type="order",
            data={"order_number": order_number, "amount": amount}
        )

    @staticmethod
    async def notify_price_drop(
        db: AsyncSession,
        user_id: UUID,
        product_name: str,
        old_price: float,
        new_price: float,
        product_id: str
    ) -> Notification:
        """Send price drop notification."""
        drop_percent = ((old_price - new_price) / old_price) * 100

        return await NotificationService.create(
            db,
            user_id=user_id,
            type=NotificationTypeEnum.PRICE_DROP,
            title="Price Drop Alert! 🎉",
            message=f"{product_name} is now ₹{new_price:.2f} ({drop_percent:.0f}% off)",
            reference_type="product",
            reference_id=UUID(product_id) if product_id else None,
            data={
                "product_id": product_id,
                "old_price": old_price,
                "new_price": new_price,
                "drop_percent": drop_percent
            }
        )
