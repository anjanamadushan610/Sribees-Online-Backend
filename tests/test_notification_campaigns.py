"""
Marketing push campaigns: what a manager may send, to whom, and what the
phone receives.

The branch rule is the one that matters: a Marketing Manager's campaign goes
to the customers their branch has served, whatever branch_id the request
carries. Without it any branch could push offers to the whole country.
"""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api.v1 import admin_campaigns
from app.api.v1.admin_campaigns import CampaignCreate
from app.models.notification import NotificationCampaign
from app.services import campaign_service


def _body(**overrides):
    base = {
        "category": "offers",
        "title": {"en": "Weekend sale", "si": "සති අන්ත අලෙවිය"},
        "message": {"en": "20% off all fruit"},
    }
    base.update(overrides)
    return CampaignCreate(**base)


# ---------------------------------------------------------------------------
# What may be sent
# ---------------------------------------------------------------------------

def test_english_is_required_the_others_fall_back():
    body = _body()
    assert body.title.si == "සති අන්ත අලෙවිය"
    assert body.title.ta is None
    with pytest.raises(ValidationError):
        _body(title={"en": "   ", "si": "x"})


def test_titles_and_messages_are_kept_to_what_a_phone_shows():
    with pytest.raises(ValidationError):
        _body(title={"en": "x" * 66})
    with pytest.raises(ValidationError):
        _body(message={"en": "ok", "ta": "y" * 241})


def test_a_product_link_needs_a_product():
    with pytest.raises(ValidationError):
        _body(link_type="product", link_value="not-a-uuid")
    pid = str(uuid.uuid4())
    assert _body(link_type="product", link_value=pid).link_value == pid
    # Any other link drops a stray value.
    assert _body(link_type="offers", link_value="junk").link_value is None


def test_only_the_three_categories():
    with pytest.raises(ValidationError):
        _body(category="transactional")


# ---------------------------------------------------------------------------
# To whom
# ---------------------------------------------------------------------------

def _scope(super_admin=False, branch=None):
    return SimpleNamespace(is_super_admin=super_admin, is_unscoped=super_admin, branch_id=branch)


def test_a_branch_manager_always_sends_to_their_own_branch():
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    assert admin_campaigns._audience_branch(_scope(branch=mine), theirs) == mine
    assert admin_campaigns._audience_branch(_scope(branch=mine), None) == mine


def test_a_super_admin_chooses_including_everyone():
    branch = uuid.uuid4()
    assert admin_campaigns._audience_branch(_scope(super_admin=True), branch) == branch
    assert admin_campaigns._audience_branch(_scope(super_admin=True), None) is None


def test_a_scoped_admin_without_a_branch_cannot_send():
    with pytest.raises(HTTPException) as exc:
        admin_campaigns._audience_branch(_scope(branch=None), None)
    assert exc.value.status_code == 403


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_audience_is_live_opted_in_customers_of_the_branch():
    branch = uuid.uuid4()
    sql = _sql(campaign_service.audience_filter("promotions", branch))
    assert "users.is_active IS true" in sql
    assert "users.is_blocked IS false" in sql
    assert "users.deleted_at IS NULL" in sql
    assert "users.notify_promotions IS true" in sql
    assert "orders.branch_id" in sql and branch.hex in sql.replace("-", "")


def test_everyone_means_no_branch_filter():
    sql = _sql(campaign_service.audience_filter("general", None))
    assert "orders" not in sql
    assert "users.notify_general IS true" in sql


# ---------------------------------------------------------------------------
# What the phone receives
# ---------------------------------------------------------------------------

def _campaign(**overrides):
    base = {
        "campaign_id": uuid.uuid4(),
        "category": "offers",
        "title": {"en": "Sale"},
        "message": {"en": "20% off"},
        "image_url": "https://cdn.example/c.jpg",
        "link_type": None,
        "link_value": None,
        "branch_id": None,
        "status": "sent",
        "scheduled_at": datetime(2026, 9, 25, tzinfo=timezone.utc),
        "started_at": None,
        "finished_at": None,
        "recipients": 200,
        "devices": 250,
        "delivered": 240,
        "failed": 10,
        "opened": 50,
        "error": None,
        "created_by_name": "Nimal",
        "created_at": datetime(2026, 9, 25, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return NotificationCampaign(**base)


def test_a_product_campaign_opens_the_product():
    pid = str(uuid.uuid4())
    data = campaign_service._payload(_campaign(link_type="product", link_value=pid))
    assert data["product_id"] == pid and data["screen"] == "product"
    assert data["type"] == "promotion" and data["category"] == "offers"
    assert data["image_url"] == "https://cdn.example/c.jpg"


def test_offers_link_and_general_type():
    assert campaign_service._payload(_campaign(link_type="offers"))["screen"] == "offers"
    general = campaign_service._payload(_campaign(category="general"))
    assert general["type"] == "system" and "screen" not in general


def test_open_rate_is_of_customers_reached():
    data = campaign_service.serialize(_campaign(), "Colombo 07")
    assert data["open_rate"] == 0.25
    assert data["branch_name"] == "Colombo 07"
    assert campaign_service.serialize(_campaign(recipients=0))["open_rate"] is None


def test_an_open_is_counted_once_per_customer():
    """count_open is only reached on an unread → read change."""
    import asyncio

    class Session:
        def __init__(self, row):
            self.row = row
            self.executed = []
            self.commits = 0

        async def execute(self, statement):
            self.executed.append(statement)
            return SimpleNamespace(scalar_one_or_none=lambda: self.row)

        async def commit(self):
            self.commits += 1

    cid = uuid.uuid4()
    row = SimpleNamespace(is_read=False, read_at=None, data={"campaign_id": str(cid)})
    db = Session(row)
    assert asyncio.run(campaign_service.mark_opened(db, uuid.uuid4(), cid)) is True
    assert row.is_read and len(db.executed) == 2  # the lookup, then opened + 1

    db = Session(row)  # already read
    assert asyncio.run(campaign_service.mark_opened(db, uuid.uuid4(), cid)) is False
    assert len(db.executed) == 1 and db.commits == 0


def test_count_open_ignores_garbage_ids():
    import asyncio

    class Session:
        async def execute(self, statement):  # pragma: no cover - must not be reached
            raise AssertionError("should not query")

    asyncio.run(campaign_service.count_open(Session(), "not-a-uuid"))
