"""
SMS gateway -- the one place that knows which company carries a text message.

Everything above this module (OTP issuing, the endpoints, the mobile app) only
knows "send this text to this number". Changing provider is a new class here
plus ``SMS_PROVIDER`` in the server's environment; no endpoint, response shape
or app release changes.

Providers:

- ``console``  -- writes the message to the server log. The default, so a
  laptop, CI and any server without credentials never spends real credit.
- ``textlk``   -- Text.lk API v3 (https://text.lk/docs/sms-api-endpoints/).

The API token travels in the Authorization header, never a URL: URLs end up
in HTTP client and proxy logs. Nothing here ever logs a credential, and the
OTP itself is only logged by the console provider.
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


class TextLkGateway:
    """Text.lk HTTP API v3 (Bearer token)."""

    name = "textlk"

    def __init__(self, api_token: str, sender_id: str, base_url: str) -> None:
        self._api_token = api_token
        self._sender_id = sender_id
        self._base_url = base_url.rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_token}", "Accept": "application/json"}

    @staticmethod
    def _body(response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise SmsSendError(f"Text.lk answered HTTP {response.status_code} without JSON")
        # Text.lk answers some failures with HTTP 200 and status "error", and
        # documents success as `true` while sending "success": check the body.
        if response.status_code != 200 or body.get("status") not in (True, "success"):
            reason = body.get("message") or body.get("errors") or body.get("data")
            raise SmsSendError(f"Text.lk refused (HTTP {response.status_code}): {str(reason)[:200]}")
        return body

    async def send(self, phone: str, text: str) -> None:
        payload = {
            "recipient": to_msisdn(phone),
            "sender_id": self._sender_id,
            "type": "plain" if text.isascii() else "unicode",
            "message": text,
        }
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.post(
                    f"{self._base_url}/sms/send", json=payload, headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise SmsSendError(f"Text.lk unreachable: {type(exc).__name__}") from exc
        self._body(response)

    async def balance(self) -> Optional[float]:
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.get(f"{self._base_url}/balance", headers=self._headers())
        except httpx.HTTPError as exc:
            raise SmsSendError(f"Text.lk unreachable: {type(exc).__name__}") from exc
        data = self._body(response).get("data") or {}
        value = data.get("remaining_balance", data.get("remaining_unit"))
        try:
            return float(str(value).replace(",", "")) if value is not None else None
        except ValueError:
            return None


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
    if provider == "textlk":
        missing = [
            name
            for name, value in (
                ("TEXTLK_API_TOKEN", settings.textlk_api_token),
                ("TEXTLK_SENDER_ID", settings.textlk_sender_id),
            )
            if not (value or "").strip()
        ]
        if missing:
            return UnconfiguredGateway(provider, missing)
        return TextLkGateway(
            api_token=settings.textlk_api_token.strip(),
            sender_id=settings.textlk_sender_id.strip(),
            base_url=settings.textlk_base_url,
        )
    if provider != "console":
        logger.error(f"Unknown SMS_PROVIDER {provider!r}; texts go to the log instead")
    return ConsoleSmsGateway()


# httpx logs every request URL at INFO; keep it quiet so a provider that
# puts a credential in a URL can never leak it into the server log.
logging.getLogger("httpx").setLevel(logging.WARNING)
