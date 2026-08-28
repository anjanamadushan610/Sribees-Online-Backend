"""
Category Service - Business Logic
"""
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import Branch
from app.models.category import BranchCategory, Category
from app.schemas.category import CategoryCreate, CategoryUpdate
from app.utils.i18n import DEFAULT_LANGUAGE, localized


class CategoryService:
    """Service class for category operations."""

    @staticmethod
    async def get_all(
        db: AsyncSession,
        include_inactive: bool = False,
        branch_id: Optional[UUID] = None,
    ) -> List[dict]:
        """
        Get all categories with product counts.

        *branch_id*, when given (and *include_inactive* is False), additionally
        requires an active ``branch_categories`` row for that branch — a
        category hidden from a branch (or not yet activated there) is excluded
        from that branch's customers, mirroring branch_inventory for products.
        Ignored when *include_inactive* is True (the admin "show me
        everything" view has no branch context).
        """
        query = (
            select(
                Category,
                func.count(func.distinct(Category.products)).label("product_count")
            )
            .outerjoin(Category.products)
            .group_by(Category.category_id)
            .order_by(Category.name)
        )

        if not include_inactive:
            query = query.where(Category.is_active == True)
            if branch_id is not None:
                query = query.join(
                    BranchCategory,
                    and_(
                        BranchCategory.category_id == Category.category_id,
                        BranchCategory.branch_id == branch_id,
                    ),
                ).where(BranchCategory.is_active.is_(True))

        result = await db.execute(query)
        categories = result.all()

        return [
            {
                **cat[0].__dict__,
                "product_count": cat[1] or 0
            }
            for cat in categories
        ]

    @staticmethod
    async def get_all_simple(
        db: AsyncSession,
        include_inactive: bool = False,
        branch_id: Optional[UUID] = None,
    ) -> List[Category]:
        """Get all categories without product counts (simpler query)."""
        query = select(Category).order_by(Category.name)

        if not include_inactive:
            query = query.where(Category.is_active == True)
            if branch_id is not None:
                query = query.join(
                    BranchCategory,
                    and_(
                        BranchCategory.category_id == Category.category_id,
                        BranchCategory.branch_id == branch_id,
                    ),
                ).where(BranchCategory.is_active.is_(True))

        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def get_hierarchical(
        db: AsyncSession,
        lang: str = DEFAULT_LANGUAGE,
        branch_id: Optional[UUID] = None,
    ) -> List[dict]:
        """
        Get categories in hierarchical structure (parent-child).

        *lang* selects the language for the customer-visible name/description,
        falling back to English per field where no translation exists.
        *branch_id* narrows to that branch's active categories — see
        ``get_all``.
        """
        categories = await CategoryService.get_all_simple(db, branch_id=branch_id)

        # Build hierarchy
        category_map = {}
        root_categories = []

        # First pass: create map with children array
        for cat in categories:
            category_map[str(cat.category_id)] = {
                "category_id": cat.category_id,
                "_id": str(cat.category_id),
                "name": localized(cat, "name", lang),
                "slug": cat.slug,
                "description": localized(cat, "description", lang),
                "image_url": cat.image_url,
                "parent_category_id": cat.parent_category_id,
                "is_active": cat.is_active,
                "product_count": 0,
                "children": []
            }

        # Second pass: build tree
        for cat in categories:
            cat_dict = category_map[str(cat.category_id)]
            if cat.parent_category_id and str(cat.parent_category_id) in category_map:
                parent = category_map[str(cat.parent_category_id)]
                parent["children"].append(cat_dict)
            else:
                root_categories.append(cat_dict)

        return root_categories

    @staticmethod
    async def get_by_id(db: AsyncSession, category_id: UUID) -> Optional[Category]:
        """Get category by ID."""
        result = await db.execute(
            select(Category).where(Category.category_id == category_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_slug(db: AsyncSession, slug: str) -> Optional[Category]:
        """Get category by slug."""
        result = await db.execute(
            select(Category).where(Category.slug == slug)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id_or_slug(
        db: AsyncSession,
        identifier: str
    ) -> Optional[Category]:
        """Get category by ID or slug."""
        # Try UUID first
        try:
            category_uuid = UUID(identifier)
            category = await CategoryService.get_by_id(db, category_uuid)
            if category:
                return category
        except ValueError:
            pass

        # Try slug
        return await CategoryService.get_by_slug(db, identifier)

    @staticmethod
    async def create(
        db: AsyncSession,
        data: CategoryCreate
    ) -> Category:
        """
        Create a new category.

        Also seeds a hidden (``is_active=False``) ``branch_categories`` row for
        every existing branch — the category exists platform-wide immediately,
        but each branch's own manager decides when it goes live there.
        """
        category = Category(
            name=data.name,
            name_si=data.name_si,
            name_ta=data.name_ta,
            slug=data.slug,
            description=data.description,
            description_si=data.description_si,
            description_ta=data.description_ta,
            image_url=data.image_url,
            parent_category_id=data.parent_category_id,
            is_active=data.is_active
        )

        db.add(category)
        # Flush (not commit) so the category has a PK before branch rows
        # referencing it are written, then commit once so they move together.
        await db.flush()
        await CategoryService._seed_branch_rows(db, category.category_id)
        await db.commit()
        await db.refresh(category)

        return category

    @staticmethod
    async def _seed_branch_rows(db: AsyncSession, category_id: UUID) -> None:
        """Create a hidden branch_categories row for *category_id* in every branch."""
        branch_ids = (await db.execute(select(Branch.branch_id))).scalars().all()
        if branch_ids:
            db.add_all([
                BranchCategory(category_id=category_id, branch_id=bid, is_active=False)
                for bid in branch_ids
            ])

    @staticmethod
    async def seed_for_new_branch(db: AsyncSession, branch_id: UUID) -> None:
        """
        Create a hidden branch_categories row for *branch_id* against every
        existing category — the counterpart to ``_seed_branch_rows``, called
        when a new branch is created so the (category, branch) matrix stays
        fully populated in both directions.
        """
        category_ids = (await db.execute(select(Category.category_id))).scalars().all()
        if category_ids:
            db.add_all([
                BranchCategory(category_id=cid, branch_id=branch_id, is_active=False)
                for cid in category_ids
            ])

    @staticmethod
    async def get_branch_category(
        db: AsyncSession,
        category_id: UUID,
        branch_id: UUID,
    ) -> Optional[BranchCategory]:
        """Fetch the branch_categories row for a (category, branch) pair."""
        result = await db.execute(
            select(BranchCategory).where(
                BranchCategory.category_id == category_id,
                BranchCategory.branch_id == branch_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_for_branch(
        db: AsyncSession,
        branch_id: UUID,
    ) -> List[Tuple[Category, bool]]:
        """
        Every category paired with its ``is_active`` status for *branch_id* —
        including categories globally inactive or not yet activated here, so
        a Branch Manager can see (and turn on) everything, not just what
        already shows to customers.
        """
        result = await db.execute(
            select(Category, BranchCategory.is_active)
            .outerjoin(
                BranchCategory,
                and_(
                    BranchCategory.category_id == Category.category_id,
                    BranchCategory.branch_id == branch_id,
                ),
            )
            .order_by(Category.name)
        )
        # A missing row (category created before this branch existed, in the
        # narrow window before seeding runs) reads as hidden, not active.
        return [(cat, bool(active)) for cat, active in result.all()]

    @staticmethod
    async def set_branch_status(
        db: AsyncSession,
        category_id: UUID,
        branch_id: UUID,
        is_active: bool,
    ) -> BranchCategory:
        """Activate/deactivate a category for one branch (upsert)."""
        row = await CategoryService.get_branch_category(db, category_id, branch_id)
        if row is None:
            row = BranchCategory(category_id=category_id, branch_id=branch_id)
            db.add(row)
        row.is_active = is_active
        await db.commit()
        await db.refresh(row)
        return row

    @staticmethod
    async def update(
        db: AsyncSession,
        category: Category,
        data: CategoryUpdate
    ) -> Category:
        """Update an existing category."""
        update_data = data.model_dump(exclude_unset=True)

        for field, value in update_data.items():
            setattr(category, field, value)

        await db.commit()
        await db.refresh(category)

        return category

    @staticmethod
    async def delete(db: AsyncSession, category: Category) -> None:
        """Delete a category."""
        await db.delete(category)
        await db.commit()

    @staticmethod
    async def has_products(db: AsyncSession, category_id: UUID) -> bool:
        """Check if any product is assigned to this category, at either level."""
        # Import here to avoid circular imports
        from app.models.product import Product

        result = await db.execute(
            select(func.count(Product.product_id))
            .where(
                or_(
                    Product.category_id == category_id,
                    Product.subcategory_id == category_id,
                )
            )
        )
        count = result.scalar()
        return count > 0

    @staticmethod
    async def has_children(db: AsyncSession, category_id: UUID) -> bool:
        """Check if the category has sub-categories hanging off it."""
        result = await db.execute(
            select(func.count(Category.category_id))
            .where(Category.parent_category_id == category_id)
        )
        return (result.scalar() or 0) > 0

    @staticmethod
    async def get_children(
        db: AsyncSession,
        parent_id: UUID,
        include_inactive: bool = False,
    ) -> List[Category]:
        """Get the sub-categories of *parent_id* (one level down)."""
        query = (
            select(Category)
            .where(Category.parent_category_id == parent_id)
            .order_by(Category.name)
        )
        if not include_inactive:
            query = query.where(Category.is_active.is_(True))

        result = await db.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def get_branch_ids(db: AsyncSession, category_id: UUID) -> List[UUID]:
        """
        Return *category_id* plus the ids of its sub-categories.

        Browsing a top-level category must surface every product filed under
        any of its sub-categories, not just the ones pinned directly to the
        parent — so listing filters resolve the category to this id set.
        """
        children = await db.execute(
            select(Category.category_id)
            .where(Category.parent_category_id == category_id)
        )
        return [category_id, *children.scalars().all()]

    @staticmethod
    async def validate_subcategory(
        db: AsyncSession,
        category_id: Optional[UUID],
        subcategory_id: Optional[UUID],
    ) -> None:
        """
        Enforce the sub-category contract before a product is written.

        A sub-category is only meaningful underneath its own parent, so:
          * a sub-category requires a category,
          * the sub-category row must exist, and
          * its ``parent_category_id`` must be exactly ``category_id``.

        Raises ``ValueError`` (callers map this to a 400) on violation.
        """
        if subcategory_id is None:
            return

        if category_id is None:
            raise ValueError("A sub-category requires a parent category.")

        if subcategory_id == category_id:
            raise ValueError("A category cannot be its own sub-category.")

        subcategory = await CategoryService.get_by_id(db, subcategory_id)
        if subcategory is None:
            raise ValueError("Sub-category not found.")

        if subcategory.parent_category_id != category_id:
            raise ValueError(
                "Sub-category does not belong to the selected category."
            )
