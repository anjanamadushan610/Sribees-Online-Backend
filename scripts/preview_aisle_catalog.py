"""Copy a bounded public catalog sample into one development branch.

Production reads run in a PostgreSQL read-only transaction. Inserts are atomic,
additive and idempotent. Customer, order, payment and credential tables are never
read. Stock is synthetic development stock, not production inventory.
"""
import asyncio
import json
import subprocess
import sys
import uuid
from urllib.parse import urljoin

PARENTS = ("574a2784-93cc-4a38-90b5-b239d569123e", "7de34f70-3be7-44eb-adf7-6332cfc56491")
BRANCH = "723859af-7a41-4eb3-963c-b2b8b1beb4bb"
CATEGORY_FIELDS = "category_id,name,name_si,name_ta,slug,description,description_si,description_ta,image_url,parent_category_id,is_active"
PRODUCT_FIELDS = "product_id,name,name_si,name_ta,slug,description,description_si,description_ta,short_description,short_description_si,short_description_ta,sku,price,category_id,subcategory_id,weight,weight_unit,display_size,display_size_unit,is_active,is_returnable"
IMAGE_FIELDS = "image_id,product_id,image_url,alt_text,is_primary,sort_order"


async def export_catalog(main_only=False):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.config.settings import settings

    if settings.app_env != "production":
        raise ValueError("Export requires production source")
    engine = create_async_engine(settings.async_database_url, echo=False)
    try:
        async with engine.connect() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            if main_only:
                categories = (await db.execute(text(
                    f"SELECT {CATEGORY_FIELDS} FROM categories "
                    "WHERE is_active = true AND parent_category_id IS NULL ORDER BY name"
                ))).mappings().all()
                if not 1 <= len(categories) <= 24:
                    raise ValueError("Unexpected main category count")
                print("SRIBEES_CATALOG_JSON=" + json.dumps(
                    {"categories": [dict(row) for row in categories]}, default=str))
                return
            params = {"a": uuid.UUID(PARENTS[0]), "b": uuid.UUID(PARENTS[1])}
            categories = (await db.execute(text(
                f"SELECT {CATEGORY_FIELDS} FROM categories WHERE is_active = true AND "
                "(category_id IN (:a,:b) OR parent_category_id IN (:a,:b)) "
                "ORDER BY parent_category_id NULLS FIRST, name"
            ), params)).mappings().all()
            products = []
            for category in categories:
                if category["parent_category_id"] is None:
                    continue
                rows = (await db.execute(text(
                    f"SELECT {PRODUCT_FIELDS} FROM products WHERE is_active = true "
                    "AND subcategory_id = :id AND sold_by_weight = false "
                    "AND EXISTS (SELECT 1 FROM product_images i WHERE i.product_id = products.product_id) "
                    "ORDER BY name LIMIT 8"
                ), {"id": category["category_id"]})).mappings().all()
                products.extend(dict(row) for row in rows)
            images = []
            for product in products:
                rows = (await db.execute(text(
                    f"SELECT {IMAGE_FIELDS} FROM product_images WHERE product_id = :id "
                    "ORDER BY is_primary DESC, sort_order LIMIT 1"
                ), {"id": product["product_id"]})).mappings().all()
                for row in rows:
                    image = dict(row)
                    image["image_url"] = urljoin("https://api.sribeesonline.com/", image["image_url"])
                    images.append(image)
            payload = {"categories": [dict(row) for row in categories], "products": products, "images": images}
            print("SRIBEES_CATALOG_JSON=" + json.dumps(payload, default=str))
    finally:
        await engine.dispose()


async def import_catalog(payload):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.config.settings import settings
    from app.services.catalog_search_service import _index_fields

    if settings.app_env != "development":
        raise ValueError("Import requires development destination")
    if len(payload["categories"]) > 30 or len(payload["products"]) > 160:
        raise ValueError("Sample exceeds preview bounds")
    engine = create_async_engine(settings.async_database_url, echo=False)
    inserted = {"categories": 0, "products": 0, "product_images": 0, "branch_inventory": 0, "branch_categories": 0}

    async def insert(db, table, fields, row):
        # Identifiers originate exclusively from the constants/callers below.
        statement = text(f"INSERT INTO {table} ({fields}) SELECT {fields} "
                         f"FROM json_populate_record(NULL::{table}, CAST(:row AS json)) "
                         "ON CONFLICT DO NOTHING")
        result = await db.execute(statement, {"row": json.dumps(row)})
        inserted[table] += result.rowcount

    try:
        async with engine.begin() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(784102119)"))
            branch = await db.scalar(text("SELECT branch_id FROM branches WHERE branch_id = :id AND is_active = true"), {"id": uuid.UUID(BRANCH)})
            if branch is None:
                raise ValueError("Expected active development preview branch")
            valid_categories = set(PARENTS)
            for row in payload["categories"]:
                if row["category_id"] not in PARENTS and row["parent_category_id"] not in PARENTS:
                    raise ValueError("Unexpected category outside preview scope")
                valid_categories.add(row["category_id"])
                if row.get("image_url"):
                    row["image_url"] = urljoin("https://api.sribeesonline.com/", row["image_url"])
                await insert(db, "categories", CATEGORY_FIELDS, row)
                exists = await db.scalar(text("SELECT category_id FROM categories WHERE category_id = :id"), {"id": uuid.UUID(row["category_id"])})
                if exists is None:
                    raise ValueError("Category slug conflict. Existing data preserved")
                await insert(db, "branch_categories", "branch_category_id,branch_id,category_id,is_active", {
                    "branch_category_id": str(uuid.uuid4()), "branch_id": BRANCH, "category_id": row["category_id"], "is_active": True})
            for row in payload["products"]:
                if row["category_id"] not in PARENTS or row["subcategory_id"] not in valid_categories:
                    raise ValueError("Unexpected product category")
                row.update(stock_quantity=25, stock_mode="unit", sold_by_weight=False, is_featured=False, is_on_sale=False, view_count=0, low_stock_threshold=5)
                row.update(_index_fields(row["name"], row.get("name_si"), row.get("name_ta"), None))
                fields = ",".join(row)
                await insert(db, "products", fields, row)
                exists = await db.scalar(text("SELECT product_id FROM products WHERE product_id = :id"), {"id": uuid.UUID(row["product_id"])})
                if exists is None:
                    raise ValueError("Product slug/SKU conflict. Existing data preserved")
                await insert(db, "branch_inventory", "inventory_id,branch_id,product_id,stock_quantity,is_active,is_on_sale", {
                    "inventory_id": str(uuid.uuid4()), "branch_id": BRANCH, "product_id": row["product_id"],
                    "stock_quantity": 25, "is_active": True, "is_on_sale": False})
            for row in payload["images"]:
                await insert(db, "product_images", IMAGE_FIELDS, row)
            print(json.dumps({"inserted": inserted, "preview_branch": BRANCH, "sample_products": len(payload["products"])}))
    finally:
        await engine.dispose()



async def import_main_categories(payload):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.config.settings import settings

    if settings.app_env != "development":
        raise ValueError("Main category preview requires development")
    rows = payload["categories"]
    if not 1 <= len(rows) <= 24 or any(row["parent_category_id"] is not None for row in rows):
        raise ValueError("Expected a bounded main-category list")
    engine = create_async_engine(settings.async_database_url, echo=False)
    try:
        async with engine.begin() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(784102119)"))
            branch = await db.scalar(text("SELECT branch_id FROM branches WHERE branch_id = :id AND is_active = true"), {"id": uuid.UUID(BRANCH)})
            if branch is None:
                raise ValueError("Expected active development preview branch")
            for row in rows:
                if row.get("image_url"):
                    row["image_url"] = urljoin("https://api.sribeesonline.com/", row["image_url"])
                await db.execute(text(
                    f"INSERT INTO categories ({CATEGORY_FIELDS}) SELECT {CATEGORY_FIELDS} "
                    "FROM json_populate_record(NULL::categories, CAST(:row AS json)) "
                    "ON CONFLICT (category_id) DO UPDATE SET "
                    "name = EXCLUDED.name, name_si = EXCLUDED.name_si, name_ta = EXCLUDED.name_ta, "
                    "slug = EXCLUDED.slug, description = EXCLUDED.description, "
                    "description_si = EXCLUDED.description_si, description_ta = EXCLUDED.description_ta, "
                    "image_url = EXCLUDED.image_url, is_active = EXCLUDED.is_active "
                    "WHERE categories.parent_category_id IS NULL"
                ), {"row": json.dumps(row)})
                await db.execute(text(
                    "INSERT INTO branch_categories (branch_category_id, branch_id, category_id, is_active) "
                    "VALUES (:id, :branch, :category, true) "
                    "ON CONFLICT (category_id, branch_id) DO UPDATE SET is_active = true"
                ), {"id": uuid.uuid4(), "branch": uuid.UUID(BRANCH), "category": uuid.UUID(row["category_id"])})
            total = await db.scalar(text("SELECT count(*) FROM categories WHERE is_active = true AND parent_category_id IS NULL"))
            print(json.dumps({"main_categories_synced": len(rows), "development_main_categories": total, "branch": BRANCH}))
    finally:
        await engine.dispose()


async def configure_preview_offers():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.config.settings import settings

    if settings.app_env != "development":
        raise ValueError("Preview offers require development")
    engine = create_async_engine(settings.async_database_url, echo=False)
    try:
        async with engine.begin() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(784102119)"))
            result = await db.execute(text(
                "WITH selected AS (SELECT p.product_id, "
                "row_number() OVER (PARTITION BY p.subcategory_id ORDER BY p.name) AS rank "
                "FROM products p JOIN branch_inventory b ON b.product_id = p.product_id "
                "WHERE p.category_id IN (:a,:b) AND b.branch_id = :branch "
                "AND p.is_active = true AND b.is_active = true AND b.stock_quantity > 0) "
                "UPDATE branch_inventory b SET is_on_sale = true, "
                "discount_percentage = 10 + selected.rank * 5 "
                "FROM selected WHERE b.product_id = selected.product_id "
                "AND b.branch_id = :branch AND selected.rank <= 3 "
                "AND b.is_on_sale = false AND b.discount_percentage IS NULL "
                "RETURNING b.product_id"
            ), {"a": uuid.UUID(PARENTS[0]), "b": uuid.UUID(PARENTS[1]), "branch": uuid.UUID(BRANCH)})
            print(json.dumps({"development_preview_offers_added": len(result.all()),
                              "test_discount_percentages": [15, 20, 25], "branch": BRANCH}))
    finally:
        await engine.dispose()


def container(project):
    result = subprocess.check_output(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
                                      "--filter", "label=com.docker.compose.service=app", "--format", "{{.ID}}"], text=True).split()
    if len(result) != 1:
        raise ValueError(f"Expected one {project} app container")
    return result[0]


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode in {"export", "export-main"}:
        asyncio.run(export_catalog(main_only=mode == "export-main"))
    elif mode == "import-main":
        asyncio.run(import_main_categories(json.load(sys.stdin)))
    elif mode == "import":
        asyncio.run(import_catalog(json.load(sys.stdin)))
    elif mode == "configure-offers":
        asyncio.run(configure_preview_offers())
    elif mode == "offers":
        code = sys.stdin.read()
        destination = container("sribees-online-dev")
        subprocess.run(["docker", "exec", destination, "python", "-c", code, "configure-offers"], check=True)
    elif mode in {"copy", "copy-main"}:
        # The same reviewed script is passed as code, without creating server files.
        code = sys.stdin.read()
        source = container("sribees-online-prod")
        destination = container("sribees-online-dev")
        sample = subprocess.check_output(["docker", "exec", source, "python", "-c", code, "export-main" if mode == "copy-main" else "export"], text=True)
        records = [line.removeprefix("SRIBEES_CATALOG_JSON=") for line in sample.splitlines() if line.startswith("SRIBEES_CATALOG_JSON=")]
        if len(records) != 1:
            raise ValueError("Expected exactly one catalog export payload")
        payload = json.loads(records[0])
        print(json.dumps({"source_sample": {key: len(value) for key, value in payload.items()}}))
        subprocess.run(["docker", "exec", "-i", destination, "python", "-c", code, "import-main" if mode == "copy-main" else "import"],
                       input=json.dumps(payload), text=True, check=True)
    else:
        raise ValueError("Unknown operation")
