"""
Admin AI Briefings -- what a branch tells the AI assistant for a while.

  POST   /prepare   — a .txt/.md file or pasted text -> the digest the
                      assistant would read, a title, a suggested mode and
                      warnings. Saves nothing; the manager reviews it first.
  GET    /          — briefings the admin can see, with live status and how
                      often the assistant has mentioned each
  POST   /          — save a reviewed briefing
  PATCH  /{id}      — edit, reschedule, pause or resume
  DELETE /{id}      — remove

Prefix "/admin/assistant-briefings" is applied by app/api/v1/router.py.

Branch isolation: a Branch Manager works on their own branch's briefings
whatever branch_id they send, and sees the network-wide ones read-only so
they know what their customers are being told. Only a Super Admin writes
network-wide briefings (branch_id omitted). See
app/services/assistant_briefings.py for how the assistant uses them.
"""

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from pydantic import AwareDatetime, BaseModel, Field, model_validator
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis
from app.core.dependencies import BranchScope, inject_branch_filter, require_permission
from app.core.rate_limiter import rate_limit
from app.models.assistant import AssistantBriefing
from app.models.branch import Branch
from app.services import assistant_briefings as briefings

router = APIRouter(tags=["Admin AI Briefings"])

Mode = Literal["relevant", "announce"]
DigestSource = Literal["ai", "manager", "fallback"]


def _check_window(starts_at: datetime, ends_at: datetime) -> None:
    if ends_at <= starts_at:
        raise ValueError("The end must be after the start")
    if ends_at - starts_at > briefings.MAX_WINDOW:
        raise ValueError("A briefing can run for at most 90 days")


class BriefingCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=briefings.TITLE_MAX)
    source_text: str = Field(..., min_length=1, max_length=briefings.SOURCE_MAX_CHARS)
    source_filename: Optional[str] = Field(None, max_length=255)
    digest: str = Field(..., min_length=1, max_length=briefings.DIGEST_MAX)
    digest_source: DigestSource = "ai"
    mode: Mode = "relevant"
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    # Super Admin only; ignored for branch-scoped admins. None = every branch.
    branch_id: Optional[UUID] = None

    @model_validator(mode="after")
    def _valid(self) -> "BriefingCreate":
        self.title = self.title.strip()
        self.digest = briefings.clean_text(self.digest)
        self.source_text = briefings.clean_text(self.source_text)
        if not self.title:
            raise ValueError("Give the briefing a title")
        _check_window(self.starts_at, self.ends_at)
        if self.ends_at <= datetime.now(timezone.utc):
            raise ValueError("This window has already ended")
        return self


class BriefingUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=briefings.TITLE_MAX)
    digest: Optional[str] = Field(None, min_length=1, max_length=briefings.DIGEST_MAX)
    mode: Optional[Mode] = None
    starts_at: Optional[AwareDatetime] = None
    ends_at: Optional[AwareDatetime] = None
    is_active: Optional[bool] = None

    @model_validator(mode="after")
    def _valid(self) -> "BriefingUpdate":
        if self.title is not None:
            self.title = self.title.strip()
            if not self.title:
                raise ValueError("Give the briefing a title")
        if self.digest is not None:
            self.digest = briefings.clean_text(self.digest)
        return self


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _visible(scope: BranchScope):
    """Rows an admin may see: all for a Super Admin, else own + network-wide."""
    if scope.is_super_admin:
        return None
    return briefings.network_or_branch(scope.branch_id)


def _may_edit(scope: BranchScope, briefing: AssistantBriefing) -> bool:
    return scope.is_super_admin or (
        briefing.branch_id is not None and briefing.branch_id == scope.branch_id
    )


async def _branch_names(db: AsyncSession, ids) -> dict:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = await db.execute(
        select(Branch.branch_id, Branch.name).where(Branch.branch_id.in_(ids))
    )
    return dict(rows.all())


def _serialize(
    b: AssistantBriefing, scope: BranchScope, names: dict, mentions: dict, now: datetime
) -> dict:
    return {
        "briefing_id": str(b.briefing_id),
        "branch_id": str(b.branch_id) if b.branch_id else None,
        "branch_name": names.get(b.branch_id) if b.branch_id else None,
        "is_network_wide": b.branch_id is None,
        "title": b.title,
        "source_filename": b.source_filename,
        "source_text": b.source_text,
        "digest": b.digest,
        "digest_source": b.digest_source,
        "mode": b.mode,
        "starts_at": b.starts_at.isoformat(),
        "ends_at": b.ends_at.isoformat(),
        "is_active": b.is_active,
        "status": briefings.status_of(b, now),
        "mentions": mentions.get(str(b.briefing_id), 0),
        "can_edit": _may_edit(scope, b),
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
    }


async def _one(
    db: AsyncSession, redis: Redis, scope: BranchScope, b: AssistantBriefing
) -> dict:
    return _serialize(
        b,
        scope,
        await _branch_names(db, [b.branch_id]),
        await briefings.mention_counts(redis, [b.briefing_id]),
        datetime.now(timezone.utc),
    )


async def _ensure_capacity(
    db: AsyncSession,
    branch_id: Optional[UUID],
    starts_at: datetime,
    ends_at: datetime,
    exclude: Optional[UUID] = None,
) -> None:
    """
    Refuse a briefing that would give one moment more briefings than a chat
    turn carries -- the extra ones would silently never be read. Counting
    overlaps with the new window is a safe over-approximation.
    """
    cap = briefings.MAX_LIVE_BRANCH if branch_id else briefings.MAX_LIVE_NETWORK
    scope = (
        AssistantBriefing.branch_id == branch_id
        if branch_id
        else AssistantBriefing.branch_id.is_(None)
    )
    query = select(func.count()).where(
        scope,
        AssistantBriefing.is_active.is_(True),
        AssistantBriefing.starts_at < ends_at,
        AssistantBriefing.ends_at > starts_at,
    )
    if exclude:
        query = query.where(AssistantBriefing.briefing_id != exclude)
    if (await db.execute(query)).scalar_one() >= cap:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"At most {cap} briefings can run at the same time"
                + (" for a branch" if branch_id else " network-wide")
                + ". End or pause one that overlaps this window first."
            ),
        )


@router.post("/prepare", response_model=dict)
@rate_limit(requests_per_minute=10)
async def prepare_briefing(
    request: Request,
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    _admin=Depends(require_permission("briefings", "create")),
    _scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Turn a manager's note into what the assistant will read. One Gemini call;
    nothing is saved. The manager reviews (and may edit) the digest, then
    POSTs it to ``/``.
    """
    if (file is None) == (not text or not text.strip()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload a .txt/.md file or paste the note -- one of the two.",
        )
    try:
        if file is not None:
            data = await file.read(briefings.UPLOAD_MAX_BYTES + 1)
            raw = briefings.decode_upload(data, file.filename)
        else:
            raw = text or ""
        source = briefings.clean_text(raw)
    except briefings.BriefingTextError as exc:
        raise _bad_request(exc)
    draft = await briefings.prepare(source)
    return {
        "success": True,
        "data": {
            **draft,
            "source_text": source,
            "source_filename": file.filename if file is not None else None,
        },
    }


@router.get("", response_model=dict)
async def list_briefings(
    branch_id: Optional[UUID] = Query(None),
    status_filter: Optional[Literal["live", "scheduled", "ended", "paused"]] = Query(
        None, alias="status"
    ),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _admin=Depends(require_permission("briefings", "read")),
    scope: BranchScope = Depends(inject_branch_filter),
):
    query = select(AssistantBriefing).order_by(AssistantBriefing.starts_at.desc())
    visible = _visible(scope)
    if visible is not None:
        query = query.where(visible)
    elif branch_id:
        query = query.where(briefings.network_or_branch(branch_id))
    rows = (await db.execute(query.limit(200))).scalars().all()
    now = datetime.now(timezone.utc)
    if status_filter:
        rows = [b for b in rows if briefings.status_of(b, now) == status_filter]
    names = await _branch_names(db, [b.branch_id for b in rows])
    mentions = await briefings.mention_counts(redis, [b.briefing_id for b in rows])
    return {
        "success": True,
        "data": {
            "items": [_serialize(b, scope, names, mentions, now) for b in rows],
            "scope": {
                "is_super_admin": scope.is_super_admin,
                "branch_id": str(scope.branch_id) if scope.branch_id else None,
            },
            "limits": {
                "digest_max": briefings.DIGEST_MAX,
                "upload_max_bytes": briefings.UPLOAD_MAX_BYTES,
                "max_live_branch": briefings.MAX_LIVE_BRANCH,
                "max_live_network": briefings.MAX_LIVE_NETWORK,
                "max_window_days": briefings.MAX_WINDOW.days,
            },
        },
    }


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_briefing(
    body: BriefingCreate,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=Depends(require_permission("briefings", "create")),
    scope: BranchScope = Depends(inject_branch_filter),
):
    branch_id = body.branch_id if scope.is_super_admin else scope.branch_id
    if not scope.is_super_admin and branch_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has no branch to brief the assistant about.",
        )
    await _ensure_capacity(db, branch_id, body.starts_at, body.ends_at)
    briefing = AssistantBriefing(
        branch_id=branch_id,
        title=body.title,
        source_filename=body.source_filename,
        source_text=body.source_text,
        digest=body.digest,
        digest_source=body.digest_source,
        mode=body.mode,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        is_active=True,
        created_by=admin.admin_id,
        updated_by=admin.admin_id,
    )
    db.add(briefing)
    await db.commit()
    await db.refresh(briefing)
    await briefings.invalidate(redis, branch_id)
    return {"success": True, "data": await _one(db, redis, scope, briefing)}


async def _editable(
    db: AsyncSession, scope: BranchScope, briefing_id: UUID
) -> AssistantBriefing:
    briefing = await db.get(AssistantBriefing, briefing_id)
    visible = briefing is not None and (
        scope.is_super_admin
        or briefing.branch_id is None
        or briefing.branch_id == scope.branch_id
    )
    if not visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Briefing not found")
    if not _may_edit(scope, briefing):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Network-wide briefings are managed by the Super Admin.",
        )
    return briefing


@router.patch("/{briefing_id}", response_model=dict)
async def update_briefing(
    briefing_id: UUID,
    body: BriefingUpdate,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=Depends(require_permission("briefings", "update")),
    scope: BranchScope = Depends(inject_branch_filter),
):
    briefing = await _editable(db, scope, briefing_id)
    starts_at = body.starts_at or briefing.starts_at
    ends_at = body.ends_at or briefing.ends_at
    if body.starts_at or body.ends_at:
        try:
            _check_window(starts_at, ends_at)
        except ValueError as exc:
            raise _bad_request(exc)
    active = briefing.is_active if body.is_active is None else body.is_active
    newly_live = active and not briefing.is_active
    if active and (newly_live or body.starts_at or body.ends_at):
        await _ensure_capacity(
            db, briefing.branch_id, starts_at, ends_at, exclude=briefing.briefing_id
        )
    for field in ("title", "digest", "mode", "starts_at", "ends_at", "is_active"):
        value = getattr(body, field)
        if value is not None:
            setattr(briefing, field, value)
    if body.digest is not None:
        briefing.digest_source = "manager"
    briefing.updated_by = admin.admin_id
    await db.commit()
    await db.refresh(briefing)
    await briefings.invalidate(redis, briefing.branch_id)
    return {"success": True, "data": await _one(db, redis, scope, briefing)}


@router.delete("/{briefing_id}", response_model=dict)
async def delete_briefing(
    briefing_id: UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _admin=Depends(require_permission("briefings", "delete")),
    scope: BranchScope = Depends(inject_branch_filter),
):
    briefing = await _editable(db, scope, briefing_id)
    branch_id = briefing.branch_id
    await db.delete(briefing)
    await db.commit()
    await briefings.invalidate(redis, branch_id)
    return {"success": True, "message": "Briefing removed"}
