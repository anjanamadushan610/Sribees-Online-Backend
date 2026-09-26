"""
Tests that every push leaves the backend able to reach a backgrounded phone.

A customer with two registered devices is sent a multicast, and that path
used to go out with no Android config at all: normal priority and no channel.
FCM holds a normal-priority push until a dozing phone next wakes, so those
customers saw nothing until they opened the app. Single-device customers were
fine, which is why it looked intermittent.

No network: firebase-admin's send functions are replaced, and the message
objects they would have sent are inspected.
"""
import asyncio

import pytest

messaging = pytest.importorskip("firebase_admin.messaging")

from app.services.fcm_service import ANDROID_CHANNEL_ID, FCMService  # noqa: E402


@pytest.fixture
def sent(monkeypatch):
    """Capture every message instead of sending it."""
    captured = []

    def fake_send(message, *args, **kwargs):
        captured.append(message)
        return "projects/test/messages/1"

    def fake_multicast(message, *args, **kwargs):
        captured.append(message)
        ok = type("R", (), {"success": True})()
        return type(
            "B",
            (),
            {
                "responses": [ok] * len(message.tokens),
                "success_count": len(message.tokens),
                "failure_count": 0,
            },
        )()

    monkeypatch.setattr(FCMService, "_initialized", True)
    monkeypatch.setattr(messaging, "send", fake_send)
    monkeypatch.setattr(messaging, "send_each_for_multicast", fake_multicast)
    return captured


def _assert_reaches_background(message):
    android = message.android
    assert android is not None, "no Android config: normal priority, no channel"
    assert android.priority == "high"
    assert android.notification.channel_id == ANDROID_CHANNEL_ID
    assert android.notification.click_action == "FLUTTER_NOTIFICATION_CLICK"
    assert message.apns is not None


def test_one_device(sent):
    asyncio.run(FCMService.send_to_token("tok", "Order shipped", "On its way"))
    _assert_reaches_background(sent[0])


def test_several_devices(sent):
    result = asyncio.run(
        FCMService.send_to_tokens(["a", "b"], "Order shipped", "On its way")
    )
    assert result["success_count"] == 2
    _assert_reaches_background(sent[0])


def test_topic_broadcast(sent):
    asyncio.run(FCMService.send_to_topic("all", "Sale", "20% off today"))
    _assert_reaches_background(sent[0])


def test_silent_topic_message(sent):
    """The banners refresh: data only, so nothing is drawn, but sent now."""
    asyncio.run(FCMService.send_data_to_topic("banners", {"type": "banners_changed"}))
    message = sent[0]
    assert message.notification is None, "a silent message must draw nothing"
    assert message.topic == "banners"
    assert message.data == {"type": "banners_changed"}
    assert message.android.priority == "high"
    assert message.apns.payload.aps.content_available is True


def test_channel_matches_the_app():
    """The app creates this channel by name; a mismatch silently demotes it."""
    assert ANDROID_CHANNEL_ID == "sribeesonline_notifications"
