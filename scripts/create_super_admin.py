"""Create the first super admin on a database that has none.

Nothing is seeded any more (demo seeds removed 2026-11-07), so a brand-new
database starts with no admin at all. Run this once, inside the app container:

    NEW_ADMIN_EMAIL=you@example.com NEW_ADMIN_PASSWORD='...' \
        python scripts/create_super_admin.py

The password comes from the environment, never the command line, so it does
not land in shell history or a process list. An email that already exists is
left untouched (exit 0), so a CI step can run this every time.
"""

import asyncio
import os
import sys

from sqlalchemy import select, text


async def main() -> int:
    email = (os.environ.get("NEW_ADMIN_EMAIL") or "").strip().lower()
    password = os.environ.get("NEW_ADMIN_PASSWORD") or ""
    name = (os.environ.get("NEW_ADMIN_NAME") or "Super Admin").strip()
    if not email or len(password) < 12:
        print("NEW_ADMIN_EMAIL and NEW_ADMIN_PASSWORD (12+ characters) are required")
        return 2

    from app.config.database import get_db_context
    from app.core.security import hash_password
    from app.models.admin import Admin, AdminRole

    async with get_db_context() as db:
        if (await db.execute(select(Admin.admin_id).where(Admin.email == email))).scalar_one_or_none():
            print("admin already exists, nothing to do")
            return 0
        role_id = None
        if (await db.execute(text("SELECT to_regclass('public.roles')"))).scalar() is not None:
            role_id = (
                await db.execute(text("SELECT role_id FROM roles WHERE system_key = 'super_admin'"))
            ).scalar()
        db.add(Admin(
            email=email,
            password_hash=hash_password(password),
            full_name=name,
            role=AdminRole.SUPER_ADMIN,
            role_id=role_id,
            is_active=True,
        ))
        await db.commit()
    print("super admin created")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(asyncio.run(main()))
