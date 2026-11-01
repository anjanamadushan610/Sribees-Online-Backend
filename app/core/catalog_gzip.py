"""Gzip for public catalogue responses only; see the note where app/main.py adds it."""

from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

COMPRESSED_PREFIXES = ("/api/v1/products", "/api/v1/categories")
MINIMUM_SIZE = 1024
COMPRESS_LEVEL = 5  # most of level 9's saving for much less CPU


class CatalogGZipMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=MINIMUM_SIZE, compresslevel=COMPRESS_LEVEL)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(COMPRESSED_PREFIXES):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)
