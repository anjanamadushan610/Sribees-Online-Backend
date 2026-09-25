"""
SRIBEESonline - Enhanced Product Search Service

Advanced search with:
- Full-text search with relevance scoring
- Faceted filtering
- Autocomplete suggestions
- Search analytics
- Redis caching for performance
- **Branch-inventory visibility enforcement** (JOIN + active + in-stock)
"""
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config.redis import get_redis
from app.models.category import Category
from app.models.product import BranchInventory, Product


class SearchFilters:
    """Search filter parameters."""

    def __init__(
        self,
        query: Optional[str] = None,
        category_id: Optional[UUID] = None,
        category_ids: Optional[List[UUID]] = None,
        min_price: Optional[Decimal] = None,
        max_price: Optional[Decimal] = None,
        in_stock: Optional[bool] = None,
        is_featured: Optional[bool] = None,
        is_on_sale: Optional[bool] = None,
        brand: Optional[str] = None,
        brands: Optional[List[str]] = None,
        rating_min: Optional[float] = None,
        tags: Optional[List[str]] = None,
        sort_by: str = "relevance",
        sort_order: str = "desc",
        branch_id: Optional[UUID] = None,
    ):
        self.query = query
        self.category_id = category_id
        self.category_ids = category_ids or []
        self.min_price = min_price
        self.max_price = max_price
        self.in_stock = in_stock
        self.is_featured = is_featured
        self.is_on_sale = is_on_sale
        self.brand = brand
        self.brands = brands or []
        self.rating_min = rating_min
        self.tags = tags or []
        self.sort_by = sort_by
        self.sort_order = sort_order
        self.branch_id = branch_id

    def cache_key(self) -> str:
        """Generate cache key for these filters."""
        key_data = {
            "q": self.query,
            "cat": str(self.category_id) if self.category_id else None,
            "cats": [str(c) for c in self.category_ids],
            "min_p": str(self.min_price) if self.min_price else None,
            "max_p": str(self.max_price) if self.max_price else None,
            "stock": self.in_stock,
            "feat": self.is_featured,
            "sale": self.is_on_sale,
            "brands": self.brands,
            "rating": self.rating_min,
            "tags": self.tags,
            "sort": f"{self.sort_by}_{self.sort_order}",
            "branch": str(self.branch_id) if self.branch_id else None,
        }
        key_str = json.dumps(key_data, sort_keys=True)
        return f"search:{hashlib.md5(key_str.encode(), usedforsecurity=False).hexdigest()}"


class SearchResult:
    """Search result with metadata."""

    def __init__(
        self,
        products: List[Product],
        total: int,
        facets: Dict[str, Any],
        query_time_ms: float,
        from_cache: bool = False,
    ):
        self.products = products
        self.total = total
        self.facets = facets
        self.query_time_ms = query_time_ms
        self.from_cache = from_cache


class ProductSearchService:
    """Enhanced product search with caching and facets."""

    CACHE_TTL = 300  # 5 minutes

    @classmethod
    async def search(
        cls,
        db: AsyncSession,
        filters: SearchFilters,
        limit: int = 20,
        offset: int = 0,
        include_facets: bool = True,
    ) -> SearchResult:
        """
        Search products with advanced filtering.

        Supports:
        - Full-text search with relevance ranking
        - Category filtering (single or multiple)
        - Price range filtering
        - Stock availability
        - Brand filtering
        - Rating filtering
        - Faceted results
        """
        start_time = datetime.utcnow()

        # Try cache first
        cache_key = f"{filters.cache_key()}:{limit}:{offset}"
        cached = await cls._get_cached(cache_key)
        if cached:
            return SearchResult(
                products=cached["products"],
                total=cached["total"],
                facets=cached.get("facets", {}),
                query_time_ms=0,
                from_cache=True,
            )

        # Build query
        query = cls._build_search_query(filters)

        # Get total count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        # Apply sorting
        query = cls._apply_sorting(query, filters)

        # Apply pagination
        query = query.limit(limit).offset(offset)

        # Load relationships
        query = query.options(
            selectinload(Product.category),
            selectinload(Product.images),
        )

        # Execute
        result = await db.execute(query)
        products = result.scalars().all()

        # Get facets
        facets = {}
        if include_facets:
            facets = await cls._get_facets(db, filters)

        query_time = (datetime.utcnow() - start_time).total_seconds() * 1000

        # Cache results
        await cls._cache_results(cache_key, products, total, facets)

        # Log search analytics
        await cls._log_search(filters.query, total)

        return SearchResult(
            products=products,
            total=total,
            facets=facets,
            query_time_ms=query_time,
        )

    @classmethod
    def _build_search_query(cls, filters: SearchFilters):
        """
        Build the search query with filters.

        When ``filters.branch_id`` is set, enforces strict branch visibility:
        - INNER JOIN with ``branch_inventory``
        - ``branch_inventory.is_active = True``
        - ``branch_inventory.stock_quantity > 0``
        - Effective price uses COALESCE(branch_price, product.price)
        """
        query = select(Product).where(Product.is_active == True)

        # Branch isolation via branch_inventory JOIN
        if filters.branch_id is not None:
            query = (
                query
                .join(
                    BranchInventory,
                    and_(
                        BranchInventory.product_id == Product.product_id,
                        BranchInventory.branch_id == filters.branch_id,
                    ),
                )
                .where(
                    BranchInventory.is_active.is_(True),
                    BranchInventory.stock_quantity > 0,
                )
            )

        # Full-text search
        if filters.query:
            query = query.where(
                or_(
                    func.lower(Product.name).contains(filters.query.lower()),
                    func.lower(Product.description).contains(filters.query.lower()),
                    func.lower(Product.sku).contains(filters.query.lower()),
                )
            )

        # Category filter
        if filters.category_id:
            query = query.where(Product.category_id == filters.category_id)
        elif filters.category_ids:
            query = query.where(Product.category_id.in_(filters.category_ids))

        # Price range — use effective price when branch context is available
        if filters.branch_id is not None:
            eff_price = func.coalesce(BranchInventory.branch_price, Product.price)
            if filters.min_price is not None:
                query = query.where(eff_price >= filters.min_price)
            if filters.max_price is not None:
                query = query.where(eff_price <= filters.max_price)
        else:
            if filters.min_price is not None:
                query = query.where(Product.price >= filters.min_price)
            if filters.max_price is not None:
                query = query.where(Product.price <= filters.max_price)

        # Stock availability — when branch is present, use branch_inventory stock
        if filters.in_stock is True:
            if filters.branch_id is not None:
                query = query.where(BranchInventory.stock_quantity > 0)
            else:
                query = query.where(Product.stock_quantity > 0)
        elif filters.in_stock is False:
            if filters.branch_id is not None:
                query = query.where(BranchInventory.stock_quantity <= 0)
            else:
                query = query.where(Product.stock_quantity <= 0)

        # Featured products
        if filters.is_featured is not None:
            query = query.where(Product.is_featured == filters.is_featured)

        # On sale products — use branch override when available
        if filters.is_on_sale is True:
            if filters.branch_id is not None:
                query = query.where(BranchInventory.is_on_sale.is_(True))
            else:
                query = query.where(Product.is_on_sale.is_(True))

        # Rating filter (attribute may not exist on all models)
        if filters.rating_min is not None and hasattr(Product, "average_rating"):
            query = query.where(Product.average_rating >= filters.rating_min)

        return query

    @classmethod
    def _apply_sorting(cls, query, filters: SearchFilters):
        """Apply sorting to query."""
        sort_order = desc if filters.sort_order == "desc" else lambda x: x.asc()

        if filters.sort_by == "price_low":
            # Sort by effective price (sale_price if exists, else price)
            query = query.order_by(
                case(
                    (Product.sale_price.isnot(None), Product.sale_price),
                    else_=Product.price,
                ).asc()
            )
        elif filters.sort_by == "price_high":
            query = query.order_by(
                case(
                    (Product.sale_price.isnot(None), Product.sale_price),
                    else_=Product.price,
                ).desc()
            )
        elif filters.sort_by == "newest":
            query = query.order_by(Product.created_at.desc())
        elif filters.sort_by == "rating":
            query = query.order_by(Product.average_rating.desc().nullslast())
        elif filters.sort_by == "popularity":
            query = query.order_by(Product.sales_count.desc().nullslast())
        elif filters.sort_by == "name":
            query = query.order_by(sort_order(Product.name))
        else:
            # Default: relevance (if search query) or newest
            if filters.query:
                # Boost exact matches
                query = query.order_by(
                    case(
                        (func.lower(Product.name) == filters.query.lower(), 0),
                        (func.lower(Product.name).startswith(filters.query.lower()), 1),
                        else_=2,
                    ),
                    Product.sales_count.desc().nullslast(),
                )
            else:
                query = query.order_by(Product.created_at.desc())

        return query

    @classmethod
    async def _get_facets(cls, db: AsyncSession, filters: SearchFilters) -> Dict[str, Any]:
        """Get faceted search results."""
        facets = {}

        # Build base query (without category/brand/price filters for accurate facets)
        base_query = select(Product).where(Product.is_active == True)

        if filters.query:
            base_query = base_query.where(
                or_(
                    func.lower(Product.name).contains(filters.query.lower()),
                    func.lower(Product.description).contains(filters.query.lower()),
                )
            )

        # Category facets
        cat_query = (
            select(
                Category.category_id,
                Category.name,
                func.count(Product.product_id).label("count"),
            )
            .join(Product, Product.category_id == Category.category_id)
            .where(Product.is_active == True)
            .group_by(Category.category_id, Category.name)
            .order_by(desc("count"))
            .limit(20)
        )

        cat_result = await db.execute(cat_query)
        facets["categories"] = [
            {"id": str(row.category_id), "name": row.name, "count": row.count}
            for row in cat_result
        ]

        # Price range facets
        price_query = select(
            func.min(
                case(
                    (Product.sale_price.isnot(None), Product.sale_price),
                    else_=Product.price,
                )
            ).label("min_price"),
            func.max(
                case(
                    (Product.sale_price.isnot(None), Product.sale_price),
                    else_=Product.price,
                )
            ).label("max_price"),
        ).where(Product.is_active == True)

        price_result = await db.execute(price_query)
        price_row = price_result.first()
        if price_row:
            facets["price_range"] = {
                "min": float(price_row.min_price) if price_row.min_price else 0,
                "max": float(price_row.max_price) if price_row.max_price else 0,
            }

        # Brand facets
        brand_query = (
            select(
                Product.brand,
                func.count(Product.product_id).label("count"),
            )
            .where(and_(Product.is_active == True, Product.brand.isnot(None)))
            .group_by(Product.brand)
            .order_by(desc("count"))
            .limit(20)
        )

        brand_result = await db.execute(brand_query)
        facets["brands"] = [
            {"name": row.brand, "count": row.count}
            for row in brand_result
            if row.brand
        ]

        return facets

    @classmethod
    async def autocomplete(
        cls,
        db: AsyncSession,
        query: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Get autocomplete suggestions for search.

        Returns product names and categories matching the query.
        """
        if not query or len(query) < 2:
            return []

        suggestions = []
        search_term = f"{query.lower()}%"

        # Product name suggestions
        product_query = (
            select(Product.name, Product.product_id)
            .where(
                and_(
                    Product.is_active == True,
                    func.lower(Product.name).like(search_term),
                )
            )
            .order_by(Product.sales_count.desc().nullslast())
            .limit(limit)
        )

        product_result = await db.execute(product_query)
        for row in product_result:
            suggestions.append({
                "type": "product",
                "text": row.name,
                "id": str(row.product_id),
            })

        # Category suggestions
        if len(suggestions) < limit:
            cat_query = (
                select(Category.name, Category.category_id)
                .where(func.lower(Category.name).like(search_term))
                .limit(limit - len(suggestions))
            )

            cat_result = await db.execute(cat_query)
            for row in cat_result:
                suggestions.append({
                    "type": "category",
                    "text": row.name,
                    "id": str(row.category_id),
                })

        return suggestions

    @classmethod
    async def get_trending_searches(cls, limit: int = 10) -> List[str]:
        """Get trending search terms from Redis."""
        redis = get_redis()
        if not redis:
            return []

        try:
            # Get top search terms by score
            trending = await redis.zrevrange(
                "search:trending",
                0,
                limit - 1,
                withscores=False,
            )
            return [t.decode() if isinstance(t, bytes) else t for t in trending]
        except Exception as e:
            logger.error(f"Failed to get trending searches: {e}")
            return []

    @classmethod
    async def _log_search(cls, query: Optional[str], result_count: int) -> None:
        """Log search for analytics."""
        if not query:
            return

        redis = get_redis()
        if not redis:
            return

        try:
            # Increment search term count
            await redis.zincrby("search:trending", 1, query.lower())

            # Trim to top 1000 terms
            await redis.zremrangebyrank("search:trending", 0, -1001)

            # Log zero-result searches for improvement
            if result_count == 0:
                await redis.lpush("search:zero_results", query.lower())
                await redis.ltrim("search:zero_results", 0, 999)

        except Exception as e:
            logger.error(f"Failed to log search: {e}")

    @classmethod
    async def _get_cached(cls, cache_key: str) -> Optional[Dict]:
        """Get cached search results."""
        redis = get_redis()
        if not redis:
            return None

        try:
            cached = await redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.error(f"Cache get failed: {e}")

        return None

    @classmethod
    async def _cache_results(
        cls,
        cache_key: str,
        products: List[Product],
        total: int,
        facets: Dict,
    ) -> None:
        """Cache search results."""
        redis = get_redis()
        if not redis:
            return

        try:
            # Serialize products (just IDs and basic info for cache)
            cache_data = {
                "product_ids": [str(p.product_id) for p in products],
                "total": total,
                "facets": facets,
            }
            await redis.setex(
                cache_key,
                cls.CACHE_TTL,
                json.dumps(cache_data),
            )
        except Exception as e:
            logger.error(f"Cache set failed: {e}")
