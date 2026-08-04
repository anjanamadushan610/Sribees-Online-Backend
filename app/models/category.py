"""
Category SQLAlchemy Models
"""
import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.config.database import Base


class Category(Base):
    """Category model for product categorization."""

    __tablename__ = "categories"

    category_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)
    description = Column(Text, nullable=True)
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
