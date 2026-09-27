"""
Sending one-time codes: every text costs money and the sign-in endpoint needs
no login, so what matters is who can make the server send, how often, and
that the provider's credentials never leak.

These pin the sending limits (per number, per address, per day for the whole
server), what the app is told (so it never has to assume a length or a
countdown), and the Notify.lk client's request shape.
"""
from __future__ import annotations

from urllib.parse import parse_qs

import fakeredis.aioredis
import httpx
import pytest

from app.config.settings import settings
from app.core.exceptions import AppException
from app.services import sms_gateway
from app.services.otp_service import OTPService
from app.services.sms_gateway import NotifyLkGateway, SmsSendError, mask_phone

pytestmark = pytest.mark.security

PHONE = "+94771234567"
KEY = f"otp:register:{PHONE}"


class FakeGateway:
    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    async def send(self, phone: str, text: str) -> None:
        if self.fail:
            raise SmsSendError("provider said no")
        self.sent.append((phone, text))

    async def balance(self):
        return 1234.0


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr("app.services.otp_service.get_sms_gateway", lambda: fake)
    monkeypatch.setattr(settings, "otp_test_phones", "")
    monkeypatch.setattr(settings, "otp_resend_cooldown_seconds", 60)
    monkeypatch.setattr(settings, "otp_max_sends_per_phone_per_hour", 5)
    monkeypatch.setattr(settings, "otp_max_sends_per_phone_per_day", 10)
    monkeypatch.setattr(settings, "otp_max_sends_per_ip_per_hour", 50)
    monkeypatch.setattr(settings, "sms_daily_limit", 300)
    return fake


async def _refused(coro) -> AppException:
    with pytest.raises(AppException) as exc:
        await coro
    return exc.value


async def test_the_code_is_texted_and_the_app_is_told_how_to_draw_it(redis, gateway):
    challenge = await OTPService.issue(redis, KEY, "0771234567", client_ip="1.2.3.4")
    code = await redis.get(KEY)
    assert gateway.sent == [(PHONE, gateway.sent[0][1])]
    assert code in gateway.sent[0][1] and "Sribees Online" in gateway.sent[0][1]
    assert gateway.sent[0][1].isascii(), "an English text is one SMS; Unicode costs two or three"
    assert challenge.code_length == len(code) == 6
    assert challenge.resend_in_seconds == 60
    assert challenge.expires_in_seconds == 180
    assert challenge.destination == "+94 77 *** **67"
    assert "4567" not in challenge.destination


async def test_a_resend_waits_for_the_cooldown(redis, gateway):
    await OTPService.issue(redis, KEY, PHONE)
    err = await _refused(OTPService.issue(redis, KEY, PHONE))
    assert err.status_code == 429
    assert err.detail["error"]["code"] == "OTP_RESEND_TOO_SOON"
    assert 0 < err.detail["error"]["retry_after"] <= 60
    assert len(gateway.sent) == 1


async def test_one_number_cannot_be_text_bombed(redis, gateway, monkeypatch):
    monkeypatch.setattr(settings, "otp_resend_cooldown_seconds", 0)
    for _ in range(5):
        await OTPService.issue(redis, KEY, PHONE)
    err = await _refused(OTPService.issue(redis, KEY, PHONE))
    assert err.status_code == 429 and err.detail["error"]["code"] == "OTP_TOO_MANY_REQUESTS"
    assert len(gateway.sent) == 5


async def test_one_address_cannot_spray_many_numbers(redis, gateway, monkeypatch):
    monkeypatch.setattr(settings, "otp_max_sends_per_ip_per_hour", 3)
    for n in range(3):
        await OTPService.issue(redis, f"k{n}", f"+9477000000{n}", client_ip="9.9.9.9")
    err = await _refused(OTPService.issue(redis, "k9", "+94770000009", client_ip="9.9.9.9"))
    assert err.status_code == 429
    # Another address is unaffected.
    await OTPService.issue(redis, "k8", "+94770000008", client_ip="8.8.8.8")
    assert len(gateway.sent) == 4


async def test_the_whole_server_stops_at_the_daily_limit(redis, gateway, monkeypatch):
    monkeypatch.setattr(settings, "sms_daily_limit", 2)
    await OTPService.issue(redis, "a", "+94770000001")
    await OTPService.issue(redis, "b", "+94770000002")
    err = await _refused(OTPService.issue(redis, "c", "+94770000003"))
    assert err.status_code == 503 and err.detail["error"]["code"] == "OTP_SMS_UNAVAILABLE"
    assert len(gateway.sent) == 2
    assert await OTPService.sent_today(redis) == 2, "a refused send is not counted"
    # The refused number is not left in a cooldown it never got a text for.
    assert await redis.get("otp:send:wait:+94770000003") is None


async def test_a_failed_send_leaves_nothing_behind(redis, gateway, monkeypatch):
    gateway.fail = True
    err = await _refused(OTPService.issue(redis, KEY, PHONE))
    assert err.status_code == 503 and err.detail["error"]["code"] == "OTP_SMS_UNAVAILABLE"
    assert await redis.get(KEY) is None, "a code nobody received must not stay guessable"
    assert await OTPService.sent_today(redis) == 0
    gateway.fail = False
    await OTPService.issue(redis, KEY, PHONE)  # no cooldown: they may retry at once
    assert len(gateway.sent) == 1


async def test_listed_test_numbers_are_never_texted_or_limited(redis, gateway, monkeypatch):
    monkeypatch.setattr(settings, "otp_test_phones", "0700000000")
    for _ in range(8):
        await OTPService.issue(redis, "t", "+94700000000")
    assert gateway.sent == []
    assert await OTPService.sent_today(redis) == 0


async def test_refusals_are_worded_in_the_apps_language(redis, gateway):
    await OTPService.issue(redis, KEY, PHONE, lang="si")
    err = await _refused(OTPService.issue(redis, KEY, PHONE, lang="si"))
    message = err.detail["error"]["message"]
    assert "තත්පර" in message and "seconds" not in message


# ---------------------------------------------------------------------------
# Over HTTP: the contract the app reads
# ---------------------------------------------------------------------------


async def test_the_request_endpoint_returns_what_the_code_screen_needs(client, gateway):
    r = await client.post("/api/v1/auth/phone/request-otp", json={"phone": "0771234567"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["codeLength"] == 6
    assert body["resendInSeconds"] == 60
    assert body["expiresInSeconds"] == 180
    assert body["destination"] == "+94 77 *** **67"
    assert body["channel"] == "sms"

    r = await client.post(
        "/api/v1/auth/phone/request-otp",
        json={"phone": "0771234567"},
        headers={"Accept-Language": "ta"},
    )
    assert r.status_code == 429, r.text
    error = r.json()["error"]
    assert error["code"] == "OTP_RESEND_TOO_SOON"
    assert error["retry_after"] > 0
    assert "வினாடிகள்" in error["message"]


async def test_a_wrong_code_gets_a_coded_answer(client, gateway):
    r = await client.post(
        "/api/v1/auth/phone/verify-otp",
        json={"phone": "0771234567", "code": "000000"},
        headers={"Accept-Language": "si"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "OTP_INVALID"


# ---------------------------------------------------------------------------
# The Notify.lk client
# ---------------------------------------------------------------------------


def _mock_http(monkeypatch, handler):
    real = httpx.AsyncClient
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        sms_gateway.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(recording), **kw)
    )
    return seen


def _gateway() -> NotifyLkGateway:
    return NotifyLkGateway("uid-1", "secret-key-1", "SriBees", "https://app.notify.lk/api/v1")


async def test_notifylk_sends_credentials_in_the_body_never_the_url(monkeypatch):
    seen = _mock_http(monkeypatch, lambda req: httpx.Response(200, json={"status": "success", "data": "Sent"}))
    await _gateway().send("+94771234567", "123456 is your code")
    (req,) = seen
    assert req.method == "POST"
    assert str(req.url) == "https://app.notify.lk/api/v1/send"
    assert "secret-key-1" not in str(req.url)
    form = parse_qs(req.content.decode())
    assert form["to"] == ["94771234567"]
    assert form["sender_id"] == ["SriBees"]
    assert form["api_key"] == ["secret-key-1"]
    assert "type" not in form


async def test_notifylk_refusals_raise_without_the_key(monkeypatch):
    _mock_http(
        monkeypatch,
        lambda req: httpx.Response(200, json={"status": "error", "errors": "Insufficient balance"}),
    )
    with pytest.raises(SmsSendError) as exc:
        await _gateway().send("+94771234567", "x")
    assert "Insufficient balance" in str(exc.value)
    assert "secret-key-1" not in str(exc.value)


async def test_notifylk_network_failure_is_a_send_error(monkeypatch):
    def boom(req):
        raise httpx.ConnectTimeout("slow", request=req)

    _mock_http(monkeypatch, boom)
    with pytest.raises(SmsSendError):
        await _gateway().send("+94771234567", "x")


async def test_notifylk_balance(monkeypatch):
    _mock_http(
        monkeypatch,
        lambda req: httpx.Response(200, json={"status": "success", "data": {"active": True, "acc_balance": 42.5}}),
    )
    assert await _gateway().balance() == 42.5


async def test_a_chosen_provider_without_credentials_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "sms_provider", "notifylk")
    monkeypatch.setattr(settings, "notifylk_user_id", "")
    monkeypatch.setattr(settings, "notifylk_api_key", "")
    gw = sms_gateway.get_sms_gateway()
    with pytest.raises(SmsSendError) as exc:
        await gw.send("+94771234567", "x")
    assert "NOTIFYLK_API_KEY" in str(exc.value)


def test_the_default_provider_spends_nothing(monkeypatch):
    monkeypatch.setattr(settings, "sms_provider", "console")
    assert sms_gateway.get_sms_gateway().name == "console"


def test_masking():
    assert mask_phone("0771234567") == "+94 77 *** **67"
    assert mask_phone("rubbish") == "***"
