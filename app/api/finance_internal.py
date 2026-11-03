"""
Internal: the event feed Sribees Finance pulls (``GET /internal/finance/events``).

Not for browsers, apps or partners. Three locks, all required:

* **Service token.** ``Authorization: Bearer <token>``; only its SHA-256 is in
  our settings (``FINANCE_SERVICE_TOKEN_SHA256``), compared in constant time.
  No token configured = the route answers 404, as if it did not exist.
* **Never through the edge.** Finance calls the app container over the internal
  Docker network. A request that came through Caddy carries
  ``X-Forwarded-For``; such a request is refused whatever token it has.
* **IP allow-list** (``FINANCE_ALLOWED_IPS``, comma-separated addresses or
  CIDRs) on the connecting peer, when set.

Reads only. Rows come back in ``seq`` order after the caller's cursor. A
transaction that commits late can leave a lower ``seq`` appear after a higher
one; Finance keeps a cursor that only advances past gaps that have stayed
gaps for a while, and dedupes on ``event_id``, so nothing is skipped or
counted twice.
"""
import hashlib
import hmac
import ipaddress
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.models.branch import Branch
from app.models.category import Category
from app.models.finance_outbox import FinanceOutbox, FinanceStockLevel
from app.models.order import Order, OrderItem
from app.models.partner_wallet import (
    PartnerWallet,
    PartnerWalletEntry,
    PartnerWithdrawal,
)
from app.models.payment import PaymentAttempt, PaymentAttemptStatus
from app.models.product import (
    BranchInventory,
    BranchVariantInventory,
    Product,
    ProductVariant,
)
from app.models.referral import ReferralCommission, ReferralCommissionStatus
from app.models.wallet import Wallet, WalletTransaction

router = APIRouter(prefix="/internal/finance", tags=["Internal: Finance"], include_in_schema=False)


def _allowed(peer: str) -> bool:
    raw = (settings.finance_allowed_ips or "").strip()
    if not raw:
        return True
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for part in raw.split(","):
        part = part.strip()
        if part and ip in ipaddress.ip_network(part, strict=False):
            return True
    return False


def require_finance_service(request: Request) -> None:
    expected = (settings.finance_service_token_sha256 or "").strip().lower()
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if request.headers.get("x-forwarded-for") or request.headers.get("forwarded"):
        raise HTTPException(status_code=403, detail="not available from outside")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    presented = hashlib.sha256(token.encode()).hexdigest() if token else ""
    if not token or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="service token required")
    peer = request.client.host if request.client else ""
    if not _allowed(peer):
        raise HTTPException(status_code=403, detail="caller not allowed")


@router.get("/events")
async def finance_events(
    after: int = Query(default=0, ge=0, description="return events with seq greater than this"),
    limit: int = Query(default=500, ge=1, le=2000),
    _: None = Depends(require_finance_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(
            select(FinanceOutbox).where(FinanceOutbox.seq > after).order_by(FinanceOutbox.seq).limit(limit)
        )
    ).scalars().all()
    return {
        "events": [
            {
                "seq": r.seq,
                "event_id": str(r.event_id),
                "type": r.event_type,
                "aggregate_id": str(r.aggregate_id) if r.aggregate_id else None,
                "occurred_at": r.occurred_at.isoformat(),
                "payload": r.payload,
            }
            for r in rows
        ],
        "enabled": bool(settings.finance_outbox_enabled),
    }


COLOMBO = ZoneInfo("Asia/Colombo")


@router.get("/totals")
async def finance_totals(
    day: date = Query(description="the business day in Sri Lanka"),
    _: None = Depends(require_finance_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What Online says happened on one Sri Lankan day, per branch, for Finance's
    daily reconciliation: delivered sales and their COD part, card money in, and
    the net wallet movement. Read straight from the source tables, not the outbox,
    so an event that never reached the outbox still shows up as a difference."""
    start = datetime.combine(day, time.min, tzinfo=COLOMBO)
    end = start + timedelta(days=1)
    rows: dict[str, dict] = defaultdict(lambda: {
        "delivered_count": 0, "delivered_total": Decimal("0"), "cod": Decimal("0"),
        "card_received": Decimal("0"), "wallet_net": Decimal("0"), "wallet_count": 0,
    })

    def key(branch_id) -> str:
        return str(branch_id) if branch_id else "none"

    delivered = await db.execute(
        select(Order.branch_id, Order.payment_method, Order.total_amount, Order.wallet_deduction, Order.referral_delivery_discount)
        .where(Order.delivered_at >= start, Order.delivered_at < end)
    )
    for branch_id, method, total, wallet, subsidy in delivered:
        r = rows[key(branch_id)]
        r["delivered_count"] += 1
        # total_amount is what was left to pay after the wallet; the order's value
        # is that plus the wallet part, and on a COD order the rider collects the total.
        r["delivered_total"] += (total or 0) + (wallet or 0)
        if subsidy:
            r["referral_delivery_subsidy"] = r.get("referral_delivery_subsidy", Decimal("0")) + subsidy
        if (method or "").upper() == "COD":
            r["cod"] += total or 0

    paid = await db.execute(
        select(Order.branch_id, func.sum(PaymentAttempt.amount))
        .join(Order, Order.order_id == PaymentAttempt.order_id)
        .where(PaymentAttempt.status == PaymentAttemptStatus.PAID.value,
               PaymentAttempt.completed_at >= start, PaymentAttempt.completed_at < end)
        .group_by(Order.branch_id)
    )
    for branch_id, amount in paid:
        rows[key(branch_id)]["card_received"] += amount or 0

    moves = await db.execute(
        select(Order.branch_id, WalletTransaction.type, func.sum(WalletTransaction.amount), func.count())
        .outerjoin(Order, Order.order_id == WalletTransaction.order_id)
        .where(WalletTransaction.created_at >= start, WalletTransaction.created_at < end)
        .group_by(Order.branch_id, WalletTransaction.type)
    )
    for branch_id, kind, amount, n in moves:
        r = rows[key(branch_id)]
        r["wallet_net"] += -(amount or 0) if kind == "spent" else (amount or 0)
        r["wallet_count"] += n

    partner_moves = (await db.execute(select(func.sum(PartnerWalletEntry.amount), func.count())
        .where(PartnerWalletEntry.created_at >= start, PartnerWalletEntry.created_at < end))).one()
    if partner_moves[1]:
        rows["none"]["partner_wallet_net"] = partner_moves[0]
        rows["none"]["partner_wallet_count"] = partner_moves[1]
    partner_paid = (await db.execute(select(func.sum(PartnerWithdrawal.amount), func.count())
        .where(PartnerWithdrawal.status == "paid", PartnerWithdrawal.decided_at >= start,
            PartnerWithdrawal.decided_at < end))).one()
    if partner_paid[1]:
        rows["none"]["partner_withdrawal_paid"] = partner_paid[0]
        rows["none"]["partner_withdrawal_count"] = partner_paid[1]

    return {
        "day": day.isoformat(),
        "branches": {
            b: {k: (f"{v:.2f}" if isinstance(v, Decimal) else v) for k, v in r.items()}
            for b, r in rows.items()
        },
    }


DISPATCHED = {"handed_to_courier", "shipped", "out_for_delivery", "delivery_failed", "rto_initiated"}
ADVANCE_IN = {"wallet_payment"}
ADVANCE_OUT = {"cancel_refund", "payment_refund"}
RECENT_DELIVERIES = timedelta(days=30)


@router.get("/opening-snapshot")
async def finance_opening_snapshot(
    cutoff: date = Query(description="Online's real start; older orders were tests"),
    _: None = Depends(require_finance_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Online's balances at go-live, for Finance's opening entry, and the outbox
    sequence they correspond to.

    Read in one REPEATABLE READ snapshot, so the balances and ``seq`` agree:
    every event up to ``seq`` that this snapshot can see is already in the
    figures. ``missing`` lists sequence numbers at or below ``seq`` that it
    cannot see (a transaction still running, or rolled back); Finance treats
    them as gaps and books any that do arrive.

    Orders: each one not yet delivered or cancelled, each with money paid in
    and not refunded (Finance's "advance"), and each delivered in the last 30
    days (so a return finds it). The advance is the same sum Finance keeps from
    events: paid gateway tries, plus wallet paid in, less wallet refunded on
    cancel or late payment.
    """
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    since = datetime.combine(cutoff, time.min, tzinfo=COLOMBO)
    seq = (await db.execute(select(func.coalesce(func.max(FinanceOutbox.seq), 0)))).scalar_one()
    recent = dict((await db.execute(
        select(FinanceOutbox.seq, FinanceOutbox.event_id).where(FinanceOutbox.seq > seq - 5000))).all())
    missing = [n for n in range(max(seq - 5000, 0) + 1, seq + 1) if n not in recent]
    # Finance's cursor must start below the first gap; these are already in the figures.
    counted = [{"seq": n, "event_id": str(e)} for n, e in sorted(recent.items())
               if missing and n > missing[0]]
    now = (await db.execute(select(func.now()))).scalar_one()

    wallets = (await db.execute(select(func.coalesce(func.sum(Wallet.balance), 0), func.count())
                                .where(Wallet.balance != 0))).one()
    partner = (await db.execute(select(func.coalesce(func.sum(PartnerWallet.balance), 0),
                                       func.count()).where(PartnerWallet.balance != 0))).one()

    paid = dict((await db.execute(
        select(PaymentAttempt.order_id, func.sum(PaymentAttempt.amount))
        .join(Order, Order.order_id == PaymentAttempt.order_id)
        .where(PaymentAttempt.status == PaymentAttemptStatus.PAID.value, Order.created_at >= since)
        .group_by(PaymentAttempt.order_id))).all())
    wallet_net: dict = defaultdict(Decimal)
    for order_id, reason, kind, delivered, amount in await db.execute(
        select(WalletTransaction.order_id, WalletTransaction.reason, WalletTransaction.type,
               Order.delivered_at.is_not(None), WalletTransaction.amount)
        .join(Order, Order.order_id == WalletTransaction.order_id)
        .where(Order.created_at >= since)
    ):
        # Rows from before reasons were recorded fall back to their direction.
        if reason in ADVANCE_IN or (reason is None and kind == "spent"):
            wallet_net[order_id] += amount or 0
        elif reason in ADVANCE_OUT or (reason is None and kind == "refund" and not delivered):
            wallet_net[order_id] -= amount or 0

    orders = []
    for o in (await db.execute(select(Order).where(Order.created_at >= since))).scalars():
        advance = Decimal(paid.get(o.order_id) or 0) + wallet_net.get(o.order_id, Decimal("0"))
        delivered = o.delivered_at is not None
        open_ = not delivered and o.status not in ("cancelled", "refunded")
        recent = delivered and o.delivered_at >= now - RECENT_DELIVERIES
        if not (open_ or recent or advance != 0):
            continue
        orders.append({
            "order_id": str(o.order_id), "order_number": o.order_number,
            "branch_id": str(o.branch_id) if o.branch_id else None, "status": o.status,
            "payment_method": o.payment_method, "total": f"{(o.total_amount or 0):.2f}",
            "advance": f"{advance:.2f}", "delivered": delivered,
            "dispatched": delivered or o.status in DISPATCHED,
        })
    older_open = (await db.execute(select(func.count()).select_from(Order).where(
        Order.created_at < since, Order.delivered_at.is_(None),
        Order.status.notin_(("cancelled", "refunded"))))).scalar_one()

    commissions = [
        {"branch_id": str(b) if b else None,
         "beneficiary": "partner" if is_partner else "customer",
         "amount": f"{amount:.2f}", "count": n}
        for b, is_partner, amount, n in await db.execute(
            select(Order.branch_id, ReferralCommission.beneficiary_partner_id.is_not(None),
                   func.sum(ReferralCommission.amount), func.count())
            .outerjoin(Order, Order.order_id == ReferralCommission.order_id)
            .where(ReferralCommission.status == ReferralCommissionStatus.CLAIMABLE)
            .group_by(Order.branch_id, ReferralCommission.beneficiary_partner_id.is_not(None)))
    ]
    return {
        "as_of": now.isoformat(),
        "seq": seq,
        "missing": missing,
        "counted_after_gap": counted,
        "branches": [{"branch_id": str(b.branch_id), "code": b.code, "name": b.name}
                     for b in (await db.execute(select(Branch))).scalars()],
        "outbox_enabled": bool(settings.finance_outbox_enabled),
        "customer_wallets": {"total": f"{wallets[0]:.2f}", "count": wallets[1]},
        "partner_wallets": {"total": f"{partner[0]:.2f}", "count": partner[1]},
        "claimable_commissions": commissions,
        "orders": orders,
        "open_orders_before_cutoff": older_open,
    }


# Placed (stock already taken off branch_inventory) but the goods have not left yet.
PLACED_NOT_SENT = ("awaiting_payment", "pending", "confirmed", "processing", "packing", "packed",
                   "ready_for_pickup")


async def _committed(db: AsyncSession) -> dict[tuple, int]:
    """Placed but not yet dispatched, per (branch, stock item), in Online's own measure:
    units, grams of a loose product, packs of a pre-packed size."""
    out: dict[tuple, int] = defaultdict(int)
    for b, p, v, mode, grams, qty in await db.execute(
            select(Order.branch_id, OrderItem.product_id, OrderItem.variant_id, Product.stock_mode,
                   ProductVariant.weight_grams, func.sum(OrderItem.quantity))
            .join(Order, Order.order_id == OrderItem.order_id)
            .join(Product, Product.product_id == OrderItem.product_id)
            .outerjoin(ProductVariant, ProductVariant.variant_id == OrderItem.variant_id)
            .where(Order.status.in_(PLACED_NOT_SENT), Order.branch_id.is_not(None))
            .group_by(Order.branch_id, OrderItem.product_id, OrderItem.variant_id, Product.stock_mode,
                      ProductVariant.weight_grams)):
        if mode == "packed" and v is not None and grams:
            out[(b, v)] += int(qty)
        elif mode == "loose" and grams:
            out[(b, p)] += int(qty) * int(grams)
        else:
            out[(b, p)] += int(qty)
    return out


def _in_finance_unit(value: int, mode: str) -> str:
    """Online keeps loose goods in grams; Finance in kilograms."""
    return f"{Decimal(value) / 1000:.3f}" if mode == "loose" else str(int(value))


@router.get("/stock-compare")
async def finance_stock_compare(
    _: None = Depends(require_finance_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """For Finance's parallel run: per branch and stock item (a product, or a pack size
    of a pre-packed product), the shop's stock, what is placed but not yet dispatched
    (Online took it off at placing, Finance takes it off at dispatch), and the sellable
    quantity Finance last pushed -- all in Finance's measure (kg for loose goods)."""
    now = (await db.execute(select(func.now()))).scalar_one()
    rows: dict[tuple, dict] = {}
    modes = dict((await db.execute(select(Product.product_id, Product.stock_mode))).all())

    def row(branch_id, item_id, product_id, variant_id=None) -> dict:
        return rows.setdefault((branch_id, item_id), {
            "branch_id": str(branch_id), "product_id": str(product_id),
            "variant_id": str(variant_id) if variant_id else None, "online_raw": 0, "committed_raw": 0,
            "finance_qty": None, "finance_unit": None, "finance_at": None,
            "stock_mode": modes.get(product_id, "unit")})

    for b, p, qty, grams in await db.execute(
            select(BranchInventory.branch_id, BranchInventory.product_id, BranchInventory.stock_quantity,
                   BranchInventory.stock_grams)):
        if modes.get(p) == "loose":
            row(b, p, p)["online_raw"] = int(grams or 0)
        elif modes.get(p) != "packed":
            row(b, p, p)["online_raw"] = int(qty or 0)
    for b, v, p, qty in await db.execute(
            select(BranchVariantInventory.branch_id, BranchVariantInventory.variant_id,
                   ProductVariant.product_id, BranchVariantInventory.stock_quantity)
            .join(ProductVariant, ProductVariant.variant_id == BranchVariantInventory.variant_id)):
        if modes.get(p) == "packed":
            row(b, v, p, v)["online_raw"] = int(qty or 0)
    variant_parent = dict((await db.execute(select(ProductVariant.variant_id, ProductVariant.product_id))).all())
    for (b, item_id), qty in (await _committed(db)).items():
        parent = variant_parent.get(item_id)
        r = row(b, item_id, parent or item_id, item_id if parent else None)
        r["committed_raw"] = qty
    for lv in (await db.execute(select(FinanceStockLevel))).scalars():
        r = row(lv.branch_id, lv.item_id, lv.product_id, lv.variant_id)
        r.update(finance_qty=f"{lv.sellable_qty:f}", finance_unit=lv.unit,
                 finance_at=lv.received_at.isoformat())
    ids = {uuid.UUID(r["product_id"]) for r in rows.values()}
    names = {pid: (name, sku) for pid, name, sku in await db.execute(
        select(Product.product_id, Product.name, Product.sku).where(Product.product_id.in_(ids)))} if ids else {}
    vnames = {vid: (name, sku) for vid, name, sku in await db.execute(
        select(ProductVariant.variant_id, ProductVariant.name, ProductVariant.sku))}
    for r in rows.values():
        name, sku = names.get(uuid.UUID(r["product_id"]), (None, None))
        if r["variant_id"]:
            vname, vsku = vnames.get(uuid.UUID(r["variant_id"]), ("", None))
            name, sku = f"{name} {vname}".strip(), vsku or sku
        r["name"], r["sku"] = name, sku
        r["online_stock"] = _in_finance_unit(r.pop("online_raw"), r["stock_mode"])
        r["committed"] = _in_finance_unit(r.pop("committed_raw"), r["stock_mode"])
    return {"as_of": now.isoformat(), "rows": list(rows.values())}


@router.get("/catalog")
async def finance_catalog(
    _: None = Depends(require_finance_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Every product with how it keeps stock, its cost price and its weight options, so
    Finance's product list (and the opening count) uses Online's ids."""
    variants: dict = defaultdict(list)
    for v in (await db.execute(select(ProductVariant).order_by(ProductVariant.sort_order))).scalars():
        variants[v.product_id].append({
            "variant_id": str(v.variant_id), "sku": v.sku, "name": v.name,
            "weight_grams": v.weight_grams, "is_active": bool(v.is_active)})
    categories = dict((await db.execute(select(Category.category_id, Category.name))).all())
    return {"products": [
        {"product_id": str(p.product_id), "sku": p.sku, "name": p.name,
         "category": categories.get(p.category_id), "stock_mode": p.stock_mode or "unit",
         "cost_price": f"{p.cost_price:.2f}" if p.cost_price is not None else None,
         "is_active": bool(p.is_active), "variants": variants.get(p.product_id, [])}
        for p in (await db.execute(select(Product))).scalars()]}


class StockLevelIn(BaseModel):
    branch_id: str = Field(min_length=36, max_length=36)
    product_id: str = Field(min_length=36, max_length=36)
    # set for a pack size of a pre-packed product
    variant_id: str | None = Field(default=None, min_length=36, max_length=36)
    sellable_qty: Decimal = Field(ge=0, max_digits=14, decimal_places=3)
    unit: str = Field(pattern="^(each|kg)$")
    version: int = Field(ge=0)


class StockLevelsIn(BaseModel):
    levels: list[StockLevelIn] = Field(max_length=5000)
    # the owner's half of the stock switch, as Sribees Finance holds it
    authority: bool = False


@router.put("/stock-levels")
async def finance_stock_levels(
    body: StockLevelsIn,
    _: None = Depends(require_finance_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record Finance's sellable quantities. A push older than what is stored for that
    branch and item is ignored. Only when BOTH halves of the switch are on (Finance's
    owner, sent as ``authority``, and our FINANCE_STOCK_AUTHORITY) does a stored figure
    also set the shop's stock: Finance's sellable less what is placed and not yet
    dispatched, in our measure (grams for loose goods), under checkout's row lock."""
    from app.services import stock_keeping

    stored = applied = 0
    apply = bool(body.authority and settings.finance_stock_authority)
    committed = await _committed(db) if apply else {}
    for lv in body.levels:
        item_id = lv.variant_id or lv.product_id
        stmt = insert(FinanceStockLevel).values(
            branch_id=lv.branch_id, item_id=item_id, product_id=lv.product_id,
            variant_id=lv.variant_id, sellable_qty=lv.sellable_qty, unit=lv.unit,
            version=lv.version, received_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[FinanceStockLevel.branch_id, FinanceStockLevel.item_id],
            set_={"sellable_qty": stmt.excluded.sellable_qty, "unit": stmt.excluded.unit,
                  "product_id": stmt.excluded.product_id, "variant_id": stmt.excluded.variant_id,
                  "version": stmt.excluded.version, "received_at": func.now()},
            where=FinanceStockLevel.version < stmt.excluded.version,
        )
        won = (await db.execute(stmt)).rowcount or 0
        stored += won
        if not (apply and won):
            continue
        mode = (await db.execute(select(Product.stock_mode).where(
            Product.product_id == uuid.UUID(lv.product_id)))).scalar_one_or_none() or "unit"
        expected = "kg" if mode == "loose" else "each"
        if lv.unit != expected or (mode == "packed") != (lv.variant_id is not None):
            logger.warning(f"finance stock push for {item_id} is {lv.unit}/{lv.variant_id}, "
                           f"but the product is kept {mode}; not applied")
            continue
        measure = int(lv.sellable_qty * 1000) if mode == "loose" else int(lv.sellable_qty)
        branch = uuid.UUID(lv.branch_id)
        item = stock_keeping.Item(mode, uuid.UUID(lv.product_id),
                                  uuid.UUID(lv.variant_id) if lv.variant_id else None, None)
        value = measure - committed.get((branch, uuid.UUID(item_id)), 0)
        if await stock_keeping.set_level(db, branch, item, value):
            applied += 1
    await db.commit()
    return {"received": len(body.levels), "stored": stored, "applied_to_shop": applied}
