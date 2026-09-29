"""
Sending one-time codes: every text costs money and the sign-in endpoint needs
no login, so what matters is who can make the server send, how often, and
that the provider's credentials never leak.

These pin the sending limits (per number, per address, per day for the whole
server), what the app is told (so it never has to assume a length or a
countdown), and the Text.lk client's request shape.
"""
from __future__ import annotations

import json

import fakeredis.aioredis
import httpx
import pytest

from app.config.settings import settings
from app.core.exceptions import AppException
from app.services import sms_gateway
from app.services.otp_service import OTPService
from app.services.sms_gateway import SmsSendError, TextLkGateway, mask_phone

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

    async def _limit(_db):
        return 100

    # The endpoints read the Super Admin's daily total from the database;
    # these tests run without one.
    monkeypatch.setattr("app.api.v1.auth.sms_daily_limit", _limit)
    return fake


LIMIT = 100


def test_the_owners_rules():
    """Pinned: the owner set these on 2026-09-27, the same for every server."""
    assert OTPService.RESEND_COOLDOWN_SECONDS == 5 * 60
    assert OTPService.MAX_SENDS_PER_IP_PER_HOUR == 10
    assert OTPService.MAX_SENDS_PER_PHONE_PER_HOUR == 5
    assert OTPService.MAX_SENDS_PER_PHONE_PER_DAY == 10
    from app.services.app_settings_service import DEFAULT_SMS_DAILY_LIMIT

    assert DEFAULT_SMS_DAILY_LIMIT == 100
    for name in ("sms_daily_limit", "otp_resend_cooldown_seconds", "otp_max_sends_per_ip_per_hour"):
        assert not hasattr(settings, name), f"{name} must not be an environment setting"


async def _refused(coro) -> AppException:
    with pytest.raises(AppException) as exc:
        await coro
    return exc.value


async def test_the_code_is_texted_and_the_app_is_told_how_to_draw_it(redis, gateway):
    challenge = await OTPService.issue(redis, KEY, "0771234567", client_ip="1.2.3.4", daily_limit=LIMIT)
    code = await redis.get(KEY)
    assert gateway.sent == [(PHONE, gateway.sent[0][1])]
    assert code in gateway.sent[0][1] and "Sribees Online" in gateway.sent[0][1]
    assert gateway.sent[0][1].isascii(), "an English text is one SMS; Unicode costs two or three"
    assert challenge.code_length == len(code) == 6
    assert challenge.resend_in_seconds == 300
    assert challenge.expires_in_seconds == 180
    assert challenge.destination == "+94 77 *** **67"
    assert "4567" not in challenge.destination


async def test_a_resend_waits_for_the_cooldown(redis, gateway):
    await OTPService.issue(redis, KEY, PHONE, daily_limit=LIMIT)
    err = await _refused(OTPService.issue(redis, KEY, PHONE, daily_limit=LIMIT))
    assert err.status_code == 429
    assert err.detail["error"]["code"] == "OTP_RESEND_TOO_SOON"
    assert 240 < err.detail["error"]["retry_after"] <= 300, "five minutes"
    assert len(gateway.sent) == 1


async def test_one_number_cannot_be_text_bombed(redis, gateway, monkeypatch):
    monkeypatch.setattr(OTPService, "RESEND_COOLDOWN_SECONDS", 0)
    for _ in range(5):
        await OTPService.issue(redis, KEY, PHONE, daily_limit=LIMIT)
    err = await _refused(OTPService.issue(redis, KEY, PHONE, daily_limit=LIMIT))
    assert err.status_code == 429 and err.detail["error"]["code"] == "OTP_TOO_MANY_REQUESTS"
    assert len(gateway.sent) == 5


async def test_one_address_gets_ten_texts_an_hour(redis, gateway):
    for n in range(10):
        await OTPService.issue(redis, f"k{n}", f"+9477000010{n}", client_ip="9.9.9.9", daily_limit=LIMIT)
    err = await _refused(OTPService.issue(redis, "k9", "+94770000099", client_ip="9.9.9.9", daily_limit=LIMIT))
    assert err.status_code == 429
    # Another address is unaffected.
    await OTPService.issue(redis, "k8", "+94770000088", client_ip="8.8.8.8", daily_limit=LIMIT)
    assert len(gateway.sent) == 11


async def test_the_whole_server_stops_at_the_daily_limit(redis, gateway):
    await OTPService.issue(redis, "a", "+94770000001", daily_limit=2)
    await OTPService.issue(redis, "b", "+94770000002", daily_limit=2)
    err = await _refused(OTPService.issue(redis, "c", "+94770000003", daily_limit=2))
    assert err.status_code == 503 and err.detail["error"]["code"] == "OTP_SMS_UNAVAILABLE"
    assert len(gateway.sent) == 2
    assert await OTPService.sent_today(redis) == 2, "a refused send is not counted"
    # The refused number is not left in a cooldown it never got a text for.
    assert await redis.get("otp:send:wait:+94770000003") is None
    history = await OTPService.sent_by_day(redis, 7)
    assert len(history) == 7 and history[0]["sent"] == 2


async def test_a_daily_limit_of_zero_sends_nothing(redis, gateway):
    err = await _refused(OTPService.issue(redis, KEY, PHONE, daily_limit=0))
    assert err.status_code == 503
    assert gateway.sent == []


async def test_a_failed_send_leaves_nothing_behind(redis, gateway, monkeypatch):
    gateway.fail = True
    err = await _refused(OTPService.issue(redis, KEY, PHONE, daily_limit=LIMIT))
    assert err.status_code == 503 and err.detail["error"]["code"] == "OTP_SMS_UNAVAILABLE"
    assert await redis.get(KEY) is None, "a code nobody received must not stay guessable"
    assert await OTPService.sent_today(redis) == 0
    gateway.fail = False
    await OTPService.issue(redis, KEY, PHONE, daily_limit=LIMIT)  # no cooldown: they may retry at once
    assert len(gateway.sent) == 1


async def test_listed_test_numbers_are_never_texted_or_limited(redis, gateway, monkeypatch):
    monkeypatch.setattr(settings, "otp_test_phones", "0700000000")
    for _ in range(8):
        await OTPService.issue(redis, "t", "+94700000000", daily_limit=LIMIT)
    assert gateway.sent == []
    assert await OTPService.sent_today(redis) == 0


async def test_refusals_are_worded_in_the_apps_language(redis, gateway):
    await OTPService.issue(redis, KEY, PHONE, lang="si", daily_limit=LIMIT)
    err = await _refused(OTPService.issue(redis, KEY, PHONE, lang="si", daily_limit=LIMIT))
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
    assert body["resendInSeconds"] == 300
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
# The Text.lk client
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


def _gateway() -> TextLkGateway:
    return TextLkGateway("secret-token-1", "SriBees", "https://app.text.lk/api/v3")


async def test_textlk_sends_the_token_in_the_header_never_the_url_or_body(monkeypatch):
    seen = _mock_http(
        monkeypatch,
        lambda req: httpx.Response(200, json={"status": "success", "message": "Your message was successfully delivered"}),
    )
    await _gateway().send("+94771234567", "123456 is your code")
    (req,) = seen
    assert req.method == "POST"
    assert str(req.url) == "https://app.text.lk/api/v3/sms/send"
    assert req.headers["Authorization"] == "Bearer secret-token-1"
    assert "secret-token-1" not in str(req.url)
    body = json.loads(req.content)
    assert "secret-token-1" not in req.content.decode()
    assert body == {
        "recipient": "94771234567",
        "sender_id": "SriBees",
        "type": "plain",
        "message": "123456 is your code",
    }


async def test_textlk_sinhala_goes_as_unicode(monkeypatch):
    seen = _mock_http(monkeypatch, lambda req: httpx.Response(200, json={"status": True}))
    await _gateway().send("+94771234567", "123456 ඔබේ කේතය")
    assert json.loads(seen[0].content)["type"] == "unicode"


async def test_textlk_error_with_http_200_is_still_a_refusal(monkeypatch):
    _mock_http(
        monkeypatch,
        lambda req: httpx.Response(200, json={"status": "error", "message": "Insufficient balance"}),
    )
    with pytest.raises(SmsSendError) as exc:
        await _gateway().send("+94771234567", "x")
    assert "Insufficient balance" in str(exc.value)
    assert "secret-token-1" not in str(exc.value)


async def test_textlk_network_failure_is_a_send_error(monkeypatch):
    def boom(req):
        raise httpx.ConnectTimeout("slow", request=req)

    _mock_http(monkeypatch, boom)
    with pytest.raises(SmsSendError):
        await _gateway().send("+94771234567", "x")


async def test_textlk_balance(monkeypatch):
    seen = _mock_http(
        monkeypatch,
        lambda req: httpx.Response(
            200, json={"status": "success", "data": {"remaining_balance": "1,250", "expired_on": "1st Jan 27"}}
        ),
    )
    assert await _gateway().balance() == 1250.0
    assert str(seen[0].url) == "https://app.text.lk/api/v3/balance"
    assert seen[0].method == "GET"


async def test_a_chosen_provider_without_credentials_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "sms_provider", "textlk")
    monkeypatch.setattr(settings, "textlk_api_token", "")
    gw = sms_gateway.get_sms_gateway()
    with pytest.raises(SmsSendError) as exc:
        await gw.send("+94771234567", "x")
    assert "TEXTLK_API_TOKEN" in str(exc.value)


def test_textlk_is_selected_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "sms_provider", "textlk")
    monkeypatch.setattr(settings, "textlk_api_token", "t")
    monkeypatch.setattr(settings, "textlk_sender_id", "SriBees")
    assert isinstance(sms_gateway.get_sms_gateway(), TextLkGateway)


def test_the_default_provider_spends_nothing(monkeypatch):
    monkeypatch.setattr(settings, "sms_provider", "console")
    assert sms_gateway.get_sms_gateway().name == "console"


def test_masking():
    assert mask_phone("0771234567") == "+94 77 *** **67"
    assert mask_phone("rubbish") == "***"
