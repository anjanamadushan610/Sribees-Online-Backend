"""
Seed Script: Branches and Post Office → Branch Mappings

Seeds 3 branches (matching existing demo data) and 10 major Sri Lankan
Post Offices mapped to those branches for testing the address-based
branch routing flow.

Usage:
    cd fastapi_backend
    python -m migrations.seed_branch_mappings

Prerequisites:
    - Migration 011 must have been run first (branches + post_office_branch_mapping tables)
    - The 'admins' table must exist (for manager FK, but nullable so not strictly required)
"""
import asyncio
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from app.config.database import engine
from app.utils.logger import logger

# ============================================================================
# Seed Data
# ============================================================================

BRANCHES = [
    {
        "branch_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "name": "Colombo Central Branch",
        "code": "COL",
        "address": "123 Galle Road, Colombo 03",
        "post_office": "Colombo",
        "district": "Colombo",
        "province": "Western",
        "phone": "+94112345678",
    },
    {
        "branch_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
        "name": "Kandy Branch",
        "code": "KDY",
        "address": "45 Peradeniya Road, Kandy",
        "post_office": "Kandy",
        "district": "Kandy",
        "province": "Central",
        "phone": "+94812345678",
    },
    {
        "branch_id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
        "name": "Galle Branch",
        "code": "GLL",
        "address": "78 Matara Road, Galle",
        "post_office": "Galle",
        "district": "Galle",
        "province": "Southern",
        "phone": "+94912345678",
    },
]

POST_OFFICE_MAPPINGS = [
    # Colombo Central Branch — Western Province
    {
        "post_office": "Colombo",
        "branch_code": "COL",
        "district": "Colombo",
        "province": "Western",
    },
    {
        "post_office": "Nugegoda",
        "branch_code": "COL",
        "district": "Colombo",
        "province": "Western",
    },
    {
        "post_office": "Dehiwala",
        "branch_code": "COL",
        "district": "Colombo",
        "province": "Western",
    },
    {
        "post_office": "Moratuwa",
        "branch_code": "COL",
        "district": "Colombo",
        "province": "Western",
    },
    # Kandy Branch — Central Province
    {
        "post_office": "Kandy",
        "branch_code": "KDY",
        "district": "Kandy",
        "province": "Central",
    },
    {
        "post_office": "Peradeniya",
        "branch_code": "KDY",
        "district": "Kandy",
        "province": "Central",
    },
    {
        "post_office": "Katugastota",
        "branch_code": "KDY",
        "district": "Kandy",
        "province": "Central",
    },
    # Galle Branch — Southern Province
    {
        "post_office": "Galle",
        "branch_code": "GLL",
        "district": "Galle",
        "province": "Southern",
    },
    {
        "post_office": "Matara",
        "branch_code": "GLL",
        "district": "Matara",
        "province": "Southern",
    },
    {
        "post_office": "Hikkaduwa",
        "branch_code": "GLL",
        "district": "Galle",
        "province": "Southern",
    },
    # Colombo Central — Western Province / Kalutara (for app address form)
    {
        "post_office": "Welipenna",
        "branch_code": "COL",
        "district": "Kalutara",
        "province": "Western Province",
    },
    {
        "post_office": "Mathugama",
        "branch_code": "COL",
        "district": "Kalutara",
        "province": "Western Province",
    },
    {
        "post_office": "Meegahathanna",
        "branch_code": "COL",
        "district": "Kalutara",
        "province": "Western Province",
    },
]


async def seed() -> None:
    """Seed branches and post office mappings."""
    logger.info("Seeding branches and post office → branch mappings...")

    async with engine.begin() as conn:
        # ------------------------------------------------------------------
        # 1. Upsert branches
        # ------------------------------------------------------------------
        for branch in BRANCHES:
            await conn.execute(
                text("""
                    INSERT INTO branches (branch_id, name, code, address, post_office,
                                          district, province, phone, is_active)
                    VALUES (:branch_id, :name, :code, :address, :post_office,
                            :district, :province, :phone, TRUE)
                    ON CONFLICT (code) DO UPDATE SET
                        name = EXCLUDED.name,
                        address = EXCLUDED.address,
                        post_office = EXCLUDED.post_office,
                        district = EXCLUDED.district,
                        province = EXCLUDED.province,
                        phone = EXCLUDED.phone,
                        updated_at = NOW()
                """),
                branch,
            )
            logger.info(f"  Branch upserted: {branch['code']} — {branch['name']}")

        # ------------------------------------------------------------------
        # 2. Build code → id map
        # ------------------------------------------------------------------
        result = await conn.execute(
            text("SELECT branch_id, code, name FROM branches WHERE code IN :codes"),
            {"codes": tuple(b["code"] for b in BRANCHES)},
        )
        branch_map = {row.code: (str(row.branch_id), row.name) for row in result}

        # ------------------------------------------------------------------
        # 3. Upsert post office mappings
        # ------------------------------------------------------------------
        for mapping in POST_OFFICE_MAPPINGS:
            branch_id, branch_name = branch_map[mapping["branch_code"]]
            await conn.execute(
                text("""
                    INSERT INTO post_office_branch_mapping
                        (mapping_id, post_office, branch_id, branch_name, district, province, is_active)
                    VALUES
                        (:mapping_id, :post_office, :branch_id, :branch_name, :district, :province, TRUE)
                    ON CONFLICT (post_office) DO UPDATE SET
                        branch_id = EXCLUDED.branch_id,
                        branch_name = EXCLUDED.branch_name,
                        district = EXCLUDED.district,
                        province = EXCLUDED.province,
                        is_active = TRUE,
                        updated_at = NOW()
                """),
                {
                    "mapping_id": str(uuid4()),
                    "post_office": mapping["post_office"],
                    "branch_id": branch_id,
                    "branch_name": branch_name,
                    "district": mapping["district"],
                    "province": mapping["province"],
                },
            )
            logger.info(
                f"  Mapping upserted: {mapping['post_office']} → "
                f"{branch_name} ({mapping['branch_code']})"
            )

    logger.info(
        f"Seeding complete: {len(BRANCHES)} branches, "
        f"{len(POST_OFFICE_MAPPINGS)} post office mappings"
    )


if __name__ == "__main__":
    asyncio.run(seed())
