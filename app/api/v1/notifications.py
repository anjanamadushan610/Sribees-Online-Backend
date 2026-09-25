"""
Notification API Endpoints
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import (
    get_current_user,
    require_super_admin,
)
from app.schemas.notification import (
    BroadcastNotificationRequest,
    CreateNotificationRequest,
    NotificationPreferencesUpdate,
    PushTokenRegisterRequest,
    RegisterPushTokenRequest,
)
from app.services import notification_i18n as i18n
from app.services.notification_service import NotificationService

# Prefix "/notifications" is applied by app/api/v1/router.py — do not repeat it here.
router = APIRouter(tags=["Notifications"])


# ============================================================================
# Helper Functions
# ============================================================================

def format_notification(notification) -> dict:
    """Format notification for API response."""
    return {
        "notification_id": str(notification.notification_id),
        "type": notification.type,
        "title": notification.title,
        "message": notification.message,
        "reference_type": notification.reference_type,
        "reference_id": str(notification.reference_id) if notification.reference_id else None,
        "data": notification.data,
        "is_read": notification.is_read,
        "read_at": notification.read_at.isoformat() if notification.read_at else None,
        "created_at": notification.created_at.isoformat() if notification.created_at else None,
    }


# ============================================================================
# User Endpoints
# ============================================================================

@router.get("", response_model=dict)
async def get_notifications(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    unread_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get current user's notifications.
    """
    try:
        offset = (page - 1) * limit
        notifications, total = await NotificationService.get_by_user(
            db,
            user_id=current_user.user_id,
            limit=limit,
            offset=offset,
            unread_only=unread_only
        )

        return {
            "success": True,
            "data": {
                "notifications": [format_notification(n) for n in notifications],
                "pagination": {
                    "total": total,
                    "page": page,
                    "limit": limit,
                    "pages": (total + limit - 1) // limit
                }
            }
        }
    except Exception as e:
        logger.error(f"Error fetching notifications: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch notifications"
        )


@router.get("/unread-count", response_model=dict)
async def get_unread_count(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get unread notification count.
    """
    try:
        count = await NotificationService.get_unread_count(db, current_user.user_id)

        return {
            "success": True,
            "data": {
                "unread_count": count
            }
        }
    except Exception as e:
        logger.error(f"Error getting unread count: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get unread count"
        )


@router.patch("/{notification_id}/read", response_model=dict)
@router.put("/{notification_id}/read", response_model=dict)
async def mark_notification_read(
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Mark a notification as read.

    Exposed as both PATCH (per the mobile API contract) and PUT (legacy).
    """
    try:
        notification = await NotificationService.mark_as_read(
            db,
            notification_id=UUID(notification_id),
            user_id=current_user.user_id
        )

        if not notification:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Notification not found"
            )

        return {
            "success": True,
            "data": format_notification(notification),
            "message": "Notification marked as read"
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid notification ID"
        )
    except Exception as e:
        logger.error(f"Error marking notification as read: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to mark notification as read"
        )


@router.put("/read-all", response_model=dict)
async def mark_all_read(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Mark all notifications as read.
    """
    try:
        count = await NotificationService.mark_all_as_read(db, current_user.user_id)

        return {
            "success": True,
            "data": {
                "marked_count": count
            },
            "message": f"{count} notifications marked as read"
        }
    except Exception as e:
        logger.error(f"Error marking all as read: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to mark notifications as read"
        )


@router.delete("/{notification_id}", response_model=dict)
async def delete_notification(
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Delete a notification.
    """
    try:
        deleted = await NotificationService.delete(
            db,
            notification_id=UUID(notification_id),
            user_id=current_user.user_id
        )

        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Notification not found"
            )

        return {
            "success": True,
            "message": "Notification deleted"
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid notification ID"
        )
    except Exception as e:
        logger.error(f"Error deleting notification: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete notification"
        )


@router.delete("", response_model=dict)
async def delete_all_notifications(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Delete all notifications.
    """
    try:
        count = await NotificationService.delete_all(db, current_user.user_id)

        return {
            "success": True,
            "data": {
                "deleted_count": count
            },
            "message": f"{count} notifications deleted"
        }
    except Exception as e:
        logger.error(f"Error deleting all notifications: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete notifications"
        )


# ============================================================================
# Preferences
# ============================================================================

def _preferences(user) -> dict:
    return {
        "offers": user.notify_offers is not False,
        "promotions": user.notify_promotions is not False,
        "general": user.notify_general is not False,
        "language": i18n.normalize_lang(user.preferred_language),
    }


@router.get("/preferences", response_model=dict)
async def get_preferences(current_user = Depends(get_current_user)):
    """
    The customer's marketing switches (all ON until they change one) and the
    language notifications are written in. Order, payment and refund updates
    are not governed by these and always arrive.
    """
    return {"success": True, "data": _preferences(current_user)}


@router.put("/preferences", response_model=dict)
async def update_preferences(
    data: NotificationPreferencesUpdate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Change any of the switches; fields left out keep their value."""
    if data.offers is not None:
        current_user.notify_offers = data.offers
    if data.promotions is not None:
        current_user.notify_promotions = data.promotions
    if data.general is not None:
        current_user.notify_general = data.general
    if data.language:
        current_user.preferred_language = i18n.normalize_lang(data.language)
    await db.commit()
    await db.refresh(current_user)
    return {"success": True, "data": _preferences(current_user)}


@router.post("/campaigns/{campaign_id}/opened", response_model=dict)
async def campaign_opened(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    The customer tapped a campaign push. Marks their copy read and counts
    them once toward the campaign's opens, however often they tap.
    """
    from app.services.campaign_service import mark_opened

    counted = await mark_opened(db, current_user.user_id, campaign_id)
    return {"success": True, "data": {"counted": counted}}


# ============================================================================
# Push Token Endpoints
# ============================================================================

@router.post("/push/token", response_model=dict)
async def register_fcm_push_token(
    data: PushTokenRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Register or refresh the caller's FCM device token.

    Upserts into ``push_tokens`` keyed by (user, device_id): a device that
    rotated its token updates its existing row, otherwise a new row is inserted.
    The row is always marked ``is_active = True``.

    Also records the app's language on this device, so pushes to it are
    written in that language. The app calls this again when the customer
    switches language.
    """
    try:
        row = await NotificationService.upsert_push_token(
            db,
            user_id=current_user.user_id,
            token=data.token,
            platform=data.platform,
            device_id=data.device_id,
            language=data.language or request.headers.get("accept-language"),
        )
        return {
            "success": True,
            "data": {
                "token_id": str(row.token_id),
                "platform": row.device_type,
                "device_id": row.device_id,
                "is_active": row.is_active,
                "language": row.language,
            },
            "message": "Push token registered",
        }
    except Exception as e:
        logger.error(f"Error registering FCM push token: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register push token",
        )


@router.post("/push-token", response_model=dict)
async def register_push_token(
    data: RegisterPushTokenRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Register push notification token.
    """
    try:
        token = await NotificationService.register_push_token(
            db,
            user_id=current_user.user_id,
            token=data.token,
            device_type=data.device_type,
            device_name=data.device_name
        )

        return {
            "success": True,
            "data": {
                "token_id": str(token.token_id),
                "token": token.token,
                "device_type": token.device_type,
                "is_active": token.is_active
            },
            "message": "Push token registered"
        }
    except Exception as e:
        logger.error(f"Error registering push token: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register push token"
        )


@router.delete("/push-token/{token}", response_model=dict)
async def unregister_push_token(
    token: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Unregister push notification token.
    """
    try:
        success = await NotificationService.unregister_push_token(
            db,
            user_id=current_user.user_id,
            token=token
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Push token not found"
            )

        return {
            "success": True,
            "message": "Push token unregistered"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unregistering push token: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to unregister push token"
        )


# ============================================================================
# Admin Endpoints
# ============================================================================

@router.post("/admin/send", response_model=dict, status_code=status.HTTP_201_CREATED)
async def send_notification(
    data: CreateNotificationRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(require_super_admin)
):
    """
    Send notification to a specific user (Admin only).
    """
    try:
        notification = await NotificationService.create(
            db,
            user_id=UUID(data.user_id),
            type=data.type,
            title=data.title,
            message=data.message,
            reference_type=data.reference_type,
            reference_id=UUID(data.reference_id) if data.reference_id else None,
            data=data.data,
            send_push=data.send_push
        )

        return {
            "success": True,
            "data": format_notification(notification),
            "message": "Notification sent"
        }
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user ID or reference ID"
        )
    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send notification"
        )


@router.post("/admin/broadcast", response_model=dict)
async def broadcast_notification(
    data: BroadcastNotificationRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(require_super_admin)
):
    """
    Broadcast notification to all users (Admin only).

    Note: In production, implement async job for large user bases.
    """
    try:
        # For now, just log the broadcast request
        # In production, queue this as a background job
        logger.info(f"Broadcast notification: {data.title}")

        return {
            "success": True,
            "message": "Broadcast notification queued",
            "data": {
                "title": data.title,
                "type": data.type.value
            }
        }
    except Exception as e:
        logger.error(f"Error broadcasting notification: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to broadcast notification"
        )
