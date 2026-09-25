"""
Admin Push Campaigns — marketing notifications to customers.

  GET   /audience            — who a campaign would reach (customers, devices, languages)
  GET   /                    — campaigns, newest first, with delivery and open counts
  POST  /                    — compose and send now, or schedule
  POST  /{id}/cancel         — cancel one that has not started
  POST  /upload-image        — the picture shown in the expanded notification

Prefix "/admin/notification-campaigns" is applied by app/api/v1/router.py.

Branch isolation is the customer directory's: a Marketing Manager's campaign
goes to the customers their branch has served, whatever branch_id they send.
Only a Super Admin can address every customer (branch_id omitted).
"""
import io
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from loguru import logger
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.dependencies import BranchScope, inject_branch_filter, require_permission
from app.core.media import media_url
from app.models.admin import Admin
from app.models.branch import Branch
from app.models.notification import CampaignStatus, NotificationCampaign
from app.models.product import Product
from app.services import campaign_service
from app.services.notification_service import _spawn
from app.services.storage import StorageError, get_storage

router = APIRouter(tags=["Admin Push Campaigns"])

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
# Android shows about this much of a title and a body before it cuts them.
TITLE_MAX = 65
MESSAGE_MAX = 240
MAX_SCHEDULE_AHEAD = timedelta(days=30)

Category = Literal["offers", "promotions", "general"]


class LocalizedText(BaseModel):
    """English is required; Sinhala and Tamil fall back to it when blank."""
    en: str = Field(..., min_length=1)
    si: Optional[str] = None
    ta: Optional[str] = None

    @model_validator(mode="after")
    def _strip(self) -> "LocalizedText":
        self.en = self.en.strip()
        self.si = (self.si or "").strip() or None
        self.ta = (self.ta or "").strip() or None
        if not self.en:
            raise ValueError("English text is required")
        return self


class CampaignCreate(BaseModel):
    category: Category
    title: LocalizedText
    message: LocalizedText
    image_url: Optional[str] = Field(None, max_length=500)
    link_type: Optional[Literal["product", "offers"]] = None
    link_value: Optional[str] = Field(None, max_length=100)
    # Super Admin only; ignored for branch-scoped admins. None = every customer.
    branch_id: Optional[UUID] = None
    # None = now.
    scheduled_at: Optional[datetime] = None

    @field_validator("title")
    @classmethod
    def _title_length(cls, v: LocalizedText) -> LocalizedText:
        for text in (v.en, v.si, v.ta):
            if text and len(text) > TITLE_MAX:
                raise ValueError(f"Titles can be at most {TITLE_MAX} characters")
        return v

    @field_validator("message")
    @classmethod
    def _message_length(cls, v: LocalizedText) -> LocalizedText:
        for text in (v.en, v.si, v.ta):
            if text and len(text) > MESSAGE_MAX:
                raise ValueError(f"Messages can be at most {MESSAGE_MAX} characters")
        return v

    @model_validator(mode="after")
    def _link(self) -> "CampaignCreate":
        if self.link_type == "product":
            try:
                UUID(str(self.link_value))
            except ValueError:
                raise ValueError("Choose the product the notification opens")
        else:
            self.link_value = None
        if self.image_url is not None:
            self.image_url = self.image_url.strip() or None
        return self


def _audience_branch(scope: BranchScope, requested: Optional[UUID]) -> Optional[UUID]:
    """The branch a campaign is for. Scoped admins get their own, always."""
    if scope.is_super_admin:
        return requested
    if scope.branch_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has no branch, so there is nobody to send to.",
        )
    return scope.branch_id


def _admin_branch_scope(scope: BranchScope) -> Optional[UUID]:
    """Which campaigns an admin may see: all for a Super Admin, else their branch's."""
    return None if scope.is_super_admin else scope.branch_id


async def _branch_names(db: AsyncSession, ids) -> dict:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = (await db.execute(select(Branch.branch_id, Branch.name).where(Branch.branch_id.in_(ids)))).all()
    return dict(rows)


@router.get("/audience", response_model=dict)
async def audience(
    category: Category = Query(...),
    branch_id: Optional[UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(require_permission("marketing", "read")),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Customers and devices a campaign would reach, split by app language."""
    target = _audience_branch(scope, branch_id)
    data = await campaign_service.estimate(db, category, target)
    data["branch_id"] = str(target) if target else None
    return {"success": True, "data": data}


@router.get("", response_model=dict)
async def list_campaigns(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(require_permission("marketing", "read")),
    scope: BranchScope = Depends(inject_branch_filter),
):
    query = select(NotificationCampaign)
    visible = _admin_branch_scope(scope)
    if visible is not None:
        query = query.where(NotificationCampaign.branch_id == visible)

    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = (
        await db.execute(
            query.order_by(NotificationCampaign.created_at.desc())
            .limit(limit)
            .offset((page - 1) * limit)
        )
    ).scalars().all()
    names = await _branch_names(db, [r.branch_id for r in rows])
    return {
        "success": True,
        "data": {
            "campaigns": [campaign_service.serialize(r, names.get(r.branch_id)) for r in rows],
            "total": total,
            "page": page,
            "limit": limit,
            "scope": {
                "is_super_admin": scope.is_super_admin,
                "branch_id": str(scope.branch_id) if scope.branch_id else None,
            },
        },
    }


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignCreate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(require_permission("marketing", "create")),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Save a campaign for the worker to send. "Now" is sent within seconds;
    a time in the future waits for it. Nothing is sent inside this request.
    """
    branch_id = _audience_branch(scope, body.branch_id)
    now = datetime.now(timezone.utc)

    when = body.scheduled_at
    if when is not None and when.tzinfo is None:
        raise HTTPException(status_code=422, detail="scheduled_at must include a timezone")
    if when is None or when <= now:
        when = now
    elif when - now > MAX_SCHEDULE_AHEAD:
        raise HTTPException(status_code=422, detail="Schedule at most 30 days ahead")

    if body.link_type == "product":
        exists = await db.scalar(
            select(func.count(Product.product_id)).where(Product.product_id == UUID(body.link_value))
        )
        if not exists:
            raise HTTPException(status_code=422, detail="That product no longer exists")

    if branch_id is not None:
        if not await db.get(Branch, branch_id):
            raise HTTPException(status_code=422, detail="Unknown branch")

    campaign = NotificationCampaign(
        category=body.category,
        title=body.title.model_dump(exclude_none=True),
        message=body.message.model_dump(exclude_none=True),
        image_url=body.image_url,
        link_type=body.link_type,
        link_value=body.link_value,
        branch_id=branch_id,
        status=CampaignStatus.SCHEDULED.value,
        scheduled_at=when,
        created_by=admin.admin_id,
        created_by_name=admin.full_name or admin.email,
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    logger.info(
        f"[admin] Campaign {campaign.campaign_id} ({body.category}) by {admin.email} "
        f"for {'branch ' + str(branch_id) if branch_id else 'every customer'} at {when.isoformat()}"
    )

    if when <= now:
        # Don't make "send now" wait for the next poll.
        _spawn(campaign_service.run_due())

    names = await _branch_names(db, [branch_id])
    return {
        "success": True,
        "data": campaign_service.serialize(campaign, names.get(branch_id)),
        "message": "Sending now" if when <= now else "Scheduled",
    }


@router.post("/{campaign_id}/cancel", response_model=dict)
async def cancel_campaign(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(require_permission("marketing", "update")),
    scope: BranchScope = Depends(inject_branch_filter),
):
    campaign = await db.get(NotificationCampaign, campaign_id, with_for_update=True)
    visible = _admin_branch_scope(scope)
    if campaign is None or (visible is not None and campaign.branch_id != visible):
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status != CampaignStatus.SCHEDULED.value:
        raise HTTPException(
            status_code=409, detail=f"Only a scheduled campaign can be cancelled; this one is {campaign.status}"
        )
    campaign.status = CampaignStatus.CANCELLED.value
    campaign.cancelled_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(campaign)
    return {"success": True, "data": campaign_service.serialize(campaign)}


@router.post(
    "/upload-image",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("marketing", "create"))],
)
async def upload_campaign_image(
    file: UploadFile = File(..., description="Image file (JPEG/PNG/WebP)"),
):
    """Store the notification picture; the URL goes back as ``image_url``."""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use a JPEG, PNG or WebP image.",
        )
    # FCM refuses images over 1 MB for Android notifications.
    max_bytes = min(settings.media_max_upload_size_mb, 1) * 1024 * 1024
    contents = await file.read()
    if len(contents) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Notification images must be under 1 MB.",
        )
    try:
        path = get_storage().save(
            fileobj=io.BytesIO(contents),
            filename=file.filename or "campaign.jpg",
            folder="campaigns",
            content_type=file.content_type,
        )
    except StorageError as exc:
        logger.error(f"Campaign image upload failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not store the image on the server. Please try again.",
        )
    return {"success": True, "data": {"image_url": media_url(path), "path": path}}
