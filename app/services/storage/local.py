"""
Local disk StorageProvider.

Writes uploads under ``MEDIA_ROOT`` and serves them from ``MEDIA_URL_PREFIX``
(mounted as StaticFiles in main.py). This is the default backend: no bucket, no
credentials, no per-request egress cost.

Operational note: ``MEDIA_ROOT`` **must** be a mounted volume in production.
Container filesystems are ephemeral, so without one every deploy silently
discards every image an admin has uploaded.
"""
import mimetypes
from pathlib import Path
from typing import BinaryIO, Optional

from loguru import logger

from app.config.settings import settings
from app.services.storage.base import (
    StorageError,
    StorageProvider,
    build_relative_path,
)


class LocalDiskStorageProvider(StorageProvider):
    """Stores media on the local filesystem."""

    def __init__(
        self,
        media_root: Optional[str] = None,
        url_prefix: Optional[str] = None,
    ):
        self.media_root = Path(media_root or settings.media_root)
        self.url_prefix = (url_prefix or settings.media_url_prefix).strip("/")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve(self, relative_path: str) -> Optional[Path]:
        """
        Map a stored relative path back to a file on disk.

        Returns None if the path escapes ``media_root``. A stored value is
        normally ours, but a crafted one ("/uploads/../../etc/passwd") must not
        be able to delete an arbitrary file, so the resolved path is checked to
        be inside the root before anything touches it.
        """
        rel = relative_path.strip("/")
        prefix = f"{self.url_prefix}/"
        if rel.startswith(prefix):
            rel = rel[len(prefix):]

        candidate = (self.media_root / rel).resolve()
        root = self.media_root.resolve()
        if root != candidate and root not in candidate.parents:
            logger.warning(f"Refusing to touch path outside media root: {relative_path}")
            return None
        return candidate

    # ------------------------------------------------------------------
    # StorageProvider
    # ------------------------------------------------------------------

    def save(
        self,
        fileobj: BinaryIO,
        filename: str,
        folder: str = "uploads",
        content_type: Optional[str] = None,
    ) -> str:
        relative_path = build_relative_path(folder, filename, self.url_prefix)

        # Strip the URL prefix back off to get the on-disk location: the path is
        # a URL contract, media_root is where it actually lives.
        on_disk = self.media_root / relative_path.lstrip("/").removeprefix(
            f"{self.url_prefix}/"
        )
        try:
            on_disk.parent.mkdir(parents=True, exist_ok=True)
            with open(on_disk, "wb") as out:
                out.write(fileobj.read())
        except OSError as exc:
            logger.error(f"Local storage write failed for {relative_path}: {exc}")
            raise StorageError(f"Failed to store file: {exc}") from exc

        ct = content_type or mimetypes.guess_type(relative_path)[0] or "application/octet-stream"
        logger.info(f"Local storage write OK: {relative_path} ({ct})")
        # Only the relative path is returned — and therefore only it is stored.
        return relative_path

    def delete(self, path: str) -> bool:
        if not path:
            return True

        # A legacy absolute URL (from the old S3 service) has no local file.
        if path.startswith(("http://", "https://")):
            logger.info(f"Skipping local delete of an absolute URL: {path}")
            return False

        target = self._resolve(path)
        if target is None:
            return False

        try:
            target.unlink(missing_ok=True)  # already gone == success
            logger.info(f"Local storage delete OK: {path}")
            return True
        except OSError as exc:
            # Never fail the caller's request over a leftover file.
            logger.error(f"Local storage delete failed for {path}: {exc}")
            return False
