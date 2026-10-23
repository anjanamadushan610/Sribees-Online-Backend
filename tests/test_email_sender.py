"""
The SMTP sender behind the partner password-reset mail. No network: smtplib
is replaced by a recorder, so these pin what is sent and how failures surface.
"""
import smtplib

import pytest

from app.config.settings import settings
from app.services import email_sender


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []
    fail_on_send = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        self.calls: list[str] = []
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append("login")

    def send_message(self, msg):
        if _FakeSMTP.fail_on_send:
            raise smtplib.SMTPRecipientsRefused({})
        self.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    _FakeSMTP.instances = []
    _FakeSMTP.fail_on_send = False
    monkeypatch.setattr(email_sender.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(email_sender.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(settings, "mail_username", "mailer")
    monkeypatch.setattr(settings, "mail_password", "not-a-real-password")
    monkeypatch.setattr(settings, "mail_server", "smtp.example.test")
    monkeypatch.setattr(settings, "mail_ssl_tls", False)
    monkeypatch.setattr(settings, "mail_starttls", True)
    return _FakeSMTP


async def test_sends_html_with_a_text_fallback_over_starttls(smtp):
    await email_sender.send_email("partner@example.com", "Code", "plain body", "<b>html</b>")
    (conn,) = smtp.instances
    assert conn.calls == ["starttls", "login"]
    (msg,) = conn.sent
    assert msg["To"] == "partner@example.com"
    assert settings.mail_from in msg["From"]
    types = [part.get_content_type() for part in msg.iter_parts()]
    assert types == ["text/plain", "text/html"]


async def test_ssl_mode_skips_starttls(smtp, monkeypatch):
    monkeypatch.setattr(settings, "mail_ssl_tls", True)
    await email_sender.send_email("partner@example.com", "Code", "t", "<p>h</p>")
    assert smtp.instances[0].calls == ["login"]


async def test_a_refused_message_is_an_email_send_error(smtp):
    smtp.fail_on_send = True
    with pytest.raises(email_sender.EmailSendError):
        await email_sender.send_email("partner@example.com", "Code", "t", "<p>h</p>")


async def test_without_credentials_nothing_is_attempted(smtp, monkeypatch):
    monkeypatch.setattr(settings, "mail_password", "")
    assert not email_sender.is_configured()
    with pytest.raises(email_sender.EmailSendError):
        await email_sender.send_email("partner@example.com", "Code", "t", "<p>h</p>")
    assert smtp.instances == []


def test_mask_email():
    assert email_sender.mask_email("partner@example.com") == "pa*****@example.com"
