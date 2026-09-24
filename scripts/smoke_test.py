#!/usr/bin/env python3
"""
Post-deploy smoke test.

Run against a live deployment right after migrations, before the deploy is
declared successful. It asks the questions a person would ask if they opened the
admin panel: can I log in, do the analytics endpoints answer, and are the
numbers real and self-consistent?

This is deliberately narrow. It is not a substitute for the browser suite in the
admin repository — it is the check that catches the failures which only appear
against the deployed database: a seed that did not run, an admin account that
cannot authenticate, a branch table that came back empty.

Usage:
    python scripts/smoke_test.py --base-url http://127.0.0.1:8000 \
        --email superadmin@sribeesonline.lk --password 'Admin@123'

Exits 0 when every check passes, 1 otherwise, so a CI step can gate on it.
Nothing is written to the database: every request here is a GET or a login.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: object = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail != "" else ""))
    if not ok:
        FAILURES.append(f"{label}: {detail}")
    return ok


def request(url: str, *, token: str | None = None, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return e.code, {"raw": body[:400].decode(errors="replace")}
    except Exception as e:  # network, DNS, timeout
        return 0, {"error": f"{type(e).__name__}: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    api = f"{base}/api/v1"

    print("Health:")
    status, body = request(f"{base}/health")
    if not check("service responds 200", status == 200, status or body):
        # Nothing downstream can pass if the service is down; report the one
        # real cause rather than a cascade of consequences.
        print("\nService is unreachable — skipping the rest.")
        return 1

    print("\nAuthentication:")
    status, body = request(
        f"{api}/admin/auth/login", payload={"email": args.email, "password": args.password}
    )
    ok = check("seeded admin can log in", status == 200, status if status != 200 else "")
    if not ok:
        check(
            "admin seed applied",
            False,
            "login failed — check that migrations/026_seed_full_dataset.sql ran",
        )
        return 1

    token = (body.get("data") or {}).get("token")
    if not check("login returned a token", bool(token)):
        return 1

    role = ((body.get("data") or {}).get("admin") or {}).get("role")
    check("account is a super admin", str(role).lower().endswith("super_admin"), role)

    print("\nAnalytics endpoints:")
    results: dict[str, dict] = {}
    for path, params in [
        ("summary", "?days=30"),
        ("sales", "?days=30&compare=true"),
        ("branches", "?days=30"),
        ("top-products", "?days=30&limit=5"),
        ("top-customers", "?days=30&limit=5"),
        ("categories", "?days=30"),
        ("order-status", "?days=30"),
    ]:
        status, body = request(f"{api}/admin/analytics/{path}{params}", token=token)
        if check(f"/{path} responds 200", status == 200, status if status != 200 else ""):
            results[path] = body.get("data") or {}

    if len(results) < 7:
        return 1

    print("\nThe data is real:")
    summary = results["summary"]
    current = summary.get("current") or {}
    revenue = float(current.get("revenue") or 0)
    orders = int(current.get("orders") or 0)

    # An empty database answers every request above with a cheerful 200 and a
    # screen full of zeros. This is the check that tells the two apart.
    #
    # A store that has just launched has no orders yet either, and that is not
    # a fault. Then the catalog carries the "right database?" question: a
    # wrong or wiped database has no products, a freshly launched store does.
    has_sales = orders > 0
    if has_sales:
        check("revenue is non-zero", revenue > 0, revenue)
        check("customers is non-zero", int(current.get("customers") or 0) > 0)
    else:
        print("  [SKIP] no orders in the last 30 days: sales checks skipped, catalog checked instead")
        status, body = request(f"{api}/admin/products?limit=1", token=token)
        total = int(((body.get("data") or {}).get("pagination") or {}).get("total") or 0)
        check("the catalog has products", status == 200 and total > 0, total)

    branches = results["branches"].get("branches") or []
    check("branches are configured", len(branches) > 0, len(branches))
    check(
        "every branch row carries a name and code",
        all(b.get("name") and b.get("code") for b in branches),
    )

    print("\nThe numbers agree with each other:")
    # Same failure mode the UI guards against: two endpoints computing the same
    # figure with different queries must not disagree, or nobody can tell which
    # screen is lying.
    branch_total = sum(float(b.get("revenue") or 0) for b in branches)
    unassigned = results["branches"].get("unassigned") or {}
    branch_total += float(unassigned.get("revenue") or 0)
    check(
        "branch revenues sum to the summary revenue",
        abs(branch_total - revenue) < 1.0,
        f"branches={branch_total} summary={revenue}",
    )

    series = results["sales"].get("series") or []
    check("the sales series is 30 points", len(series) == 30, len(series))
    check(
        "the series sums to the summary revenue",
        abs(sum(float(p.get("revenue") or 0) for p in series) - revenue) < 1.0,
        f"series={sum(float(p.get('revenue') or 0) for p in series)} summary={revenue}",
    )

    statuses = results["order-status"]
    check(
        "order statuses total the order count",
        int(statuses.get("total_orders") or 0) == orders,
        f"statuses={statuses.get('total_orders')} summary={orders}",
    )

    categories = results["categories"].get("categories") or []
    if has_sales:
        check("the category mix has rows", len(categories) > 0, len(categories))
        check("top products returned rows", len(results["top-products"].get("products") or []) > 0)
        check("top customers returned rows", len(results["top-customers"].get("customers") or []) > 0)
    if categories:
        share = sum(float(c.get("share") or 0) for c in categories)
        check("category shares total ~100%", 98.0 <= share <= 102.0, share)

    print()
    print("=" * 66)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
