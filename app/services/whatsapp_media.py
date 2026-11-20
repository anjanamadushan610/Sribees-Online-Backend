"""Authorized, bounded proxy for private WhatsApp attachment references."""

from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from fastapi.responses import Response

from app.config.settings import settings

MAX_MEDIA_BYTES = 5 * 1024 * 1024
ALLOWED_MIME = {"image/jpeg", "image/png", "application/pdf"}


async def download_attachment(reference):
    if not reference or reference.get("mime_type") not in ALLOWED_MIME:
        raise HTTPException(404, "Attachment unavailable")
    headers = {"Authorization": "Bearer " + settings.whatsapp_access_token}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            metadata = await client.get(
                f"https://graph.facebook.com/{settings.whatsapp_graph_version}/{reference['id']}",
                headers=headers,
            )
            metadata.raise_for_status()
            data = metadata.json()
            url = data.get("url", "")
            parsed = urlparse(url)
            host = parsed.hostname or ""
            if (
                parsed.scheme != "https"
                or not (
                    host == "lookaside.fbsbx.com"
                    or host.endswith(".facebook.com")
                    or host.endswith(".fbcdn.net")
                )
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
            ):
                raise ValueError("Untrusted media host")
            if (
                data.get("mime_type") not in ALLOWED_MIME
                or int(data.get("file_size", MAX_MEDIA_BYTES + 1)) > MAX_MEDIA_BYTES
            ):
                raise ValueError("Unsupported attachment")
            content = bytearray()
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_MEDIA_BYTES:
                        raise ValueError("Attachment too large")
            return Response(
                bytes(content),
                media_type=data["mime_type"],
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": 'attachment; filename="support-attachment"',
                },
            )
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(
            502, "Attachment unavailable; ask customer to resend"
        ) from None
