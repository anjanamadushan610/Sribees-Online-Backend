"""
The Super Admin "App Users" page: who has the app open, on which version, and
each install's daily use since it was downloaded -- fed by the app's heartbeat.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

HEARTBEAT = "/api/v1/app/usage/heartbeat"


def _device() -> str:
    return f"guest_{uuid.uuid4().hex[:20]}"


def _beat(device_id, event="beat", seconds=0, version="1.4.0", **over) -> dict:
    return {"device_id": device_id, "event": event, "foreground_seconds": seconds,
            "platform": "android", "app_version": version, "build_number": "14", **over}


async def _install(api, boss, device_id) -> dict:
    r = await api.get(f"/api/v1/admin/app-usage/installs/{device_id}", headers=boss.headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def test_a_signed_in_customer_is_online_and_their_day_adds_up(api, make_admin, make_customer):
    boss = await make_admin("super_admin", api=api)
    customer = await make_customer(with_address=False)
    device = _device()
    installed = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

    r = await api.post(HEARTBEAT, json=_beat(device, "open", installed_at=installed), headers=customer.headers)
    assert r.status_code == 202, r.text
    await api.post(HEARTBEAT, json=_beat(device, "beat", 120), headers=customer.headers)
    await api.post(HEARTBEAT, json=_beat(device, "beat", 95), headers=customer.headers)

    r = await api.get("/api/v1/admin/app-usage", headers=boss.headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    mine = next(o for o in data["online"] if o["device_id"] == device)
    assert mine["customer_phone"] == customer.phone and mine["is_guest"] is False
    assert mine["app_version"] == "1.4.0" and mine["today_seconds"] == 215
    assert any(v["version"] == "1.4.0" for v in data["versions"])

    history = await _install(api, boss, device)
    assert history["install"]["installed_at_is_estimate"] is False
    # Today plus the three days since the download, newest first.
    assert len(history["days"]) == 4
    assert history["days"][0]["seconds"] == 215 and history["days"][0]["opens"] == 1
    assert history["days"][-1]["measured"] is False, "days before the first heartbeat were never measured"


async def test_going_to_the_background_takes_them_offline_at_once(api, make_admin):
    boss = await make_admin("super_admin", api=api)
    device = _device()
    await api.post(HEARTBEAT, json=_beat(device, "open"))
    await api.post(HEARTBEAT, json=_beat(device, "background", 40))

    r = await api.get("/api/v1/admin/app-usage", headers=boss.headers)
    assert device not in [o["device_id"] for o in r.json()["data"]["online"]]
    history = await _install(api, boss, device)
    assert history["install"]["is_guest"] is True and history["install"]["is_online"] is False
    assert history["days"][0]["seconds"] == 40


async def test_an_update_shows_the_new_version_and_one_report_cannot_add_a_day(api, make_admin):
    boss = await make_admin("super_admin", api=api)
    device = _device()
    await api.post(HEARTBEAT, json=_beat(device, "open", version="1.3.0"))
    await api.post(HEARTBEAT, json=_beat(device, "beat", 86400, version="1.4.0"))

    history = await _install(api, boss, device)
    assert history["install"]["app_version"] == "1.4.0"
    assert history["days"][0]["seconds"] == 3600, "one report is capped at an hour"

    r = await api.get("/api/v1/admin/app-usage/installs", params={"search": device}, headers=boss.headers)
    assert r.status_code == 200 and r.json()["data"]["total"] == 1


CHROME_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _web_beat(device_id, event="beat", seconds=0) -> dict:
    return {"device_id": device_id, "event": event, "foreground_seconds": seconds, "platform": "web"}


async def test_the_web_store_shows_under_web_and_not_under_app(api, make_admin, make_customer):
    boss = await make_admin("super_admin", api=api)
    customer = await make_customer(with_address=False)
    web = f"web_{uuid.uuid4()}"
    phone = _device()
    ua = {"User-Agent": CHROME_WINDOWS}

    r = await api.post(HEARTBEAT, json=_web_beat(web, "open"), headers={**customer.headers, **ua})
    assert r.status_code == 202, r.text
    await api.post(HEARTBEAT, json=_web_beat(web, "beat", 120), headers={**customer.headers, **ua})
    await api.post(HEARTBEAT, json=_beat(phone, "open"))

    r = await api.get("/api/v1/admin/app-usage", params={"channel": "web"}, headers=boss.headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    online = {o["device_id"]: o for o in data["online"]}
    assert web in online and phone not in online
    mine = online[web]
    assert mine["platform"] == "web" and mine["browser"] == "Chrome on Windows"
    assert mine["customer_phone"] == customer.phone and mine["today_seconds"] == 120
    assert mine["is_latest_version"] is None and mine["installed_at_is_estimate"] is True
    assert data["versions"] == [], "a browser has no app version"
    assert data["online_web_count"] >= 1 and data["online_app_count"] >= 1

    r = await api.get("/api/v1/admin/app-usage", params={"channel": "app"}, headers=boss.headers)
    ids = [o["device_id"] for o in r.json()["data"]["online"]]
    assert phone in ids and web not in ids

    r = await api.get(
        "/api/v1/admin/app-usage/installs", params={"channel": "web", "search": "Chrome"}, headers=boss.headers
    )
    assert web in [i["device_id"] for i in r.json()["data"]["installs"]]
    r = await api.get(
        "/api/v1/admin/app-usage/installs", params={"channel": "app", "search": web}, headers=boss.headers
    )
    assert r.json()["data"]["total"] == 0

    # Hiding the tab takes the browser offline at once.
    await api.post(HEARTBEAT, json=_web_beat(web, "background", 30), headers={**customer.headers, **ua})
    history = await _install(api, boss, web)
    assert history["install"]["is_online"] is False and history["days"][0]["seconds"] == 150


async def test_the_channel_filter_takes_only_app_or_web(api, make_admin):
    boss = await make_admin("super_admin", api=api)
    r = await api.get("/api/v1/admin/app-usage", params={"channel": "tv"}, headers=boss.headers)
    assert r.status_code == 422


def test_browser_labels():
    from app.services.app_usage_service import browser_label

    assert browser_label(CHROME_WINDOWS) == "Chrome on Windows"
    assert browser_label(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ) == "Safari on iPhone"
    assert browser_label(
        "Mozilla/5.0 (Linux; Android 14; SM-A145F) AppleWebKit/537.36 (KHTML, like Gecko) "
        "SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36"
    ) == "Samsung Internet on Android"
    assert browser_label(CHROME_WINDOWS + " Edg/131.0.0.0") == "Edge on Windows"
    assert browser_label(None) is None and browser_label("curl/8.4") is None


async def test_the_heartbeat_refuses_junk(api):
    r = await api.post(HEARTBEAT, json=_beat("x' OR 1=1 --", "open"))
    assert r.status_code == 422
    r = await api.post(HEARTBEAT, json={**_beat(_device()), "user_id": str(uuid.uuid4())})
    assert r.status_code == 422, "the customer comes from the token, never the body"


async def test_only_a_super_admin_sees_the_page(api, make_admin):
    for role in ("branch_manager", "marketing_manager", "customer_support"):
        admin = await make_admin(role, api=api)
        r = await api.get("/api/v1/admin/app-usage", headers=admin.headers)
        assert r.status_code == 403, role
    boss = await make_admin("super_admin", api=api)
    r = await api.get("/api/v1/admin/app-usage/installs/guest_does_not_exist", headers=boss.headers)
    assert r.status_code == 404
