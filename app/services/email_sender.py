"""
Email Sender — plain SMTP, configured by the MAIL_* settings.

The only mail this server sends is the partner portal's password-reset code,
so this is deliberately small: one message, HTML plus a text fallback, sent
with the standard library in a worker thread (smtplib blocks). No provider SDK,
so moving to another mailbox is a change of .env, not of code.

When MAIL_USERNAME / MAIL_PASSWORD are not set the server cannot send mail at
all; ``is_configured`` says so and ``send`` raises ``EmailSendError`` rather
than pretending a message went out.
"""
import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from loguru import logger

from app.config.settings import settings

_TIMEOUT_SECONDS = 20


class EmailSendError(RuntimeError):
    """The message was not handed to the mail server."""


def is_configured() -> bool:
    return bool(settings.mail_server and settings.mail_username and settings.mail_password)


def mask_email(email: str) -> str:
    """``partner@example.com`` -> ``pa*****@example.com`` for logs."""
    local, _, domain = email.partition("@")
    return f"{local[:2]}{'*' * max(1, len(local) - 2)}@{domain}"


def _build(to: str, subject: str, text: str, html: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.mail_from_name, settings.mail_from))
    msg["To"] = to
    msg["Message-ID"] = make_msgid(domain=settings.mail_from.partition("@")[2] or None)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def _send_blocking(msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if settings.mail_ssl_tls:
        with smtplib.SMTP_SSL(
            settings.mail_server, settings.mail_port, timeout=_TIMEOUT_SECONDS, context=context
        ) as smtp:
            smtp.login(settings.mail_username, settings.mail_password)
            smtp.send_message(msg)
        return
    with smtplib.SMTP(settings.mail_server, settings.mail_port, timeout=_TIMEOUT_SECONDS) as smtp:
        if settings.mail_starttls:
            smtp.starttls(context=context)
        smtp.login(settings.mail_username, settings.mail_password)
        smtp.send_message(msg)


async def send_email(to: str, subject: str, text: str, html: str) -> None:
    """Send one message. Raises ``EmailSendError`` on any failure."""
    if not is_configured():
        raise EmailSendError("SMTP is not configured (MAIL_USERNAME / MAIL_PASSWORD)")
    msg = _build(to, subject, text, html)
    try:
        await asyncio.to_thread(_send_blocking, msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailSendError(f"{type(exc).__name__}: {exc}") from exc
    logger.info(f"Email '{subject}' sent to {mask_email(to)}")
