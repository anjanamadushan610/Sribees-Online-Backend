"""
Who gets a notification, in which language, and what the push looks like.

* A customer who switched a marketing category off gets nothing for it; order
  and payment updates ignore the switches.
* The in-app row is in the customer's language; each device's push is in that
  device's language, titled with the app's name.
* Only tokens FCM says are dead are deactivated. A missing Firebase config
  once reported every token as failed, and the old code switched them all off.

No network and no database: FCM and the session are stand-ins.
"""
import asyncio
import io
import uuid
from types import SimpleNamespace

import pytest

from app.models.notification import NotificationCampaign, PushToken
from app.models.user import User
from app.services import notification_i18n as i18n
from app.services import notification_service as ns
from app.services.notification_service import NotificationService


class FakeSession:
    def __init__(self, user=None):
        self.user = user
        self.added = []
        self.executed = []
        self.commits = 0

    async def get(self, model, key, **kwargs):
        return self.user

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        if getattr(obj, "notification_id", None) is None:
            obj.notification_id = uuid.uuid4()

    async def execute(self, statement):
        self.executed.append(statement)
        return SimpleNamespace(rowcount=1)


def _user(**overrides):
    base = {
        "user_id": uuid.uuid4(),
        "preferred_language": "si",
        "notify_offers": True,
        "notify_promotions": True,
        "notify_general": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def spawned(monkeypatch):
    """Capture background pushes instead of running them."""
    jobs = []

    def fake_spawn(coro):
        jobs.append(coro)

    monkeypatch.setattr(ns, "_spawn", fake_spawn)
    yield jobs
    for coro in jobs:
        coro.close()


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

def test_transactional_ignores_the_switches():
    user = _user(notify_offers=False, notify_promotions=False, notify_general=False)
    assert ns.wants(user, "transactional")
    assert not ns.wants(user, "offers")
    assert not ns.wants(user, "promotions")
    assert not ns.wants(user, "general")


def test_everything_is_on_until_switched_off():
    assert ns.wants(_user(), "offers")
    # A row from before migration 071 has no attribute at all.
    assert ns.wants(SimpleNamespace(), "promotions")


def test_an_opted_out_customer_gets_no_row_and_no_push(spawned):
    db = FakeSession(_user(notify_offers=False))
    result = asyncio.run(
        NotificationService.notify(
            db,
            db.user.user_id,
            template="promo.gift",
            type=ns.NotificationTypeEnum.PROMOTION,
            category="offers",
            params={"code": "X", "days": 3},
            per_lang_params=lambda lang: {"off": i18n.coupon_value("fixed", 100, lang)},
        )
    )
    assert result is None
    assert db.added == []
    assert spawned == []


def test_order_updates_reach_a_customer_who_switched_everything_off(spawned):
    user = _user(notify_offers=False, notify_promotions=False, notify_general=False)
    db = FakeSession(user)
    row = asyncio.run(
        NotificationService.notify(
            db,
            user.user_id,
            template="order.status.delivered",
            type=ns.NotificationTypeEnum.ORDER_STATUS,
            params={"order_number": "SB-7"},
        )
    )
    assert row is not None
    assert len(spawned) == 1


# ---------------------------------------------------------------------------
# The in-app row
# ---------------------------------------------------------------------------

def test_the_row_is_in_the_customers_language_with_the_order_photos(spawned):
    db = FakeSession(_user(preferred_language="ta"))
    photos = [f"https://cdn.example/p{i}.jpg" for i in range(6)]
    row = asyncio.run(
        NotificationService.notify(
            db,
            db.user.user_id,
            template="order.status.shipped",
            type=ns.NotificationTypeEnum.ORDER_STATUS,
            params={"order_number": "SB-3"},
            reference_type="order",
            reference_id=uuid.uuid4(),
            images=photos,
        )
    )
    assert row.title == i18n.render("order.status.shipped", "ta", {"order_number": "SB-3"})[0]
    assert row.message == i18n.render("order.status.shipped", "ta", {"order_number": "SB-3"})[1]
    assert row.type == "order_status"
    assert row.data["images"] == photos[:4]


def test_a_literal_notification_still_works(spawned):
    db = FakeSession(_user(preferred_language=None))
    row = asyncio.run(
        NotificationService.create(
            db, db.user.user_id, ns.NotificationTypeEnum.SYSTEM, "Hello", "World"
        )
    )
    assert (row.title, row.message) == ("Hello", "World")


# ---------------------------------------------------------------------------
# The push
# ---------------------------------------------------------------------------

@pytest.fixture
def fcm(monkeypatch):
    calls = []

    async def fake_send(tokens, title, body, data=None, image_url=None):
        calls.append({"tokens": list(tokens), "title": title, "body": body, "data": data, "image": image_url})
        return {
            "success_count": len(tokens) - (1 if "dead" in tokens else 0),
            "failure_count": 1 if "dead" in tokens else 0,
            "failed_tokens": [t for t in tokens if t in ("dead", "flaky")],
            "invalid_tokens": [t for t in tokens if t == "dead"],
        }

    monkeypatch.setattr(ns.FCMService, "send_to_tokens", staticmethod(fake_send))
    return calls


def _tokens(monkeypatch, rows):
    async def fake_tokens(db, user_id):
        return [SimpleNamespace(token=t, language=lang) for t, lang in rows]

    monkeypatch.setattr(NotificationService, "get_user_tokens", staticmethod(fake_tokens))


def test_each_device_reads_its_own_language_under_the_app_name(monkeypatch, fcm):
    _tokens(monkeypatch, [("phone", "si"), ("tablet", "en"), ("old", None)])
    db = FakeSession(_user(preferred_language="ta"))
    texts = i18n.render_all("order.status.delivered", {"order_number": "SB-5"})

    ok = asyncio.run(
        NotificationService.push_localized(
            db, db.user.user_id, texts, standalone=True, data={"order_id": "x", "n": None}
        )
    )

    assert ok
    by_token = {tuple(c["tokens"]): c for c in fcm}
    assert {c["title"] for c in fcm} == {i18n.BRAND_TITLE}
    assert by_token[("phone",)]["body"] == texts["si"][1]
    assert by_token[("tablet",)]["body"] == texts["en"][1]
    # A token registered before the app sent its language reads the user's.
    assert by_token[("old",)]["body"] == texts["ta"][1]
    assert by_token[("phone",)]["data"] == {"order_id": "x"}


def test_a_headline_leads_a_message_that_needs_one(monkeypatch, fcm):
    _tokens(monkeypatch, [("phone", "en")])
    texts = i18n.literal_all("Weekend sale", "20% off all fruit")
    asyncio.run(
        NotificationService.push_localized(FakeSession(_user()), uuid.uuid4(), texts, standalone=False)
    )
    assert fcm[0]["body"] == "Weekend sale\n20% off all fruit"


def test_only_dead_tokens_are_deactivated(monkeypatch, fcm):
    _tokens(monkeypatch, [("dead", "en"), ("flaky", "en"), ("fine", "en")])
    deactivated = []

    async def fake_deactivate(db, token):
        deactivated.append(token)

    monkeypatch.setattr(NotificationService, "_deactivate_token", staticmethod(fake_deactivate))
    asyncio.run(
        NotificationService.push_localized(
            FakeSession(_user()), uuid.uuid4(), i18n.literal_all("a", "b"), standalone=False
        )
    )
    assert deactivated == ["dead"]


def test_send_push_notification_respects_the_category(monkeypatch, fcm):
    _tokens(monkeypatch, [("phone", "en")])
    db = FakeSession(_user(notify_general=False))
    sent = asyncio.run(
        NotificationService.send_push_notification(db, db.user.user_id, "t", "b", category="general")
    )
    assert sent is False and fcm == []


def test_no_devices_no_push(monkeypatch, fcm):
    _tokens(monkeypatch, [])
    assert not asyncio.run(
        NotificationService.push_localized(
            FakeSession(_user()), uuid.uuid4(), i18n.literal_all("a", "b"), standalone=False
        )
    )


# ---------------------------------------------------------------------------
# FCM
# ---------------------------------------------------------------------------

messaging = pytest.importorskip("firebase_admin.messaging")


def test_multicast_splits_at_500_and_only_flags_unregistered(monkeypatch):
    from app.services.fcm_service import FCMService

    sent = []

    def fake_multicast(message, *args, **kwargs):
        sent.append(message)
        responses = []
        for token in message.tokens:
            if token == "t3":
                responses.append(SimpleNamespace(success=False, exception=messaging.UnregisteredError("gone")))
            elif token == "t4":
                responses.append(SimpleNamespace(success=False, exception=RuntimeError("quota")))
            else:
                responses.append(SimpleNamespace(success=True, exception=None))
        ok = sum(r.success for r in responses)
        return SimpleNamespace(responses=responses, success_count=ok, failure_count=len(responses) - ok)

    monkeypatch.setattr(FCMService, "_initialized", True)
    monkeypatch.setattr(messaging, "send_each_for_multicast", fake_multicast)

    tokens = [f"t{i}" for i in range(1203)]
    result = asyncio.run(
        FCMService.send_to_tokens(tokens, "Sribees Online", "hi", image_url="https://cdn.example/a.jpg")
    )

    assert [len(m.tokens) for m in sent] == [500, 500, 203]
    assert result["success_count"] == 1201
    assert result["failed_tokens"] == ["t3", "t4"]
    assert result["invalid_tokens"] == ["t3"]
    apns = sent[0].apns
    assert apns.payload.aps.mutable_content is True
    assert apns.fcm_options.image == "https://cdn.example/a.jpg"


def test_unconfigured_firebase_marks_nothing_dead(monkeypatch):
    from app.services.fcm_service import FCMService

    monkeypatch.setattr(FCMService, "_initialized", False)
    monkeypatch.setattr(FCMService, "initialize", classmethod(lambda cls: False))
    result = asyncio.run(FCMService.send_to_tokens(["a", "b"], "t", "b"))
    assert result["invalid_tokens"] == []


# ---------------------------------------------------------------------------
# The order picture
# ---------------------------------------------------------------------------

def _jpeg(color) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(out, format="JPEG")
    return out.getvalue()


def _png_with_transparency() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGBA", (300, 300), (0, 0, 0, 0)).save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize("count", [2, 3, 4])
def test_collage_is_a_2_to_1_jpeg(count):
    from PIL import Image

    from app.services.notification_media import compose

    photos = [_jpeg((200, 30 * i, 40)) for i in range(count - 1)] + [_png_with_transparency()]
    blob = compose(photos)
    with Image.open(io.BytesIO(blob)) as image:
        assert image.format == "JPEG"
        assert image.size == (1024, 512)
        # A transparent cut-out lands on white, never black.
        assert image.getpixel((1024 - 30 - 60, 256))[0] > 200


def test_one_photo_is_used_as_is():
    from app.services.notification_media import push_image

    assert asyncio.run(push_image(["https://cdn.example/only.jpg"])) == "https://cdn.example/only.jpg"
    assert asyncio.run(push_image([])) is None


def test_photos_that_will_not_download_fall_back_to_the_first(monkeypatch):
    from app.services import notification_media

    async def no_photo(client, url):
        return None

    monkeypatch.setattr(notification_media, "_fetch", no_photo)
    monkeypatch.setattr(notification_media, "get_redis_client", lambda: None)
    urls = ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]
    assert asyncio.run(notification_media.push_image(urls)) == urls[0]


def test_only_absolute_urls_are_offered_to_fcm(monkeypatch):
    from app.services import notification_media

    monkeypatch.setattr(notification_media, "media_url", lambda p: p)
    assert notification_media._absolute("/uploads/x.jpg") is None
    assert notification_media._absolute("https://cdn.example/x.jpg") == "https://cdn.example/x.jpg"


# ---------------------------------------------------------------------------
# The mapping (a stand-in session never touches it)
# ---------------------------------------------------------------------------

def test_new_columns_are_mapped():
    users = User.__table__.columns
    for name in ("preferred_language", "notify_offers", "notify_promotions", "notify_general"):
        assert name in users, name
    assert "language" in PushToken.__table__.columns
    User(full_name="A", preferred_language="si", notify_offers=False)
    PushToken(user_id=uuid.uuid4(), token="t", language="ta")
    NotificationCampaign(
        category="offers",
        title={"en": "Hi"},
        message={"en": "There"},
        scheduled_at=None,
        link_type="offers",
    )
