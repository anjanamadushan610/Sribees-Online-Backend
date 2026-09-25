"""
Notification Service

Every customer notification is written here: an in-app row in the customer's
language, and a push (Firebase Cloud Messaging) to each of their devices in
that device's language, titled with the app's name.

Two rules decide whether anything is sent at all:

* **Category.** ``transactional`` (orders, payments, refunds, wallet) always
  goes. ``offers`` / ``promotions`` / ``general`` are marketing, and a
  customer who switched one off in the app gets neither the row nor the push.
* **Push is off the request.** The row is committed where it is created; the
  push -- token lookup, the order-photo collage, the FCM call -- runs as a
  background task on its own session, so a checkout never waits on Firebase.
"""
import asyncio
import weakref
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification, PushToken
from app.schemas.notification import NotificationTypeEnum
from app.services import notification_i18n as i18n
from app.services.fcm_service import FCMService

# Marketing categories a customer can switch off (users.notify_<category>).
CATEGORY_TRANSACTIONAL = "transactional"
MARKETING_CATEGORIES = ("offers", "promotions", "general")

# At most this many pushes are being prepared at once. A sweep that settles
# cashback for a thousand customers must not open a thousand DB sessions.
# One semaphore per event loop: a semaphore is bound to the loop it first
# waits on, and tests run each case in a fresh loop.
_PUSH_CONCURRENCY = 8
_push_slots: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_background: set = set()


def _slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slots = _push_slots.get(loop)
    if slots is None:
        slots = _push_slots[loop] = asyncio.Semaphore(_PUSH_CONCURRENCY)
    return slots


def wants(user, category: str) -> bool:
    """Whether ``user`` has this category switched on (transactional always is)."""
    if category not in MARKETING_CATEGORIES:
        return True
    return getattr(user, f"notify_{category}", True) is not False


def _spawn(coro) -> None:
    """Run ``coro`` in the background, keeping a reference so it is not collected."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _string_data(data: Optional[dict]) -> Dict[str, str]:
    """FCM data values must be strings; drop the Nones and the bulky lists."""
    out: Dict[str, str] = {}
    for key, value in (data or {}).items():
        if value is None or key == "images":
            continue
        out[key] = str(value)
    return out


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
        send_push: bool = True,
        category: str = CATEGORY_TRANSACTIONAL,
    ) -> Optional[Notification]:
        """
        Write a notification whose copy is already written (one language).

        Prefer :meth:`notify` with a template, which speaks the customer's
        language. Returns None when the customer has ``category`` switched off.
        """
        return await NotificationService._write(
            db,
            user_id=user_id,
            type=type,
            texts=i18n.literal_all(title, message),
            standalone=False,
            category=category,
            reference_type=reference_type,
            reference_id=reference_id,
            data=data,
            send_push=send_push,
        )

    @staticmethod
    async def notify(
        db: AsyncSession,
        user_id: UUID,
        *,
        template: str,
        type: NotificationTypeEnum,
        params: Optional[dict] = None,
        per_lang_params=None,
        category: str = CATEGORY_TRANSACTIONAL,
        reference_type: Optional[str] = None,
        reference_id: Optional[UUID] = None,
        data: Optional[dict] = None,
        images: Optional[List[str]] = None,
        send_push: bool = True,
    ) -> Optional[Notification]:
        """
        Write a templated notification (see ``notification_i18n.TEMPLATES``).

        ``images`` are absolute product-photo URLs: the app's feed shows them
        as thumbnails and the push carries them as one picture.
        """
        return await NotificationService._write(
            db,
            user_id=user_id,
            type=type,
            texts=i18n.render_all(template, params, per_lang_params),
            standalone=i18n.is_standalone(template),
            category=category,
            reference_type=reference_type,
            reference_id=reference_id,
            data=data,
            images=images,
            send_push=send_push,
        )

    @staticmethod
    async def _write(
        db: AsyncSession,
        *,
        user_id: UUID,
        type,
        texts: Dict[str, Tuple[str, str]],
        standalone: bool,
        category: str,
        reference_type: Optional[str] = None,
        reference_id: Optional[UUID] = None,
        data: Optional[dict] = None,
        images: Optional[List[str]] = None,
        send_push: bool = True,
    ) -> Optional[Notification]:
        from app.models.user import User

        user = await db.get(User, user_id)
        if user is not None and not wants(user, category):
            logger.debug(f"{user_id} has {category} notifications off; nothing sent")
            return None

        lang = i18n.normalize_lang(getattr(user, "preferred_language", None))
        title, message = texts[lang]
        payload = dict(data or {})
        if images:
            payload["images"] = list(images)[:4]

        notification = Notification(
            user_id=user_id,
            type=getattr(type, "value", type),
            title=title,
            message=message,
            reference_type=reference_type,
            reference_id=reference_id,
            data=payload or None,
        )
        db.add(notification)
        await db.commit()
        await db.refresh(notification)
        logger.info(f"Notification created: {notification.notification_id} for user {user_id}")

        if send_push:
            push_data = {
                **payload,
                "type": payload.get("type") or getattr(type, "value", type),
                "notification_id": str(notification.notification_id),
            }
            if reference_type and reference_id and f"{reference_type}_id" not in push_data:
                push_data[f"{reference_type}_id"] = str(reference_id)
            _spawn(
                NotificationService._push_in_background(
                    user_id, texts, standalone, push_data, images or [], lang
                )
            )
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
            campaign_id = (notification.data or {}).get("campaign_id")
            if campaign_id:
                from app.services.campaign_service import count_open

                await count_open(db, campaign_id)
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
        language: Optional[str] = None,
    ) -> PushToken:
        """
        Register or refresh an FCM device token, keyed by (user_id, device_id).

        ``language`` is the app's current language on that device. It is kept
        on the token (pushes are rendered per device) and becomes the user's
        ``preferred_language`` (in-app rows are written in it).

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

        if language:
            lang = i18n.normalize_lang(language)
            row.language = lang
            from app.models.user import User

            await db.execute(
                update(User).where(User.user_id == user_id).values(preferred_language=lang)
            )

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
        data: Optional[dict] = None,
        category: str = CATEGORY_TRANSACTIONAL,
    ) -> bool:
        """
        Push already-written copy to every device of one user, now.

        Titled with the app's name; ``title`` becomes the body's first line.
        For callers that keep their own in-app row (or none).
        """
        if category != CATEGORY_TRANSACTIONAL:
            from app.models.user import User

            user = await db.get(User, user_id)
            if user is not None and not wants(user, category):
                return False
        return await NotificationService.push_localized(
            db, user_id, i18n.literal_all(title, body), standalone=False, data=data
        )

    @staticmethod
    async def push_localized(
        db: AsyncSession,
        user_id: UUID,
        texts: Dict[str, Tuple[str, str]],
        *,
        standalone: bool,
        data: Optional[dict] = None,
        image_url: Optional[str] = None,
        fallback_lang: Optional[str] = None,
    ) -> bool:
        """
        One push per language among this user's devices, each in its own words.

        A token with no recorded language (registered before the app sent
        one) reads the user's preferred language, then English.
        """
        tokens = await NotificationService.get_user_tokens(db, user_id)
        if not tokens:
            logger.debug(f"No push tokens for user {user_id}")
            return False

        if fallback_lang is None:
            from app.models.user import User

            user = await db.get(User, user_id)
            fallback_lang = getattr(user, "preferred_language", None)

        groups: Dict[str, List[str]] = defaultdict(list)
        for t in tokens:
            groups[i18n.normalize_lang(t.language or fallback_lang)].append(t.token)

        str_data = _string_data(data)
        delivered = 0
        for lang, token_strings in groups.items():
            title, message = texts.get(lang) or texts[i18n.DEFAULT_LANGUAGE]
            result = await FCMService.send_to_tokens(
                tokens=token_strings,
                title=i18n.BRAND_TITLE,
                body=i18n.push_body(title, message, standalone),
                data=str_data,
                image_url=image_url,
            )
            delivered += result.get("success_count", 0)
            for dead in result.get("invalid_tokens", []):
                await NotificationService._deactivate_token(db, dead)

        if delivered:
            logger.info(f"Push sent to user {user_id}: {delivered}/{len(tokens)} devices")
        return delivered > 0

    @staticmethod
    async def _push_in_background(
        user_id: UUID,
        texts: Dict[str, Tuple[str, str]],
        standalone: bool,
        data: dict,
        images: List[str],
        fallback_lang: str,
    ) -> None:
        """The push half of :meth:`_write`, on its own session. Never raises."""
        from app.config.database import async_session_maker
        from app.services.notification_media import push_image

        async with _slots():
            try:
                image_url = await push_image(images) if images else None
                async with async_session_maker() as session:
                    await NotificationService.push_localized(
                        session,
                        user_id,
                        texts,
                        standalone=standalone,
                        data=data,
                        image_url=image_url,
                        fallback_lang=fallback_lang,
                    )
            except Exception:
                logger.exception(f"Push to {user_id} failed; the in-app notification is saved")

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
    async def _order_context(
        db: AsyncSession, order_number: str, order_id: Optional[UUID]
    ) -> Tuple[Optional[UUID], List[str]]:
        """The order's id (looked up by number when not given) and its product photos."""
        from app.models.order import Order
        from app.services.notification_media import order_images

        if order_id is None and order_number:
            order_id = await db.scalar(
                select(Order.order_id).where(Order.order_number == order_number)
            )
        try:
            images = await order_images(db, order_id)
        except Exception as exc:
            logger.warning(f"No photos for order {order_number}: {exc}")
            images = []
        return order_id, images

    @staticmethod
    async def notify_order_placed(
        db: AsyncSession,
        user_id: UUID,
        order_number: str,
        order_id: Optional[UUID] = None,
        status: Optional[str] = None,
    ) -> Optional[Notification]:
        """Your order has been placed, with the order's photos."""
        order_id, images = await NotificationService._order_context(db, order_number, order_id)
        return await NotificationService.notify(
            db,
            user_id,
            template="order.placed",
            type=NotificationTypeEnum.ORDER_STATUS,
            params={"order_number": order_number},
            reference_type="order",
            reference_id=order_id,
            data={
                "order_number": order_number,
                "status": status,
                "order_id": str(order_id) if order_id else None,
            },
            images=images,
        )

    @staticmethod
    async def notify_order_status(
        db: AsyncSession,
        user_id: UUID,
        order_number: str,
        status: str,
        order_id: Optional[UUID] = None,
    ) -> Optional[Notification]:
        """Send an order status notification, with the order's photos."""
        order_id, images = await NotificationService._order_context(db, order_number, order_id)
        return await NotificationService.notify(
            db,
            user_id,
            template=f"order.status.{status}",
            type=NotificationTypeEnum.ORDER_STATUS,
            params={"order_number": order_number},
            reference_type="order",
            reference_id=order_id,
            data={
                "order_number": order_number,
                "status": status,
                "order_id": str(order_id) if order_id else None,
            },
            images=images,
        )

    @staticmethod
    async def notify_payment_success(
        db: AsyncSession,
        user_id: UUID,
        order_number: str,
        amount: float,
        order_id: Optional[UUID] = None,
    ) -> Optional[Notification]:
        """Send payment success notification."""
        return await NotificationService.notify(
            db,
            user_id,
            template="payment.success",
            type=NotificationTypeEnum.PAYMENT,
            params={"order_number": order_number, "amount": i18n.money(amount)},
            reference_type="order",
            reference_id=order_id,
            data={
                "order_number": order_number,
                "amount": amount,
                "order_id": str(order_id) if order_id else None,
            },
        )

    @staticmethod
    async def notify_price_drop(
        db: AsyncSession,
        user_id: UUID,
        product_name: str,
        old_price: float,
        new_price: float,
        product_id: str,
        image_url: Optional[str] = None,
    ) -> Optional[Notification]:
        """Tell a customer a product they saved got cheaper. An offer: they can switch it off."""
        drop_percent = ((old_price - new_price) / old_price) * 100

        return await NotificationService.notify(
            db,
            user_id,
            template="price.drop",
            type=NotificationTypeEnum.PRICE_DROP,
            category="offers",
            params={
                "product": product_name,
                "price": i18n.money(new_price),
                "percent": f"{drop_percent:.0f}",
            },
            reference_type="product",
            reference_id=UUID(product_id) if product_id else None,
            data={
                "product_id": product_id,
                "old_price": old_price,
                "new_price": new_price,
                "drop_percent": drop_percent,
            },
            images=[image_url] if image_url else None,
        )
