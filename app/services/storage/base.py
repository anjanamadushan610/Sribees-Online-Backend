"""
Storage Provider interface.

Every media write and delete in the application goes through this interface, so
the concrete backend (local disk today; S3 or Cloudflare R2 later) is a single
swappable object rather than something the routers know about.

The contract that makes the swap free:

  * ``save()`` returns a **relative path** — ``/uploads/categories/tea_a1b2.jpg``
    — and that, and only that, is what the database stores.
  * Nothing here builds an absolute URL. The origin lives in ``MEDIA_BASE_URL``
    and is prepended at serialization time by ``app.core.media.media_url``.

Because the stored value carries no origin, moving to S3 means writing an
``S3StorageProvider`` and pointing ``MEDIA_BASE_URL`` at the bucket. No rows
change, so there is no URL migration.
"""
import re
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO, Optional

# Anything outside this set is stripped from a filename before it is used.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


def safe_stem(filename: str) -> str:
    """Reduce a user-supplied filename to something safe to write."""
    cleaned = _SAFE_NAME.sub("_", Path(filename).stem).strip("_")
    return (cleaned or "file")[:60]


def safe_suffix(filename: str) -> str:
    """Keep a plausible extension, or none at all."""
    suffix = Path(filename).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ""


def build_relative_path(folder: str, filename: str, url_prefix: str) -> str:
    """
    Compose the stored path: ``/{prefix}/{folder}/{stem}_{random}{ext}``.

    Shared by every provider on purpose — local disk and S3 must produce the
    *identical* path for the same upload, because that identity is what lets the
    backend be swapped without rewriting a single row.

    The random suffix stops two admins uploading "logo.png" from clobbering
    each other.
    """
    folder_part = _SAFE_NAME.sub("_", folder.strip("/")) or "uploads"
    name = f"{safe_stem(filename)}_{uuid.uuid4().hex[:12]}{safe_suffix(filename)}"
    return f"/{url_prefix.strip('/')}/{folder_part}/{name}"


class StorageProvider(ABC):
    """Persist and remove media files."""

    @abstractmethod
    def save(
        self,
        fileobj: BinaryIO,
        filename: str,
        folder: str = "uploads",
        content_type: Optional[str] = None,
    ) -> str:
        """
        Store *fileobj* and return its **relative path**.

        The returned value is what gets written to the database, so it must be
        origin-free and stable — e.g. ``/uploads/categories/tea_a1b2c3.jpg``.

        Raises ``StorageError`` if the file cannot be stored.
        """

    @abstractmethod
    def delete(self, path: str) -> bool:
        """
        Remove the object at a stored *path*. Returns True if it is now gone.

        Must be tolerant of a path that is already absent (an image deleted
        twice, or a row whose file was cleaned up out of band) — that is a
        no-op, not an error. Deleting media is never worth failing a request
        the user has already been told succeeded.
        """


class StorageError(RuntimeError):
    """Raised when a provider cannot store or remove a file."""
