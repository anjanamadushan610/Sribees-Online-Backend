"""
The language of the request being served, for text built deep inside a
response (an order's payment line, its status label) without passing the
request down through every formatter.

Read from ``Accept-Language``, which the app sets from its language picker;
English when absent. A pure ASGI middleware, so the value is set before any
endpoint or dependency runs and reset after.
"""
from contextvars import ContextVar

from app.utils.i18n import DEFAULT_LANGUAGE, resolve_language

_language: ContextVar[str] = ContextVar("request_language", default=DEFAULT_LANGUAGE)


def current_language() -> str:
    return _language.get()


class RequestLanguageMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = next(
            (value.decode("latin-1") for name, value in scope.get("headers", []) if name == b"accept-language"),
            None,
        )
        token = _language.set(resolve_language(header))
        try:
            await self.app(scope, receive, send)
        finally:
            _language.reset(token)
