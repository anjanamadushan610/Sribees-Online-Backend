"""
S3-compatible StorageProvider (AWS S3, Cloudflare R2, MinIO, Spaces).

**Not active today** — ``STORAGE_BACKEND`` defaults to ``local``. It exists so
that the migration is a config change rather than a code project, and so the
abstraction is proven against a second backend instead of being a single-
implementation interface that only looks portable.

The critical detail: ``save()`` returns the **same relative path shape** as the
local provider (``/uploads/categories/tea_a1b2.jpg``) and uses that path as the
object key. So switching backends means:

    STORAGE_BACKEND=s3
    MEDIA_BASE_URL=https://cdn.example.com

and nothing else. Existing rows already hold exactly the path S3 will serve —
no URL rewrite, no migration script.

``boto3`` is imported lazily so the dependency is only needed if this provider
is actually selected.
"""
import mimetypes
from typing import BinaryIO, Optional

from loguru import logger

from app.config.settings import settings
from app.services.storage.base import (
    StorageError,
    StorageProvider,
    build_relative_path,
)


class S3StorageProvider(StorageProvider):
    """Stores media in an S3-compatible bucket."""

    def __init__(self, bucket: Optional[str] = None):
        self.bucket = bucket or settings.s3_bucket_name
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config as BotoConfig
            except ImportError as exc:  # pragma: no cover
                raise StorageError(
                    "STORAGE_BACKEND=s3 requires boto3. Install it, or set "
                    "STORAGE_BACKEND=local."
                ) from exc

            kwargs = {
                "service_name": "s3",
                "region_name": settings.aws_region,
                "config": BotoConfig(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            }
            if settings.aws_access_key_id and settings.aws_secret_access_key:
                kwargs["aws_access_key_id"] = settings.aws_access_key_id
                kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
            if settings.s3_endpoint_url:
                kwargs["endpoint_url"] = settings.s3_endpoint_url

            self._client = boto3.client(**kwargs)
            logger.info(f"S3 storage initialised — bucket={self.bucket}")
        return self._client

    @staticmethod
    def _key_for(relative_path: str) -> str:
        """The stored path IS the key, minus the leading slash."""
        return relative_path.lstrip("/")

    def save(
        self,
        fileobj: BinaryIO,
        filename: str,
        folder: str = "uploads",
        content_type: Optional[str] = None,
    ) -> str:
        # Same helper as the local provider, so both backends produce an
        # identical path for the same upload. That identity is the abstraction.
        relative_path = build_relative_path(
            folder, filename, settings.media_url_prefix
        )
        ct = (
            content_type
            or mimetypes.guess_type(relative_path)[0]
            or "application/octet-stream"
        )

        try:
            self._get_client().upload_fileobj(
                Fileobj=fileobj,
                Bucket=self.bucket,
                Key=self._key_for(relative_path),
                ExtraArgs={
                    "ContentType": ct,
                    "CacheControl": "public, max-age=31536000",
                },
            )
        except StorageError:
            raise
        except Exception as exc:
            logger.error(f"S3 upload failed for {relative_path}: {exc}")
            raise StorageError(f"Failed to upload file to S3: {exc}") from exc

        logger.info(f"S3 upload OK: {relative_path} ({ct})")
        return relative_path

    def delete(self, path: str) -> bool:
        if not path or path.startswith(("http://", "https://")):
            return False
        try:
            self._get_client().delete_object(
                Bucket=self.bucket, Key=self._key_for(path)
            )
            logger.info(f"S3 delete OK: {path}")
            return True
        except Exception as exc:
            logger.error(f"S3 delete failed for {path}: {exc}")
            return False
