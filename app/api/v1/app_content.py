"""
SRIBEESonline - GET /app/content/{page}: an app page's words, from the database.

The app draws the page from this document, so the owner can change the text
(or hide the page) with a query, no release needed -- see
migrations/074_app_content.sql for examples. Guests see these pages too.

Prefix "/app" applied by app/api/v1/router.py.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.services import app_content_service

router = APIRouter()


@router.get("/content/{page}", response_model=dict, summary="An app page's text")
async def get_app_content(
    page: str = Path(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$"),
    lang: Optional[str] = Query(None, max_length=10, description="en, si or ta; English when missing"),
    db: AsyncSession = Depends(get_db),
):
    row = await app_content_service.get_page(db, page, lang)
    if row is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return {
        "success": True,
        "data": {
            "page": row.page,
            "lang": row.lang,
            "content": row.content,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        },
    }
