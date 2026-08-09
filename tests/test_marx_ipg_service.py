"""
Tests for MarxIPGService response parsing (doc sections 5.1/5.4/5.5).

No network calls -- MarxIPGService._parse() takes an already-received
httpx.Response, so these construct one directly from the doc's own sample
JSON bodies rather than mocking the HTTP layer.
"""
import httpx
import pytest

from app.services.marx_ipg_service import MarxIPGError, MarxIPGService

_CREATE_ORDER_SUCCESS = {
    "status": 0,
    "message": "SUCCESS",
    "data": {
        "payUrl": "https://ipg.dev.marxpos.com/?tr=5057",
        "trId": 5057,
        "merchantRID": "20231023003",
    },
}

_INITIATE_PAYMENT_SUCCESS = {
    "status": 0,
    "message": "SUCCESS",
    "data": {
        "summaryResult": "SUCCESS",
        "trId": 5057,
        "merchantRID": "20231023003",
        "gatewayResponse": {
            "result": "SUCCESS",
            "response": {"gatewayCode": "APPROVED", "acquirerCode": "00", "acquirerMessage": "Approved"},
            "order": {"amount": 230.00, "currency": "LKR", "status": "CAPTURED", "totalCapturedAmount": 230.00},
        },
    },
}

_INITIATE_PAYMENT_FAILURE = {
    "status": 0,
    "message": "SUCCESS",
    "data": {
        "summaryResult": "FAILURE",
        "trId": 5058,
        "merchantRID": "20231023004",
        "gatewayResponse": {
            "result": "FAILURE",
            "response": {"gatewayCode": "DECLINED"},
            "order": {"status": "DECLINED"},
        },
    },
}

_TOP_LEVEL_FAILURE = {"status": 1, "message": "Invalid merchant-api-key"}


def _response(body: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=body, request=httpx.Request("POST", "https://example.test"))


def test_parse_create_order_success():
    data = MarxIPGService._parse("/orders", _response(_CREATE_ORDER_SUCCESS))
    assert data["payUrl"] == "https://ipg.dev.marxpos.com/?tr=5057"
    assert data["trId"] == 5057


def test_parse_top_level_status_1_raises():
    with pytest.raises(MarxIPGError):
        MarxIPGService._parse("/orders", _response(_TOP_LEVEL_FAILURE))


def test_parse_non_200_raises():
    with pytest.raises(MarxIPGError):
        MarxIPGService._parse("/orders", _response({"status": 0, "data": {}}, status_code=502))


def test_initiate_payment_success_shape():
    """
    initiate_payment() extracts summary_result/gateway_code/order_status from
    the nested gatewayResponse -- assert the exact field paths match the doc's
    5.4 sample (gatewayResponse.response.gatewayCode, gatewayResponse.order.status).
    """
    data = MarxIPGService._parse("/orders/5057", _response(_INITIATE_PAYMENT_SUCCESS))
    gateway_response = data["gatewayResponse"]
    assert data["summaryResult"] == "SUCCESS"
    assert gateway_response["response"]["gatewayCode"] == "APPROVED"
    assert gateway_response["order"]["status"] == "CAPTURED"


def test_initiate_payment_failure_shape():
    data = MarxIPGService._parse("/orders/5058", _response(_INITIATE_PAYMENT_FAILURE))
    assert data["summaryResult"] == "FAILURE"
    assert data["gatewayResponse"]["response"]["gatewayCode"] == "DECLINED"
