"""
Tests for the SribeesExpress client's contract-shaped behaviour: error-code
branching, address folding, webhook signature verification, and the
shipment-payload mapping that decides what a delivery actually meant.

All DB-free. The one thing worth stating up front, because three tests below
exist only to protect it: **a `delivered` status is not proof a COD order was
paid.** Contract section 8 flips `cod.collected` separately and says it can
lag the status, so anything about money reads that field, never the status.
"""
import hashlib
import hmac
import json
import time
from decimal import Decimal

import httpx
import pytest

from app.services.courier_service import (
    CourierError,
    CourierQuoteAlreadyUsedError,
    CourierQuoteExpiredError,
    CourierService,
    CourierUnavailableError,
    format_recipient_address,
)

# ---------------------------------------------------------------------------
# Error envelope -> exception type (contract section 4)
# ---------------------------------------------------------------------------


def _response(status_code: int, body) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        request=httpx.Request("POST", "https://example.test/x"),
    )


def test_quote_expired_and_quote_already_used_are_different_exceptions():
    """
    Both are 409. They need opposite handling -- expired can be re-quoted from
    the same cart, already-used means that quote is spent forever -- so the
    client must never branch on the status code alone.
    """
    with pytest.raises(CourierQuoteExpiredError):
        CourierService._raise_for_error(
            _response(409, {"error": {"code": "quote_expired", "message": "gone"}}), "/shipments"
        )
    with pytest.raises(CourierQuoteAlreadyUsedError):
        CourierService._raise_for_error(
            _response(409, {"error": {"code": "quote_already_used", "message": "spent"}}),
            "/shipments",
        )


def test_other_error_codes_surface_as_a_structured_courier_error():
    with pytest.raises(CourierError) as exc_info:
        CourierService._raise_for_error(
            _response(403, {"error": {"code": "quote_not_owned", "message": "nope"}}), "/shipments"
        )
    assert exc_info.value.code == "quote_not_owned"
    assert exc_info.value.status_code == 403


def test_an_unparseable_error_body_is_treated_as_an_outage():
    """
    A 4xx we cannot read means we do not actually know what happened. Calling
    that a real answer would let checkout act on a guess; calling it an outage
    makes the delivery charge "unavailable", which checkout refuses honestly.
    """
    response = httpx.Response(
        400, content=b"<html>gateway</html>", request=httpx.Request("POST", "https://x.test/y")
    )
    with pytest.raises(CourierUnavailableError):
        CourierService._raise_for_error(response, "/rates")


@pytest.mark.asyncio
async def test_call_outside_a_credential_scope_fails_fast_without_a_network_call():
    """
    The host and key come from the enclosing credential scope, so a call made
    outside one has nowhere to go. It must raise before touching the network:
    an empty base URL would otherwise surface as httpx.InvalidURL from deep
    inside the client, which the retry logic does not catch, and that would
    500 every checkout instead of reporting delivery as unavailable.
    """
    with pytest.raises(CourierUnavailableError):
        await CourierService.get_rate(postal_city_id=42, weight_kg=Decimal("1"), payment_method="cod")


# ---------------------------------------------------------------------------
# Address folding (contract section 7 -- 255-char max)
# ---------------------------------------------------------------------------


def test_recipient_address_is_comma_joined_in_reading_order():
    assert format_recipient_address(
        "12 Galle Road", "Apt 4", "Colombo", "Colombo", "00300"
    ) == "12 Galle Road, Apt 4, Colombo, Colombo, 00300"


def test_recipient_address_skips_blanks():
    assert format_recipient_address("12 Galle Road", None, "Colombo", "Colombo", "") == (
        "12 Galle Road, Colombo, Colombo"
    )


def test_recipient_address_is_truncated_to_the_documented_max():
    address = format_recipient_address("x" * 400, None, "Colombo", "Colombo", "00300")
    assert len(address) == 255


# ---------------------------------------------------------------------------
# Webhook signature verification (contract section 10.3)
# ---------------------------------------------------------------------------


_SECRET = "test-webhook-secret"


def _sign(body: bytes, secret: str = _SECRET, timestamp: int | None = None) -> str:
    t = timestamp if timestamp is not None else int(time.time())
    digest = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


class _FakeRequest:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def body(self) -> bytes:
        return self._body


_NO_DB = object()


@pytest.fixture
def stored_secrets(monkeypatch):
    """The per-account secrets kept in the database, as the verifier reads them."""
    from app.services import courier_credentials

    secrets: list[str] = []

    async def _webhook_secrets(db):
        return list(secrets)

    monkeypatch.setattr(courier_credentials, "webhook_secrets", _webhook_secrets)
    return secrets


@pytest.fixture
def webhook_secret(monkeypatch, stored_secrets):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "courier_webhook_secret", _SECRET, raising=False)
    monkeypatch.setattr(settings, "courier_webhook_tolerance_seconds", 300, raising=False)
    return _SECRET


@pytest.mark.asyncio
async def test_a_correctly_signed_body_verifies(webhook_secret):
    from app.api.v1.courier_webhooks import verify_signature

    body = b'{"event_id":"abc","shipment_id":"SXP1","status":"delivered"}'
    assert await verify_signature(_FakeRequest(body), _sign(body), _NO_DB) == body


@pytest.mark.asyncio
async def test_a_push_signed_by_any_of_our_accounts_verifies(monkeypatch, stored_secrets):
    """
    A branch with its own SribeesExpress key has its own endpoint and secret.
    A push does not say which account sent it before it is verified, so each
    stored secret must be able to verify its own account's pushes.
    """
    from app.api.v1.courier_webhooks import verify_signature
    from app.config.settings import settings

    monkeypatch.setattr(settings, "courier_webhook_secret", "", raising=False)
    monkeypatch.setattr(settings, "courier_webhook_tolerance_seconds", 300, raising=False)
    stored_secrets.extend(["account-secret", "galle-branch-secret"])

    body = b'{"shipment_id":"SXP9","status":"picked_up"}'
    for secret in stored_secrets:
        assert await verify_signature(_FakeRequest(body), _sign(body, secret=secret), _NO_DB) == body


@pytest.mark.asyncio
async def test_a_tampered_body_is_rejected(webhook_secret):
    from fastapi import HTTPException

    from app.api.v1.courier_webhooks import verify_signature

    signature = _sign(b'{"status":"failed"}')
    with pytest.raises(HTTPException) as exc_info:
        await verify_signature(_FakeRequest(b'{"status":"delivered"}'), signature, _NO_DB)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_a_signature_from_the_wrong_secret_is_rejected(webhook_secret, stored_secrets):
    from fastapi import HTTPException

    from app.api.v1.courier_webhooks import verify_signature

    stored_secrets.append("another-account-secret")
    body = b'{"status":"delivered"}'
    with pytest.raises(HTTPException):
        await verify_signature(_FakeRequest(body), _sign(body, secret="not-our-secret"), _NO_DB)


@pytest.mark.asyncio
async def test_a_replayed_signature_outside_the_tolerance_is_rejected(webhook_secret):
    """
    A valid signature replayed days later is still valid HMAC -- the signed
    timestamp is the only thing that makes it a replay.
    """
    from fastapi import HTTPException

    from app.api.v1.courier_webhooks import verify_signature

    body = b'{"status":"delivered"}'
    stale = _sign(body, timestamp=int(time.time()) - 3600)
    with pytest.raises(HTTPException) as exc_info:
        await verify_signature(_FakeRequest(body), stale, _NO_DB)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_verification_fails_closed_when_no_secret_is_configured(monkeypatch, stored_secrets):
    """No secret anywhere must never mean 'accept everything'."""
    from fastapi import HTTPException

    from app.api.v1.courier_webhooks import verify_signature
    from app.config.settings import settings

    monkeypatch.setattr(settings, "courier_webhook_secret", "", raising=False)
    body = b'{"status":"delivered"}'
    with pytest.raises(HTTPException) as exc_info:
        await verify_signature(_FakeRequest(body), _sign(body), _NO_DB)
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_a_malformed_signature_header_is_rejected(webhook_secret):
    from fastapi import HTTPException

    from app.api.v1.courier_webhooks import verify_signature

    for header in ("garbage", "t=abc,v1=def", "v1=deadbeef"):
        with pytest.raises(HTTPException):
            await verify_signature(_FakeRequest(b"{}"), header, _NO_DB)


# ---------------------------------------------------------------------------
# Shipment payload -> order mapping
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _FakeOrder:
    order_number = "FC-20260828-TEST01"
    user_id = "00000000-0000-0000-0000-000000000001"
    status = "shipped"
    courier_tracking_status = "in_transit"
    courier_tracking_updated_at = None
    courier_tracking_url = None
    courier_pickup_location_name = None
    courier_handover_required = False
    courier_handover_verified = None
    courier_handover_attempts = None
    courier_cod_collected = False
    courier_cod_collected_amount = None
    courier_cod_collected_at = None
    courier_failure_note = None
    courier_will_retry = None
    courier_remittance_id = None
    delivery_verification_status = "pending"
    delivery_verification_confirmed_at = None


@pytest.fixture
def stub_status_transition(monkeypatch):
    """
    Stand in for OrderService.update_status, which needs a real Order row and
    session (it writes status history and fires notifications). These tests
    are about the courier->order field mapping, not about that machinery.
    """
    from app.services.notification_service import NotificationService
    from app.services.order_service import OrderService

    async def _update_status(db, order, status, changed_by="Admin", notes=None):
        order.status = status.value
        return order

    async def _notify(db, **kwargs):
        return None

    monkeypatch.setattr(OrderService, "update_status", staticmethod(_update_status))
    monkeypatch.setattr(NotificationService, "notify_order_status", staticmethod(_notify))


@pytest.mark.asyncio
async def test_delivered_without_a_cod_block_does_not_mark_cash_collected(stub_status_transition):
    """
    The whole point of persisting `cod` separately. A delivered event that
    says nothing about money must leave courier_cod_collected False, or a COD
    order would look settled on a rider's status change alone.
    """
    from app.services import courier_sync_service

    order = _FakeOrder()
    await courier_sync_service.apply_shipment_payload(
        _FakeDB(),
        order,
        {"status": "delivered", "at": "2026-08-28T15:02:00+00:00"},
        commit=True,
    )
    assert order.status == "delivered"
    assert order.courier_cod_collected is False


@pytest.mark.asyncio
async def test_cod_collected_block_is_persisted_with_its_amount():
    from app.services import courier_sync_service

    order = _FakeOrder()
    order.status = "out_for_delivery"
    await courier_sync_service.apply_shipment_payload(
        _FakeDB(),
        order,
        {
            "status": "out_for_delivery",
            "at": "2026-08-28T15:02:00+00:00",
            "cod": {"collected": True, "amount": 4820.00},
        },
        commit=True,
    )
    assert order.courier_cod_collected is True
    assert order.courier_cod_collected_amount == Decimal("4820.0")
    assert order.courier_cod_collected_at is not None


@pytest.mark.asyncio
async def test_a_verified_handover_updates_our_own_verification_row():
    from app.services import courier_sync_service

    order = _FakeOrder()
    order.status = "out_for_delivery"
    await courier_sync_service.apply_shipment_payload(
        _FakeDB(),
        order,
        {
            "status": "out_for_delivery",
            "at": "2026-08-28T15:02:00+00:00",
            "handover": {"required": True, "code_verified": True, "attempts": 1},
        },
        commit=True,
    )
    assert order.courier_handover_verified is True
    assert order.courier_handover_attempts == 1
    assert order.delivery_verification_status == "confirmed"


@pytest.mark.asyncio
async def test_absent_keys_are_left_alone_rather_than_nulled():
    """
    A webhook event carries only what changed. Treating an absent key as
    "cleared" would let a later status push wipe a recorded COD collection.
    """
    from app.services import courier_sync_service

    order = _FakeOrder()
    order.courier_cod_collected = True
    order.courier_cod_collected_amount = Decimal("4820.00")
    order.courier_failure_note = "earlier note"

    await courier_sync_service.apply_shipment_payload(
        _FakeDB(), order, {"status": "returned", "at": "2026-08-28T16:00:00+00:00"}, commit=True
    )
    assert order.courier_cod_collected is True
    assert order.courier_cod_collected_amount == Decimal("4820.00")
    assert order.courier_failure_note == "earlier note"


@pytest.mark.asyncio
async def test_failure_note_is_recorded_and_the_order_shows_the_failed_attempt(stub_status_transition):
    from app.services import courier_sync_service

    order = _FakeOrder()
    await courier_sync_service.apply_shipment_payload(
        _FakeDB(),
        order,
        {
            "status": "failed",
            "at": "2026-08-28T16:00:00+00:00",
            "failure_note": "Called 3 times, no answer",
        },
        commit=True,
    )
    assert order.courier_failure_note == "Called 3 times, no answer"
    # A failed attempt is shown to the customer as delivery_failed -- it is not
    # a cancellation or a refund, which stay ops decisions.
    assert order.status == "delivery_failed"


@pytest.mark.asyncio
async def test_courier_returned_never_moves_our_order(stub_status_transition):
    """A return has refund consequences; `returned` is an ops signal only."""
    from app.services import courier_sync_service

    order = _FakeOrder()
    order.status = "delivery_failed"
    await courier_sync_service.apply_shipment_payload(
        _FakeDB(), order, {"status": "returned", "at": "2026-08-29T16:00:00+00:00"}, commit=True
    )
    assert order.courier_tracking_status == "returned"
    assert order.status == "delivery_failed"


@pytest.mark.asyncio
async def test_the_riders_pickup_is_our_handed_to_courier(stub_status_transition):
    from app.services import courier_sync_service

    order = _FakeOrder()
    order.status = "ready_for_pickup"
    await courier_sync_service.apply_shipment_payload(
        _FakeDB(), order, {"status": "picked_up", "at": "2026-09-13T10:00:00+00:00"}, commit=True
    )
    assert order.status == "handed_to_courier"


@pytest.mark.asyncio
async def test_a_late_courier_event_never_moves_an_order_backwards(stub_status_transition):
    """
    Webhooks arrive out of order and the sweep replays history. A `picked_up`
    that lands after `out_for_delivery` must not pull the order back.
    """
    from app.services import courier_sync_service

    order = _FakeOrder()
    order.status = "out_for_delivery"
    await courier_sync_service.apply_shipment_payload(
        _FakeDB(), order, {"status": "picked_up", "at": "2026-09-13T09:00:00+00:00"}, commit=True
    )
    assert order.status == "out_for_delivery"
    assert order.courier_tracking_status == "picked_up"


@pytest.mark.parametrize(
    "current,target,expected",
    [
        ("ready_for_pickup", "handed_to_courier", True),
        ("handed_to_courier", "shipped", True),
        ("shipped", "out_for_delivery", True),
        ("out_for_delivery", "delivered", True),
        ("ready_for_pickup", "delivered", True),        # a missed push in between
        ("out_for_delivery", "delivery_failed", True),
        ("delivery_failed", "out_for_delivery", True),  # redispatch
        ("delivery_failed", "delivered", True),
        ("out_for_delivery", "handed_to_courier", False),
        ("delivered", "out_for_delivery", False),
        ("cancelled", "handed_to_courier", False),
        ("return_requested", "delivered", False),
        ("delivered", "delivery_failed", False),
        ("delivery_failed", "shipped", False),
    ],
)
def test_courier_status_mirroring_is_forward_only(current, target, expected):
    from app.services.courier_sync_service import should_mirror

    assert should_mirror(current, target) is expected


@pytest.mark.asyncio
async def test_courier_cancelled_never_cancels_our_order():
    """
    Cancelling a paid order has refund consequences. A courier-side cancel is
    an ops signal, not an instruction.
    """
    from app.services import courier_sync_service

    order = _FakeOrder()
    await courier_sync_service.apply_shipment_payload(
        _FakeDB(), order, {"status": "cancelled", "at": "2026-08-28T16:00:00+00:00"}, commit=True
    )
    assert order.courier_tracking_status == "cancelled"
    assert order.status == "shipped"

# ---------------------------------------------------------------------------
# Round-2 fields: will_retry and remittance_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_will_retry_is_recorded_on_a_failed_shipment(stub_status_transition):
    """
    The machine-readable half of a failure. `failure_note` is free text by
    design, so this is the only part of a failure we branch on: it decides
    refund-now versus tell-the-customer-tomorrow.
    """
    from app.services import courier_sync_service

    order = _FakeOrder()
    await courier_sync_service.apply_shipment_payload(
        _FakeDB(),
        order,
        {
            "status": "failed",
            "at": "2026-08-28T16:00:00+00:00",
            "will_retry": True,
            "failure_note": "Called 3 times, no answer",
        },
        commit=True,
    )
    assert order.courier_will_retry is True
    assert order.courier_failure_note == "Called 3 times, no answer"


@pytest.mark.asyncio
async def test_will_retry_false_is_recorded_not_treated_as_absent(stub_status_transition):
    """False and None mean different things -- False is 'heading for returned'."""
    from app.services import courier_sync_service

    order = _FakeOrder()
    order.courier_will_retry = True
    await courier_sync_service.apply_shipment_payload(
        _FakeDB(),
        order,
        {"status": "failed", "at": "2026-08-28T17:00:00+00:00", "will_retry": False},
        commit=True,
    )
    assert order.courier_will_retry is False


@pytest.mark.asyncio
async def test_remittance_id_is_recorded_when_the_cod_is_paid_out(stub_status_transition):
    from app.services import courier_sync_service

    order = _FakeOrder()
    await courier_sync_service.apply_shipment_payload(
        _FakeDB(),
        order,
        {"status": "delivered", "at": "2026-08-28T16:00:00+00:00", "remittance_id": 12},
        commit=True,
    )
    assert order.courier_remittance_id == 12


@pytest.mark.asyncio
async def test_a_null_remittance_id_never_clears_a_settled_one(stub_status_transition):
    """
    Most payloads carry `remittance_id: null` because most shipments are not
    settled yet. Letting that overwrite a real id would make money we have
    already received look unreceived.
    """
    from app.services import courier_sync_service

    order = _FakeOrder()
    order.courier_remittance_id = 12
    await courier_sync_service.apply_shipment_payload(
        _FakeDB(),
        order,
        {"status": "delivered", "at": "2026-08-29T09:00:00+00:00", "remittance_id": None},
        commit=True,
    )
    assert order.courier_remittance_id == 12


# ---------------------------------------------------------------------------
# Section 7a request shaping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_filter_accepts_a_list_and_is_sent_comma_separated(monkeypatch):
    from app.services.courier_service import CourierService as CS

    captured = {}

    async def _fake_request(method, path, *, json=None, params=None):
        captured["params"] = params
        return []

    monkeypatch.setattr(CS, "_request", staticmethod(_fake_request))
    await CS.list_shipments(status_filter=["in_transit", "out_for_delivery"])
    assert captured["params"]["status_filter"] == "in_transit,out_for_delivery"


@pytest.mark.asyncio
async def test_status_filter_still_accepts_a_bare_string(monkeypatch):
    from app.services.courier_service import CourierService as CS

    captured = {}

    async def _fake_request(method, path, *, json=None, params=None):
        captured["params"] = params
        return []

    monkeypatch.setattr(CS, "_request", staticmethod(_fake_request))
    await CS.list_shipments(status_filter="delivered")
    assert captured["params"]["status_filter"] == "delivered"


@pytest.mark.asyncio
async def test_list_limit_is_clamped_to_the_documented_maximum(monkeypatch):
    """Over 200 is rejected outright, which would break every sweep."""
    from app.services.courier_service import SHIPMENT_LIST_LIMIT_MAX
    from app.services.courier_service import CourierService as CS

    captured = {}

    async def _fake_request(method, path, *, json=None, params=None):
        captured["params"] = params
        return []

    monkeypatch.setattr(CS, "_request", staticmethod(_fake_request))
    await CS.list_shipments(limit=1000)
    assert captured["params"]["limit"] == SHIPMENT_LIST_LIMIT_MAX


# ---------------------------------------------------------------------------
# Booking field maxima (round-2 field answers)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recipient_name_and_phone_are_truncated_to_their_maxima(monkeypatch):
    """
    Exceeding these is a 422 at booking time, and a booking that fails on an
    over-long name is an order that silently never ships.
    """
    from app.services.courier_service import RECIPIENT_NAME_MAX, RECIPIENT_PHONE_MAX
    from app.services.courier_service import CourierService as CS

    captured = {}

    async def _fake_request(method, path, *, json=None, params=None):
        captured["json"] = json
        return {}

    monkeypatch.setattr(CS, "_request", staticmethod(_fake_request))
    await CS.book_shipment(
        merchant_order_ref="FC-TEST",
        quote_id="qt_x",
        payment_method="prepaid",
        recipient_name="N" * 400,
        recipient_phone="0" * 90,
        address_line1="1 Main St",
    )
    assert len(captured["json"]["recipient_name"]) == RECIPIENT_NAME_MAX
    assert len(captured["json"]["recipient_phone"]) == RECIPIENT_PHONE_MAX


# ---------------------------------------------------------------------------
# 2026-09-13 contract: postal cities, origin pricing, confirmed quotes
# ---------------------------------------------------------------------------


def _capture_requests(monkeypatch, response=None):
    from app.services.courier_service import CourierService as CS

    calls: list[dict] = []

    async def _fake_request(method, path, *, json=None, params=None):
        calls.append({"method": method, "path": path, "json": json, "params": params})
        return response if response is not None else {}

    monkeypatch.setattr(CS, "_request", staticmethod(_fake_request))
    return calls


@pytest.mark.asyncio
async def test_a_rate_is_asked_for_by_postal_city_and_names_the_outlet(monkeypatch):
    from app.services.courier_service import CourierService as CS

    calls = _capture_requests(
        monkeypatch,
        {
            "serviceable": True,
            "quote_id": "qt_1",
            "amount_lkr": "555.00",
            "expires_at": "2026-09-13T10:30:00+00:00",
            "origin_postal_city_id": 98,
        },
    )
    quote = await CS.get_rate(
        postal_city_id=812, weight_kg=Decimal("2.5"), payment_method="cod", cod_amount=Decimal("4000"), outlet_id=7
    )
    [call] = calls
    assert call["path"] == "/rates"
    assert call["json"]["postal_city_id"] == 812
    assert call["json"]["outlet_id"] == 7
    assert "city_id" not in call["json"]
    assert quote.amount_lkr == Decimal("555.00")
    assert quote.origin_postal_city_id == 98


@pytest.mark.asyncio
async def test_confirming_a_quote_posts_to_its_confirm_route(monkeypatch):
    from app.services.courier_service import CourierService as CS

    calls = _capture_requests(
        monkeypatch,
        {
            "serviceable": True,
            "quote_id": "qt_1",
            "amount_lkr": "555.00",
            "expires_at": "2026-09-20T10:00:00+00:00",
            "confirmed": True,
        },
    )
    quote = await CS.confirm_quote("qt_1")
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/rates/qt_1/confirm"
    assert quote.confirmed is True


@pytest.mark.asyncio
async def test_a_booking_says_how_the_customer_pays_and_never_sends_the_retired_post_office(monkeypatch):
    from app.services.courier_service import CourierService as CS

    calls = _capture_requests(monkeypatch)
    await CS.book_shipment(
        merchant_order_ref="FC-COD",
        quote_id="qt_1",
        payment_method="cod",
        recipient_name="A. Perera",
        recipient_phone="0771234567",
        address_line1="7 Beach Road",
        postal_city="Galle",
        district="Galle",
        cod_amount=Decimal("4820.00"),
        outlet_id=7,
    )
    body = calls[0]["json"]
    assert body["payment_method"] == "cod"
    assert body["cod_amount"] == 4820.0
    assert body["outlet_id"] == 7
    assert "post_office" not in body
    assert "handover_code" not in body
    # The flat rendering still reads naturally, postal city included.
    assert body["recipient_address"] == "7 Beach Road, Galle, Galle"
