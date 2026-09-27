"""
SMS gateway -- the one place that knows which company carries a text message.

Everything above this module (OTP issuing, the endpoints, the mobile app) only
knows "send this text to this number". Changing provider is a new class here
plus ``SMS_PROVIDER`` in the server's environment; no endpoint, response shape
or app release changes.

Providers:

- ``console``  -- writes the message to the server log. The default, so a
  laptop, CI and any server without credentials never spends real credit.
- ``notifylk`` -- Notify.lk (https://developer.notify.lk/api-endpoints/).

Credentials travel in a POST body, never a query string: URLs end up in HTTP
client logs and proxy logs, bodies do not. Nothing here ever logs a
credential, and the OTP itself is only logged by the console provider.
"""
from __future__ import annotations

import logging
from typing import Optional, Protocol

import httpx
from loguru import logger

from app.config.settings import settings

REQUEST_TIMEOUT = 15.0


class SmsSendError(Exception):
    """The provider did not accept the message. Safe to show in logs."""


class SmsGateway(Protocol):
    name: str

    async def send(self, phone: str, text: str) -> None:
        """Deliver ``text`` to ``phone`` (+94 form) or raise SmsSendError."""

    async def balance(self) -> Optional[float]:
        """Remaining account credit, or None when the provider has none."""


def to_msisdn(phone: str) -> str:
    """``+94771234567`` -> ``94771234567``, the form SMS providers take."""
    from app.utils.identity import normalize_mobile

    return normalize_mobile(phone).lstrip("+")


def mask_phone(phone: str) -> str:
    """``+94771234567`` -> ``+94 77 *** **67``: enough to recognise, not to copy."""
    from app.utils.identity import normalize_mobile

    try:
        digits = normalize_mobile(phone)[3:]
    except ValueError:
        return "***"
    return f"+94 {digits[:2]} *** **{digits[-2:]}"


class ConsoleSmsGateway:
    """Log instead of send. Development, tests, and any server with no provider."""

    name = "console"

    async def send(self, phone: str, text: str) -> None:
        logger.info(f"[CONSOLE SMS] to {phone}: {text}")

    async def balance(self) -> Optional[float]:
        return None


class NotifyLkGateway:
    """Notify.lk HTTP API v1."""

    name = "notifylk"

    def __init__(self, user_id: str, api_key: str, sender_id: str, base_url: str) -> None:
        self._user_id = user_id
        self._api_key = api_key
        self._sender_id = sender_id
        self._base_url = base_url.rstrip("/")

    def _auth(self) -> dict:
        return {"user_id": self._user_id, "api_key": self._api_key}

    @staticmethod
    def _body(response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise SmsSendError(f"Notify.lk answered HTTP {response.status_code} without JSON")
        if response.status_code != 200 or body.get("status") != "success":
            # Their error payloads carry a reason, never our credentials.
            reason = body.get("errors") or body.get("message") or body.get("data")
            raise SmsSendError(f"Notify.lk refused (HTTP {response.status_code}): {str(reason)[:200]}")
        return body

    async def send(self, phone: str, text: str) -> None:
        data = {
            **self._auth(),
            "sender_id": self._sender_id,
            "to": to_msisdn(phone),
            "message": text,
        }
        if not text.isascii():
            data["type"] = "unicode"
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.post(f"{self._base_url}/send", data=data)
        except httpx.HTTPError as exc:
            raise SmsSendError(f"Notify.lk unreachable: {type(exc).__name__}") from exc
        self._body(response)

    async def balance(self) -> Optional[float]:
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                # Their status call is GET-only, so the credentials ride in the
                # query string here. It is called by a Super Admin on demand,
                # never per customer, and httpx's own URL logging is silenced
                # below.
                response = await client.get(f"{self._base_url}/status", params=self._auth())
        except httpx.HTTPError as exc:
            raise SmsSendError(f"Notify.lk unreachable: {type(exc).__name__}") from exc
        data = self._body(response).get("data") or {}
        value = data.get("acc_balance")
        return float(value) if value is not None else None


class UnconfiguredGateway:
    """A provider was chosen but its credentials are missing: fail every send."""

    def __init__(self, name: str, missing: list[str]) -> None:
        self.name = name
        self.missing = missing

    async def send(self, phone: str, text: str) -> None:
        raise SmsSendError(f"SMS provider {self.name!r} is missing {', '.join(self.missing)}")

    async def balance(self) -> Optional[float]:
        raise SmsSendError(f"SMS provider {self.name!r} is missing {', '.join(self.missing)}")


def get_sms_gateway() -> SmsGateway:
    """The provider this server is configured for, read fresh each call."""
    provider = (settings.sms_provider or "console").strip().lower()
    if provider == "notifylk":
        missing = [
            name
            for name, value in (
                ("NOTIFYLK_USER_ID", settings.notifylk_user_id),
                ("NOTIFYLK_API_KEY", settings.notifylk_api_key),
                ("NOTIFYLK_SENDER_ID", settings.notifylk_sender_id),
            )
            if not (value or "").strip()
        ]
        if missing:
            return UnconfiguredGateway(provider, missing)
        return NotifyLkGateway(
            user_id=settings.notifylk_user_id.strip(),
            api_key=settings.notifylk_api_key.strip(),
            sender_id=settings.notifylk_sender_id.strip(),
            base_url=settings.notifylk_base_url,
        )
    if provider != "console":
        logger.error(f"Unknown SMS_PROVIDER {provider!r}; texts go to the log instead")
    return ConsoleSmsGateway()


# httpx logs every request URL at INFO. The balance call above carries
# credentials in its URL, so keep httpx's own logger to warnings and worse.
logging.getLogger("httpx").setLevel(logging.WARNING)
