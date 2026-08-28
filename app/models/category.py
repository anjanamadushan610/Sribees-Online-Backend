"""
Category SQLAlchemy Models
"""
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.config.database import Base


class Category(Base):
    """Category model for product categorization."""

    __tablename__ = "categories"

    category_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # `name`/`description` hold the ENGLISH copy and double as the fallback
    # whenever the requested locale has no translation (see app.utils.i18n).
    name = Column(String(100), nullable=False)
    name_si = Column(String(100), nullable=True)
    name_ta = Column(String(100), nullable=True)
    slug = Column(String(100), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    description_si = Column(Text, nullable=True)
    description_ta = Column(Text, nullable=True)
    image_url = Column(String(500), nullable=True)
    parent_category_id = Column(UUID(as_uuid=True), ForeignKey("categories.category_id"), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Self-referential relationship for parent-child categories
    parent = relationship("Category", remote_side=[category_id], backref="children")

    # Relationship to products. `products` has two FKs into `categories`
    # (category_id + subcategory_id), so this must pin the one it pairs with.
    products = relationship(
        "Product",
        back_populates="category",
        foreign_keys="Product.category_id",
        lazy="dynamic",
    )

    # Products whose *sub*-category is this row (leaf-level assignment).
    subcategory_products = relationship(
        "Product",
        foreign_keys="Product.subcategory_id",
        lazy="dynamic",
        viewonly=True,
    )

    def __repr__(self):
        return f"<Category {self.name}>"


class BranchCategory(Base):
    """
    Per-branch visibility toggle for a category.

    Mirrors ``BranchInventory`` on the product side: every (category, branch)
    pair gets a row, seeded eagerly whenever either side is created (see
    ``admin_catalog.create_category`` / ``admin_branches.create_branch``).
    New rows default to hidden — a brand new category or a brand new branch
    starts dark until that branch's own manager turns it on. Existing pairs at
    the time this table was introduced were backfilled active (migration 040)
    so the live storefront did not go dark retroactively.
    """

    __tablename__ = "branch_categories"

    branch_category_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category_id = Column(
        UUID(as_uuid=True),
        ForeignKey("categories.category_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    branch_id = Column(
        UUID(as_uuid=True),
        ForeignKey("branches.branch_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    is_active = Column(
        Boolean,
        default=False,
        nullable=False,
        server_default="false",
        comment="False = category hidden for this branch's customers, even if globally active",
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("category_id", "branch_id", name="uq_branch_category_category_branch"),
        Index("idx_branch_category_branch_active", "branch_id", "is_active"),
    )

    category = relationship("Category", backref="branch_links")
    branch = relationship("Branch", backref="category_links")

    def __repr__(self):
        return f"<BranchCategory category={self.category_id} branch={self.branch_id} active={self.is_active}>"
