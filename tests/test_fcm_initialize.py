"""
Firebase starts only from credentials that can work, and says why when it
cannot. A push service that silently fails to start is how production ran
without push notifications for weeks, so every refusal is a clear False
with a log line, never an exception out of startup.

No network: firebase-admin's Certificate and initialize_app are replaced.
"""
import json

import pytest

pytest.importorskip("firebase_admin")

from app.services import fcm_service  # noqa: E402
from app.services.fcm_service import FCMService  # noqa: E402


@pytest.fixture
def firebase(monkeypatch):
    """Fresh, uninitialised service with the SDK calls recorded, not made."""
    calls = {"cert": [], "init": 0}

    def fake_cert(source):
        calls["cert"].append(source)
        if source == "boom":
            raise ValueError("bad key")
        return object()

    def fake_init(cred):
        calls["init"] += 1

    monkeypatch.setattr(FCMService, "_initialized", False)
    monkeypatch.setattr(fcm_service.credentials, "Certificate", fake_cert)
    monkeypatch.setattr(fcm_service.firebase_admin, "initialize_app", fake_init)
    monkeypatch.setattr(fcm_service.settings, "firebase_credentials_json", None, raising=False)
    monkeypatch.setattr(fcm_service.settings, "firebase_credentials_path", None, raising=False)
    return calls


def test_nothing_configured_means_push_is_off(firebase):
    assert FCMService.initialize() is False
    assert firebase["init"] == 0


def test_pasted_json_is_used_directly(firebase, monkeypatch):
    monkeypatch.setattr(fcm_service.settings, "firebase_credentials_json", json.dumps({"type": "service_account"}))
    assert FCMService.initialize() is True
    assert firebase["cert"] == [{"type": "service_account"}]
    assert FCMService.initialize() is True, "a second call is a no-op"
    assert firebase["init"] == 1


def test_json_that_does_not_parse_is_refused(firebase, monkeypatch):
    monkeypatch.setattr(fcm_service.settings, "firebase_credentials_json", "{not json")
    assert FCMService.initialize() is False
    assert firebase["cert"] == []


def test_a_missing_credentials_file_is_refused(firebase, monkeypatch, tmp_path):
    monkeypatch.setattr(fcm_service.settings, "firebase_credentials_path", str(tmp_path / "nope.json"))
    assert FCMService.initialize() is False


def test_a_credentials_file_is_used(firebase, monkeypatch, tmp_path):
    key = tmp_path / "firebase.json"
    key.write_text("{}")
    monkeypatch.setattr(fcm_service.settings, "firebase_credentials_path", str(key))
    assert FCMService.initialize() is True
    assert firebase["cert"] == [str(key)]


def test_a_key_firebase_rejects_is_reported_not_raised(firebase, monkeypatch):
    monkeypatch.setattr(fcm_service.settings, "firebase_credentials_json", json.dumps("boom"))
    assert FCMService.initialize() is False
    assert FCMService._initialized is False


def test_without_the_sdk_push_is_off(firebase, monkeypatch):
    monkeypatch.setattr(fcm_service, "FIREBASE_AVAILABLE", False)
    assert FCMService.initialize() is False
