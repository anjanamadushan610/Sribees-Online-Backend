from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.api.v1.free_delivery import GrantBody, create_grants
from app.services import free_delivery_notifications as notices
from app.services import notification_i18n as i18n
from app.services.notification_service import NotificationService


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "si", "ta", None])
@pytest.mark.parametrize("offers", [True, False])
async def test_grant_uses_selected_language_and_offer_preference(monkeypatch, lang, offers):
    db = AsyncMock()
    db.add = Mock()
    db.get.return_value = SimpleNamespace(preferred_language=lang, notify_offers=offers)
    pushes = []

    def spawn(coro):
        pushes.append(coro)
        coro.close()

    monkeypatch.setattr("app.services.notification_service._spawn", spawn)
    count = await notices.notify_grants(
        db, [(uuid4(), uuid4())],
        ends_at=datetime(2027, 1, 1, 20, tzinfo=timezone.utc),
        min_order=Decimal("1000"), branch_name="Matugama",
    )
    assert count == int(offers)
    assert len(pushes) == int(offers)
    if offers:
        row = db.add.call_args.args[0]
        assert "2027-01-02 01:30" in row.message
        assert "Matugama" in row.message and "Rs. 1,000.00" in row.message
        expected = i18n.render_all(
            "free_delivery.granted", {"expires": "2027-01-02 01:30"},
            lambda code: notices.grant_params(code, min_order=Decimal("1000"), branch_name="Matugama"),
        )[i18n.normalize_lang(lang)]
        assert (row.title, row.message) == expected
    else:
        db.add.assert_not_called()


def test_no_minimum_and_unrestricted_branch_copy():
    for lang in i18n.LANGUAGES:
        params = notices.grant_params(lang, min_order=Decimal("0"), branch_name=None)
        assert params["scope"] and params["minimum"] == ""


@pytest.mark.asyncio
async def test_notification_failure_does_not_stop_other_customers(monkeypatch):
    notify = AsyncMock(side_effect=[RuntimeError("db unavailable"), object()])
    monkeypatch.setattr(NotificationService, "notify", notify)
    db = AsyncMock()
    assert await notices.notify_grants(
        db, [(uuid4(), uuid4()), (uuid4(), uuid4())],
        ends_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        min_order=Decimal("0"), branch_name=None,
    ) == 1
    db.rollback.assert_awaited_once()
    assert notify.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_fails", [False, True])
async def test_grants_commit_before_notification_and_deduplicate(monkeypatch, commit_fails):
    user_id = uuid4()
    db = AsyncMock()
    db.add = Mock()
    db.execute.return_value = Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[user_id]))))
    if commit_fails:
        db.commit.side_effect = RuntimeError("commit failed")

    async def notify(*args, **kwargs):
        db.commit.assert_awaited_once()
        assert len(args[1]) == 1
        assert "note" not in kwargs
        return 1

    notification = AsyncMock(side_effect=notify)
    monkeypatch.setattr("app.api.v1.free_delivery.notify_grants", notification)
    body = GrantBody(user_ids=[user_id, user_id], ends_at=datetime(2099, 1, 1, tzinfo=timezone.utc), note="Internal only")
    if commit_fails:
        with pytest.raises(RuntimeError, match="commit failed"):
            await create_grants(body, db, SimpleNamespace(admin_id=uuid4()))
        notification.assert_not_awaited()
    else:
        result = await create_grants(body, db, SimpleNamespace(admin_id=uuid4()))
        assert result["data"] == {"granted": 1, "notified": 1}
        assert db.add.call_count == 1
