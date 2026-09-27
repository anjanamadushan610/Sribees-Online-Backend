"""
AppContent SQLAlchemy Model

Words the app shows that the owner changes without an app release: one row
per page per language, the page's text as a JSON document. The first page is
"developer" (Profile -> About the developer).

Edited with plain SQL (see migrations/074_app_content.sql for examples); a
trigger stamps updated_at, and app_content_service notices the change and
tells open apps to re-read.
"""
from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB

from app.config.database import Base


class AppContent(Base):
    __tablename__ = "app_content"
    __table_args__ = (
        CheckConstraint("lang IN ('en', 'si', 'ta')", name="ck_app_content_lang"),
    )

    page = Column(String(64), primary_key=True, comment="Page key, e.g. developer")
    lang = Column(String(8), primary_key=True, comment="en, si or ta")
    content = Column(JSONB, nullable=False, comment="The page's text in this language")
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
