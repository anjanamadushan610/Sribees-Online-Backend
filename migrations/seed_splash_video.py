"""
Seed Script: Upload the initial splash video to S3 and save the URL.

Usage (from fastapi_backend/):
    python -m migrations.seed_splash_video

Requirements:
    - AWS credentials configured (env vars or .env file)
    - The local video file exists at the path below
    - PostgreSQL database is reachable
    - The app_settings table has been created (run migration 014 first)

This script is idempotent: if a splash_video_url setting already exists
with a non-null value, it will skip the upload.
"""
import asyncio
import os
import sys

# Ensure the project root is on sys.path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config.settings import settings  # noqa: E402

# Path to the local video file
LOCAL_VIDEO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "Grocery_App_Animation_Video_Creation.mp4",
)


async def seed():
    from app.config.database import async_session_factory, engine
    from app.services.app_settings_service import AppSettingsService
    from app.services.storage_service import StorageService

    print("=" * 60)
    print("Splash Video Seed Script")
    print("=" * 60)

    # Verify local file exists
    if not os.path.isfile(LOCAL_VIDEO_PATH):
        print(f"[ERROR] Video file not found at: {LOCAL_VIDEO_PATH}")
        print("        Please check the file path and try again.")
        return

    file_size_mb = os.path.getsize(LOCAL_VIDEO_PATH) / (1024 * 1024)
    print(f"[INFO]  Local file: {LOCAL_VIDEO_PATH}")
    print(f"[INFO]  File size:  {file_size_mb:.2f} MB")

    async with async_session_factory() as db:
        # Check if already seeded
        existing = await AppSettingsService.get_splash_video(db)
        if existing and existing.value:
            print(f"[SKIP]  Splash video already configured: {existing.value}")
            print("        Delete the setting or set value to NULL to re-seed.")
            return

        # Upload to S3
        print(f"[INFO]  Uploading to S3 bucket: {settings.s3_bucket_name} ...")
        try:
            url = StorageService.upload_local_file(
                local_path=LOCAL_VIDEO_PATH,
                folder="splash",
                content_type="video/mp4",
            )
        except Exception as exc:
            print(f"[ERROR] S3 upload failed: {exc}")
            print("        Make sure AWS credentials are configured in .env:")
            print("          AWS_ACCESS_KEY_ID=...")
            print("          AWS_SECRET_ACCESS_KEY=...")
            print("          S3_BUCKET_NAME=...")
            return

        print(f"[OK]    Uploaded to: {url}")

        # Save URL in database
        setting = await AppSettingsService.set_splash_video_url(db, url, is_active=True)
        print(f"[OK]    Saved to app_settings: id={setting.setting_id}")

    await engine.dispose()
    print("=" * 60)
    print("Seed complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(seed())
