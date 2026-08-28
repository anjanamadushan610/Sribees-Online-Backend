"""
One-time migration: copy every file under MEDIA_ROOT (local disk storage) into
the configured S3 bucket, preserving the exact same relative path as the S3
object key.

Why the path must stay identical: the database only ever stores the relative
path (e.g. ``/uploads/products/tea_a1b2.jpg``), never an origin — see
app/core/media.py. Switching STORAGE_BACKEND from 'local' to 's3' does not
touch a single row; every existing product/category/banner image keeps
resolving correctly as long as the object exists in the bucket under that same
path. This script is what makes that true — run it once, BEFORE flipping
STORAGE_BACKEND to 's3' in production, so no image goes missing mid-cutover.

Usage (run from fastapi_backend/, with the venv active and S3 settings in
.env — AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or an instance role,
S3_BUCKET_NAME, AWS_REGION, and STORAGE_BACKEND=s3 so get_storage() returns
the S3 provider):

    python scripts/migrate_media_to_s3.py --dry-run   # see what would upload
    python scripts/migrate_media_to_s3.py              # actually upload
    python scripts/migrate_media_to_s3.py --skip-existing  # don't re-upload objects already in the bucket

Safe to re-run: with --skip-existing, anything already in the bucket is left
alone (a HeadObject check per file). Without it, every file is re-uploaded
unconditionally — harmless (same bytes, same key) but slower and costs
transfer.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

from app.config.settings import settings  # noqa: E402


def _iter_local_files(media_root: Path):
    if not media_root.exists():
        return
    for path in sorted(media_root.rglob("*")):
        if path.is_file():
            yield path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be uploaded without touching the bucket.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files that already exist in the bucket under the same key.",
    )
    args = parser.parse_args()

    media_root = Path(settings.media_root)
    url_prefix = settings.media_url_prefix.strip("/")

    files = list(_iter_local_files(media_root))
    if not files:
        logger.info(f"Nothing to migrate — {media_root} has no files.")
        return

    logger.info(f"Found {len(files)} file(s) under {media_root}")

    if args.dry_run:
        for f in files:
            rel = f.relative_to(media_root).as_posix()
            logger.info(f"[dry-run] would upload: /{url_prefix}/{rel}")
        logger.info(f"[dry-run] {len(files)} file(s) would be uploaded to "
                     f"bucket '{settings.s3_bucket_name}'. Nothing was touched.")
        return

    if (settings.storage_backend or "local").strip().lower() != "s3":
        logger.error(
            "STORAGE_BACKEND is not 's3' — set it in .env before running this "
            "for real (or pass --dry-run to preview without it)."
        )
        raise SystemExit(1)

    import mimetypes

    from app.services.storage.s3 import S3StorageProvider

    provider = S3StorageProvider()
    client = provider._get_client()  # noqa: SLF001 — this script owns the migration, not the abstraction
    bucket = provider.bucket

    uploaded = 0
    skipped = 0
    failed = 0

    for f in files:
        rel = f.relative_to(media_root).as_posix()
        # The key MUST be the file's existing relative path, verbatim — NOT
        # provider.save()'s path (that appends a fresh random suffix on every
        # call, which would upload each file under a brand new key and leave
        # every existing database row pointing at a path that no longer
        # exists in the bucket, defeating the entire point of this script).
        key = f"{url_prefix}/{rel}"

        if args.skip_existing:
            try:
                client.head_object(Bucket=bucket, Key=key)
                skipped += 1
                continue
            except Exception:  # noqa: BLE001 — any failure here just means "not found, proceed to upload"
                pass

        content_type = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        try:
            client.upload_file(
                Filename=str(f),
                Bucket=bucket,
                Key=key,
                ExtraArgs={
                    "ContentType": content_type,
                    "CacheControl": "public, max-age=31536000",
                },
            )
            uploaded += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.error(f"Failed to upload {key}: {exc}")

    logger.info(
        f"Done — uploaded {uploaded}, skipped {skipped} (already existed), "
        f"failed {failed}, out of {len(files)} total."
    )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
