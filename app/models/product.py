"""
Product SQLAlchemy Models
"""
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.config.database import Base


class Product(Base):
    """
    Global product catalog.

    Holds the master / "Global Admin" values for every product.  Branch-level
    overrides (price, discount, stock, on-sale flag) live in ``BranchInventory``.
    """

    __tablename__ = "products"

    product_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    slug = Column(String(255), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    short_description = Column(String(500), nullable=True)
    sku = Column(String(100), unique=True, nullable=True)
    price = Column(Numeric(10, 2), nullable=False, comment="Global base price")
    compare_at_price = Column(Numeric(10, 2), nullable=True)
    cost_price = Column(Numeric(10, 2), nullable=True)
    category_id = Column(
        UUID(as_uuid=True),
        ForeignKey("categories.category_id"),
        nullable=True,
        index=True,
        comment="Top-level category",
    )
    subcategory_id = Column(
        UUID(as_uuid=True),
        ForeignKey("categories.category_id"),
        nullable=True,
        index=True,
        comment="Sub-category — its parent_category_id must equal category_id",
    )

    stock_quantity = Column(Integer, default=0, comment="Global default stock")
    low_stock_threshold = Column(Integer, default=10)
    weight = Column(Numeric(10, 3), nullable=True)
    weight_unit = Column(String(10), default="kg")
    is_active = Column(Boolean, default=True)
    is_featured = Column(Boolean, default=False)

    # Global-level marketing defaults (fallback when no branch override exists)
    discount_percentage = Column(
        Float,
        nullable=True,
        default=None,
        comment="Global default discount percentage (0-100)",
    )
    discount_price = Column(
        Numeric(10, 2),
        nullable=True,
        default=None,
        comment="Global default sale price after discount",
    )
    is_on_sale = Column(
        Boolean,
        default=False,
        index=True,
        comment="Global default Quick Sale flag",
    )

    view_count = Column(Integer, default=0)
    meta_title = Column(String(255), nullable=True)
    meta_description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    # `products` has TWO FKs into `categories` (category_id + subcategory_id), so
    # both relationships must name their foreign_keys explicitly — otherwise
    # SQLAlchemy raises AmbiguousForeignKeysError.
    category = relationship(
        "Category",
        foreign_keys=[category_id],
        back_populates="products",
    )
    subcategory = relationship("Category", foreign_keys=[subcategory_id])
    images = relationship("ProductImage", back_populates="product", cascade="all, delete-orphan")
    variants = relationship("ProductVariant", back_populates="product", cascade="all, delete-orphan")
    reviews = relationship("Review", back_populates="product", cascade="all, delete-orphan")
    branch_inventory = relationship(
        "BranchInventory",
        back_populates="product",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Product {self.name}>"


class BranchInventory(Base):
    """
    Branch-specific overrides for a global product.

    Each row says "branch X carries product Y with *these* overrides."
    Any nullable override field that is NULL means "fall back to the
    global value on the ``products`` table."
    """

    __tablename__ = "branch_inventory"

    inventory_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(
        UUID(as_uuid=True),
        ForeignKey("products.product_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    branch_id = Column(
        UUID(as_uuid=True),
        ForeignKey("branches.branch_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    branch_price = Column(
        Numeric(10, 2),
        nullable=True,
        comment="Override price for this branch (NULL → use product.price)",
    )
    stock_quantity = Column(
        Integer,
        default=0,
        comment="Branch-level stock on hand",
    )
    reserved_quantity = Column(
        Integer,
        default=0,
        nullable=False,
        server_default="0",
        comment="Units reserved by pending orders (available = stock - reserved)",
    )
    low_stock_threshold = Column(
        Integer,
        default=10,
        nullable=False,
        server_default="10",
        comment="Branch-level low-stock alert threshold",
    )
    discount_percentage = Column(
        Float,
        nullable=True,
        comment="Branch override discount % (NULL → use product.discount_percentage)",
    )
    is_on_sale = Column(
        Boolean,
        default=False,
        comment="Branch override Quick Sale flag",
    )
    is_active = Column(
        Boolean,
        default=True,
        comment="False = product hidden for this branch even if globally active",
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("product_id", "branch_id", name="uq_branch_inventory_product_branch"),
        Index("idx_branch_inv_branch_sale", "branch_id", "is_on_sale"),
        Index("idx_branch_inv_branch_active", "branch_id", "is_active"),
        Index("idx_branch_inv_branch_sale_discount", "branch_id", "is_on_sale", "discount_percentage"),
    )

    # Relationships
    product = relationship("Product", back_populates="branch_inventory")
    branch = relationship("Branch", backref="branch_inventory")

    def __repr__(self):
        return f"<BranchInventory product={self.product_id} branch={self.branch_id}>"


class ProductImage(Base):
    """Product images model."""

    __tablename__ = "product_images"

    image_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.product_id", ondelete="CASCADE"), nullable=False)
    image_url = Column(String(500), nullable=False)
    alt_text = Column(String(255), nullable=True)
    is_primary = Column(Boolean, default=False)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationship
    product = relationship("Product", back_populates="images")

    def __repr__(self):
        return f"<ProductImage {self.image_id}>"


class ProductVariant(Base):
    """Product variants model (size, color, etc.)."""

    __tablename__ = "product_variants"

    variant_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.product_id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)
    sku = Column(String(100), unique=True, nullable=True)
    price = Column(Numeric(10, 2), nullable=False)
    compare_at_price = Column(Numeric(10, 2), nullable=True)
    stock_quantity = Column(Integer, default=0)
    image_url = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationship
    product = relationship("Product", back_populates="variants")
    variant_options = relationship("VariantOption", back_populates="variant", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ProductVariant {self.name}>"


class VariantType(Base):
    """Variant types (e.g., Size, Color)."""

    __tablename__ = "variant_types"

    variant_type_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(50), nullable=False)
    display_name = Column(String(50), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationship
    options = relationship("VariantOption", back_populates="variant_type")

    def __repr__(self):
        return f"<VariantType {self.name}>"


class VariantOption(Base):
    """Variant options linking variants to types."""

    __tablename__ = "variant_options"

    option_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    variant_id = Column(UUID(as_uuid=True), ForeignKey("product_variants.variant_id", ondelete="CASCADE"), nullable=False)
    variant_type_id = Column(UUID(as_uuid=True), ForeignKey("variant_types.variant_type_id"), nullable=False)
    value = Column(String(100), nullable=False)

    # Relationships
    variant = relationship("ProductVariant", back_populates="variant_options")
    variant_type = relationship("VariantType", back_populates="options")

    def __repr__(self):
        return f"<VariantOption {self.value}>"


class Review(Base):
    """Product reviews model."""

    __tablename__ = "reviews"

    review_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.product_id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    rating = Column(Integer, nullable=False)
    title = Column(String(255), nullable=True)
    comment = Column(Text, nullable=True)
    is_verified_purchase = Column(Boolean, default=False)
    is_approved = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    product = relationship("Product", back_populates="reviews")
    user = relationship("User", back_populates="reviews")

    # Enforce one review per (product, user) at the database level.
    __table_args__ = (
        UniqueConstraint("product_id", "user_id", name="uq_product_user_review"),
    )

    def __repr__(self):
        return f"<Review {self.review_id}>"
