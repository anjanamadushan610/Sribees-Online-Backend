"""
SRIBEESonline - Firebase Cloud Messaging Service

Push notification service using Firebase Admin SDK.
Replaces Expo Push Notifications for Flutter mobile app.
"""
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.config.settings import settings

# Firebase Admin SDK (optional import - install with: pip install firebase-admin)
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    logger.warning("firebase-admin not installed. FCM push notifications disabled.")


# The Android channel every push lands in. The mobile app creates it in
# MainActivity.kt and names it in res/values/notifications.xml -- change all
# three together, or background pushes fall into an unnamed low-importance
# channel.
ANDROID_CHANNEL_ID = "sribeesonline_notifications"


def _android_config() -> "messaging.AndroidConfig":
    # High priority is what lets FCM wake a phone in Doze. At normal priority
    # a push to a backgrounded app is held until the device next wakes, which
    # on aggressive OEM battery savers can be hours.
    return messaging.AndroidConfig(
        priority="high",
        notification=messaging.AndroidNotification(
            sound="default",
            click_action="FLUTTER_NOTIFICATION_CLICK",
            channel_id=ANDROID_CHANNEL_ID,
        ),
    )


def _apns_config(image_url: Optional[str] = None) -> "messaging.APNSConfig":
    # mutable-content lets an iOS Notification Service Extension attach the
    # image; without one iOS simply shows the text.
    return messaging.APNSConfig(
        payload=messaging.APNSPayload(
            aps=messaging.Aps(sound="default", badge=1, mutable_content=bool(image_url)),
        ),
        fcm_options=messaging.APNSFCMOptions(image=image_url) if image_url else None,
    )


# Errors that mean "this token will never work again" -- the app was
# uninstalled or the token belongs to another Firebase project. Anything else
# (quota, a network blip, FCM itself down) says nothing about the device, and
# deactivating on it once switched off every token when FCM was unconfigured.
def _is_dead_token_error(exc) -> bool:
    if exc is None or not FIREBASE_AVAILABLE:
        return False
    dead = tuple(
        cls
        for cls in (
            getattr(messaging, "UnregisteredError", None),
            getattr(messaging, "SenderIdMismatchError", None),
        )
        if cls is not None
    )
    return isinstance(exc, dead)


# FCM's multicast limit.
MULTICAST_LIMIT = 500


class FCMService:
    """Firebase Cloud Messaging service for push notifications.

    Every send path uses the same Android/APNs config. The multicast path once
    went out without it, so a customer with two devices got normal-priority
    pushes that never arrived while the app was in the background.
    """

    _initialized = False

    @classmethod
    def initialize(cls) -> bool:
        """
        Initialize Firebase Admin SDK.

        Requires FIREBASE_CREDENTIALS_PATH environment variable pointing to
        the service account JSON file.
        """
        if cls._initialized:
            return True

        if not FIREBASE_AVAILABLE:
            logger.warning("Firebase Admin SDK not available")
            return False

        # The JSON itself, if we were given it. Preferred in a container:
        # no file on the host, no volume to mount, no path to get wrong -- and
        # a mount that silently fails leaves push dead with no error anywhere.
        cred_json = getattr(settings, 'firebase_credentials_json', None)
        cred_source = None

        if cred_json:
            import json as _json

            try:
                cred_source = _json.loads(cred_json)
            except ValueError:
                logger.error(
                    "FIREBASE_CREDENTIALS_JSON is not valid JSON -- push "
                    "notifications are disabled. Check the secret was pasted "
                    "whole, including its newlines."
                )
                return False
        else:
            cred_path = getattr(settings, 'firebase_credentials_path', None)
            if not cred_path:
                logger.warning(
                    "Neither FIREBASE_CREDENTIALS_JSON nor "
                    "FIREBASE_CREDENTIALS_PATH is configured -- push disabled"
                )
                return False

            cred_file = Path(cred_path)
            if not cred_file.exists():
                logger.error(f"Firebase credentials file not found: {cred_path}")
                return False
            cred_source = str(cred_file)

        try:
            cred = credentials.Certificate(cred_source)
            firebase_admin.initialize_app(cred)
            cls._initialized = True
            logger.info("Firebase Admin SDK initialized")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Firebase: {e}")
            return False

    @classmethod
    async def send_to_token(
        cls,
        token: str,
        title: str,
        body: str,
        data: Optional[Dict[str, str]] = None,
        image_url: Optional[str] = None,
    ) -> bool:
        """
        Send push notification to a single device token.

        Args:
            token: FCM device token
            title: Notification title
            body: Notification body text
            data: Optional data payload (must be string key-value pairs)
            image_url: Optional image URL for rich notification

        Returns:
            True if sent successfully, False otherwise
        """
        if not cls._initialized:
            if not cls.initialize():
                return False

        try:
            # Build notification
            notification = messaging.Notification(
                title=title,
                body=body,
                image=image_url,
            )

            message = messaging.Message(
                notification=notification,
                data=data or {},
                android=_android_config(),
                apns=_apns_config(image_url),
                token=token,
            )

            # firebase-admin's send() is synchronous/blocking — run it off the
            # event loop so a slow FCM call never stalls the API request.
            response = await asyncio.to_thread(messaging.send, message)
            logger.info(f"FCM message sent: {response}")
            return True

        except messaging.UnregisteredError:
            logger.warning(f"FCM token unregistered: {token[:20]}...")
            return False
        except Exception as e:
            logger.error(f"FCM send failed: {e}")
            return False

    @classmethod
    async def send_to_tokens(
        cls,
        tokens: List[str],
        title: str,
        body: str,
        data: Optional[Dict[str, str]] = None,
        image_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send push notification to multiple device tokens.

        Args:
            tokens: List of FCM device tokens
            title: Notification title
            body: Notification body text
            data: Optional data payload
            image_url: Optional image URL

        Returns:
            Dict with success_count, failure_count, failed_tokens, and
            invalid_tokens -- the subset that will never work again and
            should be deactivated.
        """
        empty = {"success_count": 0, "failure_count": 0, "failed_tokens": [], "invalid_tokens": []}
        if not tokens:
            return empty

        if not cls._initialized:
            if not cls.initialize():
                return {**empty, "failure_count": len(tokens), "failed_tokens": list(tokens)}

        notification = messaging.Notification(title=title, body=body, image=image_url)
        total = dict(empty, failed_tokens=[], invalid_tokens=[])

        for start in range(0, len(tokens), MULTICAST_LIMIT):
            chunk = tokens[start:start + MULTICAST_LIMIT]
            message = messaging.MulticastMessage(
                notification=notification,
                data=data or {},
                android=_android_config(),
                apns=_apns_config(image_url),
                tokens=chunk,
            )
            try:
                # Off the event loop: firebase-admin is blocking.
                response = await asyncio.to_thread(
                    messaging.send_each_for_multicast, message
                )
            except Exception as e:
                logger.error(f"FCM multicast failed: {e}")
                total["failure_count"] += len(chunk)
                total["failed_tokens"].extend(chunk)
                continue

            total["success_count"] += response.success_count
            total["failure_count"] += response.failure_count
            for idx, send_response in enumerate(response.responses):
                if not send_response.success:
                    total["failed_tokens"].append(chunk[idx])
                    if _is_dead_token_error(getattr(send_response, "exception", None)):
                        total["invalid_tokens"].append(chunk[idx])

        logger.info(
            f"FCM multicast: {total['success_count']} success, "
            f"{total['failure_count']} failures"
        )
        return total

    @classmethod
    async def send_to_topic(
        cls,
        topic: str,
        title: str,
        body: str,
        data: Optional[Dict[str, str]] = None,
    ) -> bool:
        """
        Send push notification to a topic (broadcast).

        Topics: 'all', 'promotions', 'order_updates', etc.
        """
        if not cls._initialized:
            if not cls.initialize():
                return False

        try:
            message = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                data=data or {},
                android=_android_config(),
                apns=_apns_config(),
                topic=topic,
            )

            response = messaging.send(message)
            logger.info(f"FCM topic message sent to '{topic}': {response}")
            return True

        except Exception as e:
            logger.error(f"FCM topic send failed: {e}")
            return False

    @classmethod
    async def subscribe_to_topic(cls, tokens: List[str], topic: str) -> bool:
        """Subscribe tokens to a topic."""
        if not cls._initialized:
            if not cls.initialize():
                return False

        try:
            response = messaging.subscribe_to_topic(tokens, topic)
            logger.info(f"Subscribed {response.success_count} tokens to '{topic}'")
            return True
        except Exception as e:
            logger.error(f"Topic subscription failed: {e}")
            return False

    @classmethod
    async def unsubscribe_from_topic(cls, tokens: List[str], topic: str) -> bool:
        """Unsubscribe tokens from a topic."""
        if not cls._initialized:
            if not cls.initialize():
                return False

        try:
            response = messaging.unsubscribe_from_topic(tokens, topic)
            logger.info(f"Unsubscribed {response.success_count} tokens from '{topic}'")
            return True
        except Exception as e:
            logger.error(f"Topic unsubscription failed: {e}")
            return False


# ============================================================================
# Notification Templates
# ============================================================================

class NotificationTemplates:
    """Pre-defined notification templates for common events."""

    @staticmethod
    def order_confirmed(order_id: str, total: float) -> Dict[str, str]:
        return {
            "title": "Order Confirmed! ✅",
            "body": f"Your order #{order_id[:8]} for LKR {total:,.2f} has been confirmed.",
            "data": {"type": "order", "order_id": order_id, "action": "view_order"},
        }

    @staticmethod
    def order_shipped(order_id: str, tracking_number: Optional[str] = None) -> Dict[str, str]:
        body = f"Your order #{order_id[:8]} is on its way!"
        if tracking_number:
            body += f" Tracking: {tracking_number}"
        return {
            "title": "Order Shipped! 📦",
            "body": body,
            "data": {"type": "order", "order_id": order_id, "action": "track_order"},
        }

    @staticmethod
    def order_delivered(order_id: str) -> Dict[str, str]:
        return {
            "title": "Order Delivered! 🎉",
            "body": f"Your order #{order_id[:8]} has been delivered. Enjoy!",
            "data": {"type": "order", "order_id": order_id, "action": "rate_order"},
        }

    @staticmethod
    def price_drop(product_name: str, old_price: float, new_price: float, product_id: str) -> Dict[str, str]:
        discount = ((old_price - new_price) / old_price) * 100
        return {
            "title": "Price Drop Alert! 💰",
            "body": f"{product_name} is now {discount:.0f}% off! LKR {new_price:,.2f}",
            "data": {"type": "product", "product_id": product_id, "action": "view_product"},
        }

    @staticmethod
    def back_in_stock(product_name: str, product_id: str) -> Dict[str, str]:
        return {
            "title": "Back in Stock! 🔔",
            "body": f"{product_name} is available again. Get it before it's gone!",
            "data": {"type": "product", "product_id": product_id, "action": "view_product"},
        }

    @staticmethod
    def cart_reminder(item_count: int) -> Dict[str, str]:
        return {
            "title": "Forgot Something? 🛒",
            "body": f"You have {item_count} item{'s' if item_count > 1 else ''} waiting in your cart!",
            "data": {"type": "cart", "action": "view_cart"},
        }

    @staticmethod
    def promotional(title: str, body: str, promo_code: Optional[str] = None) -> Dict[str, str]:
        data = {"type": "promotion", "action": "view_promotions"}
        if promo_code:
            data["promo_code"] = promo_code
        return {"title": title, "body": body, "data": data}
