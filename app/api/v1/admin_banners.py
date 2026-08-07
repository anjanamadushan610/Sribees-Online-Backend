"""
Admin Banner Management API Endpoints

CRUD for the mobile home-screen promotional carousel.

Branch isolation is the point of this module: a Branch Manager curates the
banners their own customers see, and can neither read nor touch another
branch's. Super Admins additionally manage *platform-wide* banners
(``branch_id IS NULL``) that every branch displays.

  GET    /admin/banners              — list (own branch + platform-wide)
  POST   /admin/banners              — create
  POST   /admin/banners/upload-image — upload the slide artwork
  PUT    /admin/banners/{banner_id}  — update
  DELETE /admin/banners/{banner_id}  — delete

Prefix "/admin/banners" is applied by app/api/v1/router.py.
"""
import io
from typing import Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.dependencies import BranchScope, inject_branch_filter, require_roles
from app.core.media import media_url
from app.models.banner import Banner
from app.schemas.banner import BannerCreate, BannerUpdate
from app.services.storage import StorageError, get_storage

# Banner curation is a marketing/branch-management job.
router = APIRouter(
    dependencies=[
        Depends(require_roles("super_admin", "branch_manager", "marketing_manager"))
    ],
    tags=["Admin Banners"],
)

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


# ============================================================================
# Helpers
# ============================================================================

def _format_banner(banner: Banner) -> dict:
    """Serialize a Banner row; image_url is resolved to a fetchable URL here."""
    return {
        "banner_id": str(banner.banner_id),
        "title": banner.title,
        "subtitle": banner.subtitle,
        "image_url": media_url(banner.image_url),
        "link_type": banner.link_type,
        "target_id": str(banner.target_id) if banner.target_id else None,
        "branch_id": str(banner.branch_id) if banner.branch_id else None,
        "is_platform_wide": banner.branch_id is None,
        "sort_order": banner.sort_order,
        "is_active": banner.is_active,
        "created_at": banner.created_at.isoformat() if banner.created_at else None,
    }


async def _get_owned_banner(
    db: AsyncSession, banner_id: UUID, scope: BranchScope
) -> Banner:
    """
    Load a banner the caller is allowed to modify, or raise.

    A scoped admin may only touch their own branch's rows — notably *not* the
    platform-wide ones, which belong to the Super Admin who set them.
    """
    result = await db.execute(select(Banner).where(Banner.banner_id == banner_id))
    banner = result.scalar_one_or_none()

    if banner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Banner not found",
        )

    if not scope.is_super_admin and banner.branch_id != scope.branch_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You can only manage banners for your own branch. "
                "Platform-wide banners are managed by a Super Admin."
            ),
        )

    return banner


# ============================================================================
# GET / — list
# ============================================================================

@router.get("", response_model=dict)
async def list_banners(
    branch_id: Optional[UUID] = Query(
        None, description="Super Admin only: narrow to a single branch"
    ),
    include_inactive: bool = Query(True, description="Include deactivated banners"),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    List banners visible to the caller.

    Scoped admins get their own branch's banners plus the platform-wide ones
    (read-only for them) — which is exactly the set their customers see.
    """
    query = select(Banner)

    if scope.is_super_admin:
        if branch_id is not None:
            query = query.where(
                or_(Banner.branch_id == branch_id, Banner.branch_id.is_(None))
            )
    else:
        query = query.where(
            or_(Banner.branch_id == scope.branch_id, Banner.branch_id.is_(None))
        )

    if not include_inactive:
        query = query.where(Banner.is_active.is_(True))

    query = query.order_by(Banner.sort_order, Banner.created_at)
    banners = (await db.execute(query)).scalars().all()

    return {
        "success": True,
        "data": {
            "banners": [_format_banner(b) for b in banners],
            "scope": {
                "is_super_admin": scope.is_super_admin,
                "branch_id": str(scope.branch_id) if scope.branch_id else None,
            },
        },
    }


# ============================================================================
# POST /upload-image
# ============================================================================

@router.post(
    "/upload-image",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
)
async def upload_banner_image(
    file: UploadFile = File(..., description="Image file (JPEG/PNG/WebP/GIF)"),
):
    """
    Upload banner artwork and return its URL.

    Declared before ``/{banner_id}`` so that path parameter does not swallow
    "upload-image". The returned URL is passed back as ``image_url`` on create
    or update, which lets the admin upload before the banner exists.
    """
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid file type '{file.content_type}'. "
                f"Allowed: {', '.join(sorted(ALLOWED_IMAGE_TYPES))}"
            ),
        )

    max_mb = settings.media_max_upload_size_mb
    contents = await file.read()
    if len(contents) > max_mb * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"File too large ({len(contents) / 1024 / 1024:.1f} MB). "
                f"Maximum allowed: {max_mb} MB."
            ),
        )

    try:
        path = get_storage().save(
            fileobj=io.BytesIO(contents),
            filename=file.filename or "banner.jpg",
            folder="banners",
            content_type=file.content_type,
        )
    except StorageError as exc:
        logger.error(f"Banner image upload failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not store the image on the server. Please try again.",
        )

    return {
        "success": True,
        "data": {"image_url": media_url(path), "filename": file.filename},
        "message": "Image uploaded successfully",
    }


# ============================================================================
# POST / — create
# ============================================================================

@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_banner(
    body: BannerCreate,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Create a banner.

    A scoped admin's banner is always pinned to their own branch — any
    ``branch_id`` in the body is ignored, so they cannot publish into someone
    else's storefront. A Super Admin omitting ``branch_id`` creates a
    platform-wide banner.
    """
    branch_id = body.branch_id if scope.is_super_admin else scope.branch_id

    banner = Banner(
        title=body.title,
        subtitle=body.subtitle,
        image_url=body.image_url,
        link_type=body.link_type,
        target_id=body.target_id,
        branch_id=branch_id,
        sort_order=body.sort_order,
        is_active=body.is_active,
    )
    db.add(banner)
    await db.commit()
    await db.refresh(banner)

    logger.info(
        f"[admin] Banner '{banner.title}' created "
        f"({'platform-wide' if branch_id is None else f'branch {branch_id}'})"
    )
    return {
        "success": True,
        "data": _format_banner(banner),
        "message": "Banner created successfully",
    }


# ============================================================================
# PUT /{banner_id} — update
# ============================================================================

@router.put("/{banner_id}", response_model=dict)
async def update_banner(
    banner_id: UUID,
    body: BannerUpdate,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Update a banner the caller owns."""
    banner = await _get_owned_banner(db, banner_id, scope)

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(banner, field, value)

    await db.commit()
    await db.refresh(banner)

    logger.info(f"[admin] Banner {banner_id} updated")
    return {
        "success": True,
        "data": _format_banner(banner),
        "message": "Banner updated successfully",
    }


# ============================================================================
# DELETE /{banner_id}
# ============================================================================

@router.delete("/{banner_id}", response_model=dict)
async def delete_banner(
    banner_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Delete a banner the caller owns."""
    banner = await _get_owned_banner(db, banner_id, scope)
    title = banner.title

    await db.delete(banner)
    await db.commit()

    logger.info(f"[admin] Banner {banner_id} ('{title}') deleted")
    return {
        "success": True,
        "message": f"Banner '{title}' deleted",
    }
