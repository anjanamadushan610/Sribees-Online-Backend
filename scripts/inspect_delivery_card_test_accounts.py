"""Read selected development test identities without exposing credentials."""
import asyncio
import base64
import json
import os
import re
import sys


def validate_selection(selection):
    code = str(selection.get("customer_code", "")).strip().upper()
    rider = str(selection.get("rider_name", "")).strip()
    if not re.fullmatch(r"SR[0-9A-F]{6}", code):
        raise ValueError("Customer code must be SR followed by six hexadecimal characters")
    if not rider or len(rider) > 150 or any(ord(char) < 32 for char in rider):
        raise ValueError("A valid rider name is required")
    return code, rider


async def inspect_accounts(kind):
    from sqlalchemy import text

    selection = json.loads(base64.b64decode(os.environ["CARD_TEST_SELECTION_B64"], validate=True))
    code, rider = validate_selection(selection)
    if kind == "online":
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.config.settings import settings

        if settings.app_env != "development":
            raise ValueError("Customer lookup requires development")
        engine = create_async_engine(settings.async_database_url, echo=False)
        query = text(
            "SELECT user_id, (email IS NOT NULL AND trim(email) <> '') AS has_email "
            "FROM users WHERE lower(replace(user_id::text, '-', '')) LIKE :prefix "
            "AND deleted_at IS NULL AND is_active = true"
        )
        parameters = {"prefix": code[2:].lower() + "%"}
    elif kind == "express":
        from app.config import get_settings
        from app.db import engine

        if get_settings().app_env != "development":
            raise ValueError("Rider lookup requires development")
        query = text(
            "SELECT s.id, s.name FROM staff s "
            "WHERE lower(trim(s.name)) = lower(:name) AND s.is_active = true "
            "AND EXISTS (SELECT 1 FROM staff_roles sr JOIN roles r ON r.id = sr.role_id "
            "WHERE sr.staff_id = s.id AND r.name = 'Delivery Rider' AND r.guard_name = 'staff')"
        )
        parameters = {"name": rider}
    else:
        raise ValueError("Lookup kind must be online or express")
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            rows = (await connection.execute(query, parameters)).mappings().all()
            if len(rows) != 1:
                raise ValueError(f"Expected exactly one active development {kind} account; found {len(rows)}")
            row = rows[0]
            if kind == "online":
                result = {"customer_code": code, "customer_id": str(row["user_id"]), "has_email": row["has_email"]}
                result["profile_email_status"] = "configured" if row["has_email"] else "missing"
                orders = (await connection.execute(text(
                    "SELECT order_number, status, payment_method, payment_status, total_amount, "
                    "courier_shipment_id, branch_id FROM orders "
                    "WHERE user_id = :user_id ORDER BY created_at DESC LIMIT 5"
                ), {"user_id": row["user_id"]})).mappings().all()
                result["recent_orders"] = [dict(order) for order in orders]
                from types import SimpleNamespace

                from app.payments.delivery_card import payment_view

                payment_orders = (await connection.execute(text(
                    "SELECT order_number, user_id, status, payment_method, payment_status, total_amount, "
                    "courier_shipment_id, courier_cod_collected, delivery_card_state "
                    "FROM orders WHERE user_id = :user_id ORDER BY created_at DESC LIMIT 5"
                ), {"user_id": row["user_id"]})).mappings().all()
                result["payment_actions"] = []
                for payment_order in payment_orders:
                    view = payment_view(SimpleNamespace(**payment_order), "en")
                    result["payment_actions"].append({
                        "order_number": payment_order["order_number"],
                        "state": view["state"] if view else None,
                        "action": "available" if view and view["can_pay"] else "unavailable",
                    })
            else:
                result = {"rider_id": row["id"], "rider_name": row["name"]}
            print(json.dumps(result, default=str))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(inspect_accounts(sys.argv[1]))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except Exception as exc:
        raise SystemExit(f"Development account lookup failed ({type(exc).__name__})") from None
