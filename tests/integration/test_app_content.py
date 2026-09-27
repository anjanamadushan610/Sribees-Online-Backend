"""
App pages whose words live in the database (app_content, migration 074):
served in the customer's language, and an edit made with plain SQL -- the way
the owner changes them after release -- reaches open apps as a silent push.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.fixture
def pushes(monkeypatch):
    from app.services.fcm_service import FCMService

    sent = []

    async def fake_send(topic, data):
        sent.append((topic, data))
        return True

    monkeypatch.setattr(FCMService, "send_data_to_topic", staticmethod(fake_send))
    return sent


async def test_the_developer_page_is_seeded_in_three_languages(api, migrated):
    for lang, name_word in (("en", "What we build"), ("si", "අපි නිර්මාණය කරන දේ"), ("ta", "நாங்கள் உருவாக்குபவை")):
        r = await api.get("/api/v1/app/content/developer", params={"lang": lang})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["lang"] == lang
        content = data["content"]
        assert content["services_title"] == name_word
        assert content["hero"]["name"] == "Sribees Digitel Solutions"
        assert content["cta"]["url"] == "https://www.sribeesdigitel.com"
        assert content["menu"]["title"] and len(content["services"]) == 6


async def test_an_unknown_language_gets_english_and_a_missing_page_404s(api, migrated):
    r = await api.get("/api/v1/app/content/developer", params={"lang": "fr"})
    assert r.json()["data"]["lang"] == "en"
    r = await api.get("/api/v1/app/content/developer")
    assert r.json()["data"]["lang"] == "en"
    r = await api.get("/api/v1/app/content/no-such-page")
    assert r.status_code == 404
    r = await api.get("/api/v1/app/content/Bad Page!")
    assert r.status_code == 422


async def test_a_hidden_page_is_gone_and_a_hidden_language_falls_back(api, db, migrated):
    await db.execute(text("UPDATE app_content SET is_active = false WHERE page = 'developer' AND lang = 'si'"))
    await db.commit()
    try:
        r = await api.get("/api/v1/app/content/developer", params={"lang": "si"})
        assert r.json()["data"]["lang"] == "en"

        await db.execute(text("UPDATE app_content SET is_active = false WHERE page = 'developer'"))
        await db.commit()
        r = await api.get("/api/v1/app/content/developer", params={"lang": "si"})
        assert r.status_code == 404
    finally:
        await db.execute(text("UPDATE app_content SET is_active = true WHERE page = 'developer'"))
        await db.commit()


async def test_a_query_edit_bumps_the_version_and_sends_one_push(api, db, migrated, app_redis, pushes):
    from app.services import app_content_service, live_sync

    await app_redis.delete("app_content:fingerprint")
    # First look: only a baseline, no announcement.
    assert await app_content_service.check_once(db) is False
    assert await app_content_service.check_once(db) is False
    before = (await live_sync.versions(None, None))["content"]

    # The owner's kind of edit: raw SQL, no ORM, no updated_at in the query.
    original = (await db.execute(text(
        "SELECT content->'hero'->>'tagline' FROM app_content WHERE page='developer' AND lang='en'"
    ))).scalar_one()
    await db.execute(text(
        "UPDATE app_content SET content = jsonb_set(content, '{hero,tagline}', to_jsonb('Built in Sri Lanka'::text)) "
        "WHERE page = 'developer' AND lang = 'en'"
    ))
    await db.commit()
    try:
        assert await app_content_service.check_once(db) is True
        assert pushes == [("sync", {"type": "sync", "groups": "content", "branches": ""})]
        after = (await live_sync.versions(None, None))["content"]
        assert after != before

        # Other workers looking at the same state stay quiet.
        assert await app_content_service.check_once(db) is False
        assert len(pushes) == 1

        r = await api.get("/api/v1/app/content/developer", params={"lang": "en"})
        assert r.json()["data"]["content"]["hero"]["tagline"] == "Built in Sri Lanka"
        r = await api.get("/api/v1/app/sync-state", headers={"X-Device-Id": "content_guest_1"})
        assert r.json()["data"]["content"] == after
    finally:
        await db.execute(
            text("UPDATE app_content SET content = jsonb_set(content, '{hero,tagline}', to_jsonb(CAST(:t AS text))) "
                 "WHERE page = 'developer' AND lang = 'en'"),
            {"t": original},
        )
        await db.commit()


def test_language_codes_are_normalised():
    from app.services.app_content_service import normalize_lang

    assert normalize_lang("si") == "si"
    assert normalize_lang("SI-LK") == "si"
    assert normalize_lang("ta_LK") == "ta"
    assert normalize_lang(None) == "en"
    assert normalize_lang("de") == "en"
