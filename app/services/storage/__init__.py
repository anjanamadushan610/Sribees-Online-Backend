"""
Storage provider selection.

``get_storage()`` is the only way the application obtains a provider. Routers and
services depend on the ``StorageProvider`` interface, never on a concrete
backend, so moving from disk to S3/R2 touches this module and the env — nothing
else.
"""
from functools import lru_cache

from loguru import logger

from app.config.settings import settings
from app.services.storage.base import (
    StorageError,
    StorageProvider,
    build_relative_path,
)
from app.services.storage.local import LocalDiskStorageProvider

__all__ = [
    "StorageProvider",
    "StorageError",
    "LocalDiskStorageProvider",
    "build_relative_path",
    "get_storage",
]


@lru_cache(maxsize=1)
def get_storage() -> StorageProvider:
    """Return the configured provider (cached — providers are stateless)."""
    backend = (settings.storage_backend or "local").strip().lower()

    if backend == "s3":
        # Imported here so boto3 is only required when S3 is actually selected.
        from app.services.storage.s3 import S3StorageProvider

        logger.info(f"Media storage: S3 (bucket={settings.s3_bucket_name})")
        return S3StorageProvider()

    if backend != "local":
        logger.warning(
            f"Unknown STORAGE_BACKEND '{backend}' — falling back to local disk."
        )

    logger.info(
        f"Media storage: local disk (root={settings.media_root}, "
        f"served at {settings.media_url_prefix})"
    )
    return LocalDiskStorageProvider()
