"""
Product Service - Business Logic

Implements the **Global Catalog + Branch Overrides** pattern:
  - ``Product`` holds global / Super-Admin defaults.
  - ``BranchInventory`` holds per-branch overrides.
  - Effective values are resolved via COALESCE:
        effective_price    = branch_price    ?? product.price
        effective_discount = branch_discount ?? product.discount_percentage
  - A ``BranchInventory.is_active = False`` row hides the product for that branch.
"""
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.branch import Branch
from app.models.product import (
    BranchInventory,
    Product,
    ProductImage,
    ProductVariant,
    VariantOption,
)
from app.schemas.product import (
    ProductCreate,
    ProductImageCreate,
    ProductUpdate,
    ProductVariantCreate,
)
from app.services.category_service import CategoryService
from app.services.storage import get_storage


class ProductService:
    """Service class for product operations."""

    @staticmethod
    async def _category_filter(db: AsyncSession, category_id: UUID):
        """
        Build the WHERE clause for "products in this category".

        A category page must show everything filed underneath it, so this
        matches a product whose ``category_id`` is the category *or* whose
        ``subcategory_id`` is one of its children. Passing a sub-category's own
        id still works — it simply has no children, and products pinned to it
        carry it in ``subcategory_id``.
        """
        ids = await CategoryService.get_branch_ids(db, category_id)
        return or_(
            Product.category_id.in_(ids),
            Product.subcategory_id.in_(ids),
        )

    @staticmethod
    async def get_all(
        db: AsyncSession,
        limit: int = 20,
        offset: int = 0,
        category_id: Optional[UUID] = None,
        subcategory_id: Optional[UUID] = None,
        is_featured: Optional[bool] = None,
        search: Optional[str] = None,
        min_price: Optional[Decimal] = None,
        max_price: Optional[Decimal] = None,
        in_stock: Optional[bool] = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        include_inactive: bool = False,
        is_active: Optional[bool] = None,
    ) -> Tuple[List[Product], int]:
        """Get all products with filtering and pagination.

        The default (``include_inactive=False``, ``is_active=None``) preserves
        the original behaviour of returning only active products. Admin
        catalog views pass ``include_inactive=True`` to see the full Global
        Catalog, optionally narrowing to a single status via ``is_active``.
        """

        # Base query
        query = (
            select(Product)
            .options(
                selectinload(Product.category),
                selectinload(Product.subcategory),
                selectinload(Product.images)
            )
        )

        # Status filtering: explicit is_active wins; otherwise keep the
        # active-only default unless the caller opts into inactive rows.
        if is_active is not None:
            query = query.where(Product.is_active == is_active)
        elif not include_inactive:
            query = query.where(Product.is_active == True)

        # Apply filters
        if category_id:
            query = query.where(
                await ProductService._category_filter(db, category_id)
            )

        if subcategory_id:
            query = query.where(Product.subcategory_id == subcategory_id)

        if is_featured is not None:
            query = query.where(Product.is_featured == is_featured)

        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                or_(
                    Product.name.ilike(search_pattern),
                    Product.description.ilike(search_pattern)
                )
            )

        if min_price is not None:
            query = query.where(Product.price >= min_price)

        if max_price is not None:
            query = query.where(Product.price <= max_price)

        if in_stock is not None:
            if in_stock:
                query = query.where(Product.stock_quantity > 0)
            else:
                query = query.where(Product.stock_quantity <= 0)

        # Count total
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar()

        # Apply sorting
        sort_column = getattr(Product, sort_by, Product.created_at)
        if sort_order.lower() == "asc":
            query = query.order_by(sort_column.asc())
        else:
            query = query.order_by(sort_column.desc())

        # Apply pagination
        query = query.limit(limit).offset(offset)

        # Execute
        result = await db.execute(query)
        products = result.scalars().all()

        return products, total

    @staticmethod
    async def get_by_id(
        db: AsyncSession,
        product_id: UUID,
        include_inactive: bool = False
    ) -> Optional[Product]:
        """Get product by ID with all related data."""
        query = (
            select(Product)
            .options(
                selectinload(Product.category),
                selectinload(Product.subcategory),
                selectinload(Product.images),
                selectinload(Product.variants).selectinload(ProductVariant.variant_options).selectinload(VariantOption.variant_type)
            )
            .where(Product.product_id == product_id)
        )

        if not include_inactive:
            query = query.where(Product.is_active == True)

        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_slug(
        db: AsyncSession,
        slug: str,
        include_inactive: bool = False
    ) -> Optional[Product]:
        """Get product by slug."""
        query = (
            select(Product)
            .options(
                selectinload(Product.category),
                selectinload(Product.subcategory),
                selectinload(Product.images),
                selectinload(Product.variants)
            )
            .where(Product.slug == slug)
        )

        if not include_inactive:
            query = query.where(Product.is_active == True)

        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id_or_slug(
        db: AsyncSession,
        identifier: str,
        include_inactive: bool = False
    ) -> Optional[Product]:
        """Get product by ID or slug."""
        try:
            product_uuid = UUID(identifier)
            product = await ProductService.get_by_id(db, product_uuid, include_inactive)
            if product:
                return product
        except ValueError:
            pass

        return await ProductService.get_by_slug(db, identifier, include_inactive)

    @staticmethod
    async def get_featured(
        db: AsyncSession,
        limit: int = 10
    ) -> List[Product]:
        """Get featured products."""
        query = (
            select(Product)
            .options(
                selectinload(Product.category),
                selectinload(Product.subcategory),
                selectinload(Product.images)
            )
            .where(and_(Product.is_active == True, Product.is_featured == True))
            .order_by(Product.created_at.desc())
            .limit(limit)
        )

        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def get_by_category(
        db: AsyncSession,
        category_id: UUID,
        limit: int = 20,
        offset: int = 0
    ) -> Tuple[List[Product], int]:
        """Get products by category."""
        return await ProductService.get_all(
            db,
            limit=limit,
            offset=offset,
            category_id=category_id
        )

    @staticmethod
    async def search(
        db: AsyncSession,
        query_string: str,
        limit: int = 20,
        offset: int = 0
    ) -> Tuple[List[Product], int]:
        """Search products by name or description."""
        return await ProductService.get_all(
            db,
            limit=limit,
            offset=offset,
            search=query_string
        )

    @staticmethod
    async def create(
        db: AsyncSession,
        data: ProductCreate
    ) -> Product:
        """Create a new product."""
        product = Product(
            name=data.name,
            slug=data.slug,
            description=data.description,
            short_description=data.short_description,
            sku=data.sku,
            price=data.price,
            compare_at_price=data.compare_at_price,
            cost_price=data.cost_price,
            category_id=data.category_id,
            subcategory_id=data.subcategory_id,
            stock_quantity=data.stock_quantity,
            low_stock_threshold=data.low_stock_threshold,
            weight=data.weight,
            weight_unit=data.weight_unit,
            is_active=data.is_active,
            is_featured=data.is_featured,
            meta_title=data.meta_title,
            meta_description=data.meta_description
        )

        db.add(product)
        await db.commit()
        await db.refresh(product)

        return product

    @staticmethod
    async def update(
        db: AsyncSession,
        product: Product,
        data: ProductUpdate
    ) -> Product:
        """Update an existing product."""
        update_data = data.model_dump(exclude_unset=True)

        for field, value in update_data.items():
            setattr(product, field, value)

        await db.commit()
        await db.refresh(product)

        return product

    @staticmethod
    async def delete(db: AsyncSession, product: Product) -> None:
        """Delete a product."""
        await db.delete(product)
        await db.commit()

    @staticmethod
    async def increment_view_count(db: AsyncSession, product_id: UUID) -> None:
        """Increment product view count."""
        product = await ProductService.get_by_id(db, product_id, include_inactive=True)
        if product:
            product.view_count += 1
            await db.commit()

    @staticmethod
    async def add_image(
        db: AsyncSession,
        product_id: UUID,
        data: ProductImageCreate
    ) -> ProductImage:
        """Add an image to a product."""
        # If this is primary, unset other primary images
        if data.is_primary:
            await db.execute(
                ProductImage.__table__.update()
                .where(ProductImage.product_id == product_id)
                .values(is_primary=False)
            )

        image = ProductImage(
            product_id=product_id,
            image_url=data.image_url,
            alt_text=data.alt_text,
            is_primary=data.is_primary,
            sort_order=data.sort_order
        )

        db.add(image)
        await db.commit()
        await db.refresh(image)

        return image

    @staticmethod
    async def remove_image(db: AsyncSession, image_id: UUID) -> None:
        """
        Remove a product image — both the row and the stored file.

        The file is deleted through the StorageProvider, so this works the same
        on disk today and in a bucket later. Deleting only the row would leak
        the file forever; on the local backend that is an EC2 volume filling up
        with images nothing references.
        """
        result = await db.execute(
            select(ProductImage).where(ProductImage.image_id == image_id)
        )
        image = result.scalar_one_or_none()

        if not image:
            return

        stored_path = image.image_url
        await db.delete(image)
        await db.commit()

        # After the commit: an orphaned file is a wasted byte, but a deleted file
        # whose row survived a rollback is a broken image on the storefront.
        if stored_path:
            try:
                get_storage().delete(stored_path)
            except Exception as exc:  # never fail the request over cleanup
                logger.warning(f"Could not delete media for image {image_id}: {exc}")

    @staticmethod
    async def set_primary_image(
        db: AsyncSession,
        product_id: UUID,
        image_id: UUID,
    ) -> ProductImage:
        """
        Mark one image as the primary thumbnail, unsetting all others for the
        same product. Raises ValueError if the image doesn't belong to the
        product.
        """
        result = await db.execute(
            select(ProductImage).where(
                ProductImage.image_id == image_id,
                ProductImage.product_id == product_id,
            )
        )
        image = result.scalar_one_or_none()
        if image is None:
            raise ValueError("Image not found for this product")

        # Clear primary on every image for this product, then set the target.
        await db.execute(
            ProductImage.__table__.update()
            .where(ProductImage.product_id == product_id)
            .values(is_primary=False)
        )
        image.is_primary = True
        await db.commit()
        await db.refresh(image)
        return image

    @staticmethod
    async def has_variants(db: AsyncSession, product_id: UUID) -> bool:
        """Check if product has variants."""
        result = await db.execute(
            select(func.count(ProductVariant.variant_id))
            .where(ProductVariant.product_id == product_id)
        )
        count = result.scalar()
        return count > 0

    @staticmethod
    async def get_variants(db: AsyncSession, product_id: UUID) -> List[ProductVariant]:
        """Get product variants."""
        result = await db.execute(
            select(ProductVariant)
            .options(selectinload(ProductVariant.variant_options).selectinload(VariantOption.variant_type))
            .where(ProductVariant.product_id == product_id)
            .order_by(ProductVariant.sort_order)
        )
        return result.scalars().all()

    @staticmethod
    async def add_variant(
        db: AsyncSession,
        product_id: UUID,
        data: ProductVariantCreate
    ) -> ProductVariant:
        """Add a variant to a product."""
        variant = ProductVariant(
            product_id=product_id,
            name=data.name,
            sku=data.sku,
            price=data.price,
            compare_at_price=data.compare_at_price,
            stock_quantity=data.stock_quantity,
            image_url=data.image_url,
            is_active=data.is_active,
            sort_order=data.sort_order
        )

        db.add(variant)
        await db.commit()
        await db.refresh(variant)

        return variant

    @staticmethod
    async def update_stock(
        db: AsyncSession,
        product_id: UUID,
        quantity: int,
        variant_id: Optional[UUID] = None
    ) -> None:
        """Update product or variant stock quantity."""
        if variant_id:
            result = await db.execute(
                select(ProductVariant).where(ProductVariant.variant_id == variant_id)
            )
            variant = result.scalar_one_or_none()
            if variant:
                variant.stock_quantity = quantity
        else:
            product = await ProductService.get_by_id(db, product_id, include_inactive=True)
            if product:
                product.stock_quantity = quantity

        await db.commit()

    # ================================================================
    # Branch-Aware Product Retrieval  (strict visibility enforcement)
    # ================================================================
    #
    # MANDATORY RULES (applied by every ``*_for_branch`` method):
    #   1. A ``branch_inventory`` row MUST exist for the (product, branch).
    #   2. ``branch_inventory.is_active`` MUST be True.
    #   3. ``branch_inventory.stock_quantity`` MUST be > 0.
    # If any rule fails the product is treated as "Hidden" for that branch.
    # ================================================================

    @staticmethod
    def _branch_visibility_join(query, branch_id: UUID):
        """
        Apply the mandatory branch-inventory JOIN + WHERE filters to *query*.

        Returns a tuple (query, eff_price_label, eff_discount_label) so
        callers can use the computed columns for sorting.
        """
        eff_price = func.coalesce(
            BranchInventory.branch_price, Product.price,
        ).label("effective_price")

        eff_discount = func.coalesce(
            BranchInventory.discount_percentage,
            Product.discount_percentage,
            literal(0),
        ).label("effective_discount")

        query = (
            query
            .join(
                BranchInventory,
                and_(
                    BranchInventory.product_id == Product.product_id,
                    BranchInventory.branch_id == branch_id,
                ),
            )
            .where(
                BranchInventory.is_active.is_(True),
                BranchInventory.stock_quantity > 0,
            )
        )
        return query, eff_price, eff_discount

    @staticmethod
    async def get_all_for_branch(
        db: AsyncSession,
        branch_id: UUID,
        limit: int = 20,
        offset: int = 0,
        category_id: Optional[UUID] = None,
        subcategory_id: Optional[UUID] = None,
        is_featured: Optional[bool] = None,
        search: Optional[str] = None,
        min_price: Optional[Decimal] = None,
        max_price: Optional[Decimal] = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Get products visible in *branch_id* with effective pricing.

        Enforces strict branch visibility (JOIN + active + in-stock).
        """
        base = (
            select(Product, BranchInventory)
            .options(
                selectinload(Product.category),
                selectinload(Product.subcategory),
                selectinload(Product.images),
            )
            .where(Product.is_active.is_(True))
        )

        base, eff_price, eff_discount = ProductService._branch_visibility_join(base, branch_id)

        if category_id:
            base = base.where(await ProductService._category_filter(db, category_id))
        if subcategory_id:
            base = base.where(Product.subcategory_id == subcategory_id)
        if is_featured is not None:
            base = base.where(Product.is_featured == is_featured)
        if search:
            pattern = f"%{search}%"
            base = base.where(or_(
                Product.name.ilike(pattern),
                Product.description.ilike(pattern),
            ))
        if min_price is not None:
            base = base.where(eff_price >= min_price)
        if max_price is not None:
            base = base.where(eff_price <= max_price)

        # Count
        count_q = select(func.count()).select_from(base.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        # Sort
        sort_col = getattr(Product, sort_by, Product.created_at)
        base = base.order_by(sort_col.desc() if sort_order == "desc" else sort_col.asc())
        base = base.limit(limit).offset(offset)

        rows = (await db.execute(base)).unique().all()
        items = []
        for product, inv in rows:
            items.append({
                "product": product,
                "effective": ProductService.compute_effective_data(product, inv),
            })
        return items, total

    @staticmethod
    async def get_by_id_for_branch(
        db: AsyncSession,
        product_id: UUID,
        branch_id: UUID,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch a single product detail enforcing strict branch visibility.

        Returns None (→ 404) if the product is not active/stocked in the branch.
        """
        query = (
            select(Product, BranchInventory)
            .options(
                selectinload(Product.category),
                selectinload(Product.subcategory),
                selectinload(Product.images),
                selectinload(Product.variants)
                    .selectinload(ProductVariant.variant_options)
                    .selectinload(VariantOption.variant_type),
            )
            .where(Product.product_id == product_id, Product.is_active.is_(True))
        )
        query, _, _ = ProductService._branch_visibility_join(query, branch_id)

        row = (await db.execute(query)).unique().first()
        if row is None:
            return None
        product, inv = row
        return {
            "product": product,
            "effective": ProductService.compute_effective_data(product, inv),
        }

    @staticmethod
    async def get_by_ids_for_branch(
        db: AsyncSession,
        product_ids: List[UUID],
        branch_id: UUID,
    ) -> List[Dict[str, Any]]:
        """
        Fetch multiple products by ID list, enforcing branch visibility.

        Used by Semantic Search to post-filter vector-search results against
        the user's active branch.  IDs not visible in the branch are silently
        dropped.
        """
        if not product_ids:
            return []

        query = (
            select(Product, BranchInventory)
            .options(
                selectinload(Product.category),
                selectinload(Product.subcategory),
                selectinload(Product.images),
            )
            .where(
                Product.product_id.in_(product_ids),
                Product.is_active.is_(True),
            )
        )
        query, _, _ = ProductService._branch_visibility_join(query, branch_id)

        rows = (await db.execute(query)).unique().all()
        items = []
        for product, inv in rows:
            items.append({
                "product": product,
                "effective": ProductService.compute_effective_data(product, inv),
            })
        return items

    @staticmethod
    async def get_featured_for_branch(
        db: AsyncSession,
        branch_id: UUID,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Get featured products visible in *branch_id*."""
        query = (
            select(Product, BranchInventory)
            .options(
                selectinload(Product.category),
                selectinload(Product.subcategory),
                selectinload(Product.images),
            )
            .where(
                Product.is_active.is_(True),
                Product.is_featured.is_(True),
            )
        )
        query, _, _ = ProductService._branch_visibility_join(query, branch_id)
        query = query.order_by(Product.created_at.desc()).limit(limit)

        rows = (await db.execute(query)).unique().all()
        return [
            {"product": p, "effective": ProductService.compute_effective_data(p, inv)}
            for p, inv in rows
        ]

    # ================================================================
    # Branch Inventory — COALESCE / fallback helpers
    # ================================================================

    @staticmethod
    async def get_branch_inventory(
        db: AsyncSession,
        product_id: UUID,
        branch_id: UUID,
    ) -> Optional[BranchInventory]:
        """Fetch the BranchInventory row for a (product, branch) pair."""
        result = await db.execute(
            select(BranchInventory).where(
                BranchInventory.product_id == product_id,
                BranchInventory.branch_id == branch_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_or_create_branch_inventory(
        db: AsyncSession,
        product_id: UUID,
        branch_id: UUID,
    ) -> BranchInventory:
        """Get existing or create a new BranchInventory row (upsert-like)."""
        inv = await ProductService.get_branch_inventory(db, product_id, branch_id)
        if inv is not None:
            return inv

        inv = BranchInventory(
            product_id=product_id,
            branch_id=branch_id,
        )
        db.add(inv)
        await db.commit()
        await db.refresh(inv)
        return inv

    @staticmethod
    def compute_effective_data(
        product: Product,
        inv: Optional[BranchInventory],
    ) -> Dict[str, Any]:
        """
        Merge global product data with branch overrides using fallback logic.

        Returns a dict with all effective values for presentation.
        """
        global_price = product.price or Decimal("0")
        global_discount = product.discount_percentage
        global_on_sale = product.is_on_sale

        if inv is not None:
            eff_price = Decimal(str(inv.branch_price)) if inv.branch_price is not None else global_price
            eff_discount = inv.discount_percentage if inv.discount_percentage is not None else global_discount
            eff_on_sale = inv.is_on_sale
            stock = inv.stock_quantity
            is_active = inv.is_active
            branch_price = inv.branch_price
            branch_discount = inv.discount_percentage
            inventory_id = inv.inventory_id
        else:
            eff_price = global_price
            eff_discount = global_discount
            eff_on_sale = global_on_sale
            stock = product.stock_quantity
            is_active = product.is_active
            branch_price = None
            branch_discount = None
            inventory_id = None

        # Compute effective discount price
        eff_discount_price = None
        if eff_discount and eff_discount > 0 and eff_price:
            factor = Decimal(str(1 - eff_discount / 100))
            eff_discount_price = (
                eff_price * factor
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return {
            "inventory_id": inventory_id,
            "global_price": float(global_price),
            "branch_price": float(branch_price) if branch_price is not None else None,
            "effective_price": float(eff_price),
            "global_discount": global_discount,
            "branch_discount": branch_discount,
            "effective_discount": eff_discount,
            "effective_discount_price": float(eff_discount_price) if eff_discount_price else None,
            "stock_quantity": stock,
            "is_on_sale": eff_on_sale,
            "is_active": is_active,
        }

    @staticmethod
    async def get_effective_product_data(
        db: AsyncSession,
        product_id: UUID,
        branch_id: UUID,
    ) -> Optional[Dict[str, Any]]:
        """
        Full effective-data lookup for a single product in a branch.

        Returns None if the product doesn't exist or is hidden for the branch.
        """
        product = await ProductService.get_by_id(db, product_id)
        if product is None:
            return None

        inv = await ProductService.get_branch_inventory(db, product_id, branch_id)

        # If a BranchInventory row exists and is_active=False, product is hidden
        if inv is not None and not inv.is_active:
            return None

        return ProductService.compute_effective_data(product, inv)

    # ================================================================
    # Quick Sale feed  (branch-aware)
    # ================================================================

    @staticmethod
    async def get_quick_sale_products(
        db: AsyncSession,
        branch_id: UUID,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Fetch Quick-Sale products for the given branch.

        Joins ``products`` with ``branch_inventory``, applying COALESCE
        fallback for price/discount, and filtering by effective on-sale
        status.  Results are sorted by effective discount DESC.
        """
        # Effective discount: COALESCE(branch, global)
        eff_discount = func.coalesce(
            BranchInventory.discount_percentage,
            Product.discount_percentage,
            literal(0),
        ).label("effective_discount")

        # Effective price: COALESCE(branch_price, product.price)
        eff_price = func.coalesce(
            BranchInventory.branch_price,
            Product.price,
        ).label("effective_price")

        query = (
            select(Product, BranchInventory, eff_discount, eff_price)
            .join(
                BranchInventory,
                and_(
                    BranchInventory.product_id == Product.product_id,
                    BranchInventory.branch_id == branch_id,
                ),
            )
            .options(
                selectinload(Product.category),
                selectinload(Product.subcategory),
                selectinload(Product.images),
            )
            .where(
                Product.is_active.is_(True),
                BranchInventory.is_active.is_(True),
                BranchInventory.is_on_sale.is_(True),
            )
            .order_by(eff_discount.desc())
            .limit(limit)
        )

        result = await db.execute(query)
        rows = result.unique().all()

        items: List[Dict[str, Any]] = []
        for product, inv, _discount_val, _price_val in rows:
            eff_data = ProductService.compute_effective_data(product, inv)
            items.append({
                "product": product,
                "effective": eff_data,
            })
        return items

    # ================================================================
    # Branch Inventory CRUD  (used by Marketing Manager API)
    # ================================================================

    @staticmethod
    async def update_branch_inventory(
        db: AsyncSession,
        product_id: UUID,
        branch_id: UUID,
        **fields,
    ) -> BranchInventory:
        """
        Create-or-update the BranchInventory row for a product in a branch.

        Setting a field to ``None`` clears the override (falls back to global).
        """
        from app.core.exceptions import ProductNotFoundError

        product = await ProductService.get_by_id(db, product_id, include_inactive=True)
        if product is None:
            raise ProductNotFoundError(str(product_id))

        inv = await ProductService.get_or_create_branch_inventory(db, product_id, branch_id)

        for key, value in fields.items():
            if hasattr(inv, key):
                setattr(inv, key, value)

        await db.commit()
        await db.refresh(inv)

        logger.info(
            f"Branch inventory updated: product={product_id} branch={branch_id} "
            f"fields={list(fields.keys())}"
        )
        return inv

    # ================================================================
    # Branch Inventory — admin stock ledger (list / update by inventory_id)
    # ================================================================

    @staticmethod
    async def list_inventory(
        db: AsyncSession,
        branch_id: Optional[UUID] = None,
        limit: int = 20,
        offset: int = 0,
        search: Optional[str] = None,
        low_stock_only: bool = False,
    ) -> Tuple[List[Tuple[BranchInventory, Product, "Branch"]], int]:
        """
        List branch_inventory rows joined with their product and branch.

        ``branch_id`` scopes to a single branch (branch managers); pass None for
        the all-branches view (super admins). Ordered by product name.
        """
        query = (
            select(BranchInventory, Product, Branch)
            .join(Product, BranchInventory.product_id == Product.product_id)
            .join(Branch, BranchInventory.branch_id == Branch.branch_id)
        )

        if branch_id is not None:
            query = query.where(BranchInventory.branch_id == branch_id)

        if search:
            pattern = f"%{search}%"
            query = query.where(
                or_(Product.name.ilike(pattern), Product.sku.ilike(pattern))
            )

        if low_stock_only:
            query = query.where(
                BranchInventory.stock_quantity <= BranchInventory.low_stock_threshold
            )

        count_q = select(func.count()).select_from(query.subquery())
        total = (await db.execute(count_q)).scalar() or 0

        query = query.order_by(Product.name).limit(limit).offset(offset)
        rows = (await db.execute(query)).all()
        return [tuple(r) for r in rows], total

    @staticmethod
    async def get_inventory_by_id(
        db: AsyncSession,
        inventory_id: UUID,
    ) -> Optional[Tuple[BranchInventory, Product, "Branch"]]:
        """Fetch a single inventory row with its product and branch."""
        result = await db.execute(
            select(BranchInventory, Product, Branch)
            .join(Product, BranchInventory.product_id == Product.product_id)
            .join(Branch, BranchInventory.branch_id == Branch.branch_id)
            .where(BranchInventory.inventory_id == inventory_id)
        )
        row = result.first()
        return tuple(row) if row else None

    @staticmethod
    async def update_inventory_row(
        db: AsyncSession,
        inv: BranchInventory,
        **fields,
    ) -> BranchInventory:
        """
        Apply the provided override fields to an existing inventory row.

        Callers pass ``model_dump(exclude_unset=True)``, so a key being present
        means the admin intended to set it — including to ``None``. That
        matters: ``branch_price=None`` is how a Branch Manager *clears* a local
        override and falls the product back to the global price, so None must
        be written through rather than skipped.
        """
        for key, value in fields.items():
            if hasattr(inv, key):
                setattr(inv, key, value)
        await db.commit()
        await db.refresh(inv)
        logger.info(
            f"Inventory row {inv.inventory_id} updated: fields={list(fields.keys())}"
        )
        return inv

    @staticmethod
    async def upsert_branch_override(
        db: AsyncSession,
        product_id: UUID,
        branch_id: UUID,
        **fields,
    ) -> Tuple[BranchInventory, bool]:
        """
        Create or update the branch override for a (product, branch) pair.

        This is how a Branch Manager pulls a product out of the Global Catalog
        and into their branch: the first call inserts the ``branch_inventory``
        row (making the product visible to customers in that branch), and later
        calls update it. Returns ``(row, created)``.

        Idempotent by (product_id, branch_id), which the unique constraint from
        migration 019 guarantees.
        """
        existing = await db.execute(
            select(BranchInventory).where(
                and_(
                    BranchInventory.product_id == product_id,
                    BranchInventory.branch_id == branch_id,
                )
            )
        )
        inv = existing.scalar_one_or_none()

        if inv is not None:
            return await ProductService.update_inventory_row(db, inv, **fields), False

        inv = BranchInventory(product_id=product_id, branch_id=branch_id, **fields)
        db.add(inv)
        await db.commit()
        await db.refresh(inv)
        logger.info(
            f"Branch override created: product={product_id} branch={branch_id}"
        )
        return inv, True

    @staticmethod
    async def list_unstocked_products(
        db: AsyncSession,
        branch_id: UUID,
        limit: int = 20,
        offset: int = 0,
        search: Optional[str] = None,
    ) -> Tuple[List[Product], int]:
        """
        Global-catalog products that *branch_id* does not carry yet.

        Backs the "Add product to my branch" picker: a Branch Manager can only
        override what they don't already have a row for, so anything already in
        branch_inventory is excluded.
        """
        carried = (
            select(BranchInventory.product_id)
            .where(BranchInventory.branch_id == branch_id)
        )

        query = (
            select(Product)
            .options(selectinload(Product.category), selectinload(Product.images))
            .where(
                Product.is_active.is_(True),
                Product.product_id.not_in(carried),
            )
        )

        if search:
            pattern = f"%{search}%"
            query = query.where(
                or_(Product.name.ilike(pattern), Product.sku.ilike(pattern))
            )

        total = (
            await db.execute(select(func.count()).select_from(query.subquery()))
        ).scalar() or 0

        query = query.order_by(Product.name).limit(limit).offset(offset)
        products = (await db.execute(query)).scalars().all()
        return list(products), total

    @staticmethod
    async def update_product_discount(
        db: AsyncSession,
        product_id: UUID,
        discount_percentage: Optional[float],
        is_on_sale: Optional[bool],
    ) -> Product:
        """
        Update global-level discount on a Product (Super Admin use).

        Auto-computes ``discount_price`` from the base price.
        """
        product = await ProductService.get_by_id(db, product_id, include_inactive=True)
        if product is None:
            from app.core.exceptions import ProductNotFoundError
            raise ProductNotFoundError(str(product_id))

        if discount_percentage is not None:
            product.discount_percentage = discount_percentage

            if discount_percentage > 0 and product.price:
                factor = Decimal(str(1 - discount_percentage / 100))
                product.discount_price = (
                    product.price * factor
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                product.discount_price = None

        if is_on_sale is not None:
            product.is_on_sale = is_on_sale

        await db.commit()
        await db.refresh(product)

        logger.info(
            f"Global discount updated for product {product_id}: "
            f"{discount_percentage}% | on_sale={product.is_on_sale}"
        )
        return product
