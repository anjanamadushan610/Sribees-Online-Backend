"""
Cashback that waits for the return window, points that never go down, and
members-only coupons.

DB-free, in the style of the neighbouring money tests: stand-ins for orders
and a session that answers the one query each path makes.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models.coupon import Coupon
from app.models.loyalty import LoyaltyTier, OrderReward, OrderRewardStatus
from app.models.order import OrderStatus
from app.schemas.loyalty import LoyaltyTierCreate
from app.services.coupon_service import CouponService, CouponValidationError
from app.services.loyalty_service import LoyaltyService, PointsResult
from app.services.order_reward_service import OrderRewardService

D = Decimal


# ---------------------------------------------------------------------------
# Mapping — a fake can't see a mis-mapped column (a804ce6)
# ---------------------------------------------------------------------------


def test_the_new_columns_live_on_the_right_tables():
    assert "min_tier_id" in Coupon.__table__.columns
    assert {"cashback_amount", "vests_at", "points_awarded", "wallet_transaction_id"} <= set(
        OrderReward.__table__.columns.keys()
    )
    assert OrderReward(cashback_amount=D("12.50")).cashback_amount == D("12.50")


def test_the_backfill_and_seed_can_insert_without_an_id():
    # create_all() builds these tables before migration 066 runs its INSERTs,
    # which name no id: the database must be able to make one.
    assert OrderReward.__table__.c.reward_id.server_default is not None
    assert LoyaltyTier.__table__.c.tier_id.server_default is not None


# ---------------------------------------------------------------------------
# Points arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "carry, amount, points, left",
    [
        (D("0"), D("700"), 0, D("700.00")),
        (D("700"), D("700"), 1, D("400.00")),  # the carry is the point of the carry
        (D("0"), D("2500"), 2, D("500.00")),
        (D("0"), D("1000"), 1, D("0.00")),
        (D("300"), D("-50"), 0, D("300.00")),  # a negative amount adds nothing
    ],
)
def test_points_carry_the_rupees_short_of_the_next_point(carry, amount, points, left):
    assert LoyaltyService.points_for_spend(carry, amount, D("1000")) == (points, left)


def test_a_zero_rate_is_refused_rather_than_dividing_by_it():
    with pytest.raises(ValueError):
        LoyaltyService.points_for_spend(D("0"), D("100"), D("0"))


def _tiers():
    return [
        SimpleNamespace(tier_id="m", name="Member", min_points=0),
        SimpleNamespace(tier_id="s", name="Silver", min_points=25),
        SimpleNamespace(tier_id="g", name="Gold", min_points=100),
    ]


@pytest.mark.parametrize("points, current, upcoming", [
    (0, "Member", "Silver"), (24, "Member", "Silver"), (25, "Silver", "Gold"),
    (99, "Silver", "Gold"), (100, "Gold", None), (5000, "Gold", None),
])
def test_the_level_is_the_highest_the_points_reach(points, current, upcoming):
    assert LoyaltyService.tier_for(points, _tiers()).name == current
    nxt = LoyaltyService.next_tier(points, _tiers())
    assert (nxt.name if nxt else None) == upcoming


# ---------------------------------------------------------------------------
# Order value and returned cashback
# ---------------------------------------------------------------------------


def _item(item_id, price, qty, rate=D("1")):
    return SimpleNamespace(
        order_item_id=item_id, unit_price=D(price), quantity=qty, cashback_percentage=rate
    )


def _order(**extra):
    fields = {
        "order_id": "o1", "order_number": "SB-1", "user_id": "u1",
        "status": OrderStatus.DELIVERED.value,
        "subtotal": D("5000"), "discount_amount": D("500"), "tax_amount": D("450"),
        "shipping_amount": D("350"), "refund_amount": None,
        "cashback_earned": D("50.00"), "cashback_credited_at": None,
        "delivered_at": datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc),
        "items": [], "return_items": None,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


def test_points_count_the_goods_not_the_delivery():
    # 5000 - 500 coupon + 450 tax; the 350 delivery charge is not goods.
    assert OrderRewardService.qualifying_amount(_order()) == D("4950.00")


def test_returned_goods_come_off_the_points_value():
    order = _order(status=OrderStatus.REFUNDED.value, refund_amount=D("2000"))
    assert OrderRewardService.qualifying_amount(order) == D("2950.00")


def test_the_points_value_never_goes_negative():
    order = _order(status=OrderStatus.REFUNDED.value, refund_amount=D("9000"))
    assert OrderRewardService.qualifying_amount(order) == D("0.00")


def test_returned_cashback_is_at_each_lines_own_rate():
    items = [_item("a", "1000", 2, D("5")), _item("b", "3000", 1, D("1"))]  # 100 + 30
    order = _order(items=items, cashback_earned=D("130.00"),
                   return_items=[{"order_item_id": "a", "quantity": 1}])
    assert OrderRewardService.returned_cashback(order) == D("50.00")


def test_a_whole_order_return_takes_all_its_cashback():
    items = [_item("a", "1000", 2, D("5")), _item("b", "3000", 1, D("1"))]
    order = _order(items=items, cashback_earned=D("130.00"), return_items=None)
    assert OrderRewardService.returned_cashback(order) == D("130.00")


def test_lines_without_a_frozen_rate_share_the_cashback_by_value():
    # Bought before migration 065: no per-line rate, so split by value.
    items = [_item("a", "1000", 1, None), _item("b", "3000", 1, None)]
    order = _order(items=items, cashback_earned=D("40.00"),
                   return_items=[{"order_item_id": "b", "quantity": 1}])
    assert OrderRewardService.returned_cashback(order) == D("30.00")


# ---------------------------------------------------------------------------
# What the order screen says
# ---------------------------------------------------------------------------


def test_undelivered_cashback_is_pending_with_no_date_yet():
    state = OrderRewardService.order_cashback_state(
        _order(status=OrderStatus.PACKING.value, delivered_at=None)
    )
    assert state == {"cashback_status": "pending", "cashback_available_at": None}


def test_delivered_cashback_arrives_a_minute_after_the_return_window():
    state = OrderRewardService.order_cashback_state(_order())
    expected = datetime(2026, 9, 24, 20, 1, tzinfo=timezone.utc)
    assert state["cashback_status"] == "pending"
    assert datetime.fromisoformat(state["cashback_available_at"]) == expected


def test_an_open_return_gives_no_date():
    state = OrderRewardService.order_cashback_state(
        _order(status=OrderStatus.RETURN_REQUESTED.value)
    )
    assert state == {"cashback_status": "pending", "cashback_available_at": None}


def test_credited_and_cancelled_orders_say_so():
    credited = _order(cashback_credited_at=datetime.now(timezone.utc))
    cancelled = _order(status=OrderStatus.CANCELLED.value)
    assert OrderRewardService.order_cashback_state(credited)["cashback_status"] == "credited"
    assert OrderRewardService.order_cashback_state(cancelled)["cashback_status"] == "reversed"


# ---------------------------------------------------------------------------
# Money movements
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _Session:
    """Answers every query with the one reward row the path looks up."""

    def __init__(self, row=None):
        self.row = row

    async def execute(self, _stmt):
        return _Result(self.row)

    async def flush(self):
        return None


@pytest.fixture
def ledger(monkeypatch):
    moves = []

    async def _apply(db, **kwargs):
        moves.append((kwargs["tx_type"].value, kwargs["amount"], kwargs["title"]))
        return SimpleNamespace(transaction_id="tx-1")

    from app.services import order_reward_service

    monkeypatch.setattr(order_reward_service.WalletService, "apply_transaction", _apply)
    return moves


def _reward(**extra):
    fields = {
        "order_id": "o1", "user_id": "u1", "cashback_amount": D("50.00"),
        "status": OrderRewardStatus.PENDING, "wallet_transaction_id": None,
        "qualifying_amount": None, "points_awarded": 0, "settled_at": None,
        "reversed_at": None,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


@pytest.fixture
def points(monkeypatch):
    added = []

    async def _add_spend(db, user_id, amount):
        added.append(amount)
        return PointsResult(points_added=4, lifetime_points=4, tiers_reached=[])

    async def _issue(db, user_id, pts):
        return []

    monkeypatch.setattr(LoyaltyService, "add_spend", _add_spend)
    monkeypatch.setattr(LoyaltyService, "issue_level_rewards", _issue)
    return added


@pytest.mark.asyncio
async def test_settling_credits_the_cashback_once_and_adds_the_points(ledger, points):
    order, reward = _order(), _reward()
    settled = await OrderRewardService._settle(_Session(), reward, order)

    assert ledger == [("earned", D("50.00"), "Order cashback")]
    assert order.cashback_credited_at is not None
    assert reward.status == OrderRewardStatus.SETTLED
    assert reward.wallet_transaction_id == "tx-1"
    assert points == [D("4950.00")]
    assert (settled.cashback_credited, settled.points_added) == (D("50.00"), 4)


@pytest.mark.asyncio
async def test_cashback_credited_at_checkout_is_not_paid_again(ledger, points):
    # An order from before rewards were held: its cashback went in at checkout.
    order = _order(cashback_credited_at=datetime(2026, 9, 23, tzinfo=timezone.utc))
    reward = _reward()
    settled = await OrderRewardService._settle(_Session(), reward, order)

    assert ledger == []
    assert settled.cashback_credited == D("0.00")
    assert points == [D("4950.00")]  # it still earns its points
    assert reward.status == OrderRewardStatus.SETTLED


@pytest.mark.asyncio
async def test_cancelling_before_settlement_moves_no_money(ledger):
    reward = _reward()
    await OrderRewardService.reverse_for_order(_Session(reward), _order(), reason="test")
    assert ledger == []
    assert reward.status == OrderRewardStatus.REVERSED


@pytest.mark.asyncio
async def test_cancelling_after_credit_claws_back_what_was_credited(ledger):
    order = _order(cashback_credited_at=datetime(2026, 9, 23, tzinfo=timezone.utc))
    await OrderRewardService.reverse_for_order(_Session(_reward()), order, reason="test")
    assert ledger == [("spent", D("50.00"), "Order cancellation cashback reversal")]


@pytest.mark.asyncio
async def test_a_return_lowers_held_cashback_without_touching_the_wallet(ledger):
    items = [_item("a", "1000", 2, D("5")), _item("b", "3000", 1, D("1"))]
    order = _order(items=items, cashback_earned=D("130.00"),
                   return_items=[{"order_item_id": "a", "quantity": 1}])
    reward = _reward(cashback_amount=D("130.00"))

    taken = await OrderRewardService.reduce_for_return(_Session(reward), order)

    assert taken == D("50.00")
    assert reward.cashback_amount == D("80.00")
    assert order.cashback_earned == D("80.00")
    assert ledger == []


@pytest.mark.asyncio
async def test_a_return_on_credited_cashback_takes_back_only_the_difference(ledger):
    items = [_item("a", "1000", 2, D("5")), _item("b", "3000", 1, D("1"))]
    order = _order(items=items, cashback_earned=D("130.00"),
                   cashback_credited_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
                   return_items=[{"order_item_id": "a", "quantity": 1}])

    await OrderRewardService.reduce_for_return(
        _Session(_reward(cashback_amount=D("130.00"))), order
    )
    assert ledger == [("spent", D("50.00"), "Cashback adjusted — items returned")]


# ---------------------------------------------------------------------------
# Members-only coupons
# ---------------------------------------------------------------------------


def _coupon(min_tier=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        is_budget_depleted=False, is_active=True, branch_id=None,
        valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=1),
        target_user_id=None, usage_limit=None, used_count=0, per_user_limit=None,
        first_order_only=False, min_order_value=D("0"),
        min_tier_id=min_tier.tier_id if min_tier else None, min_tier=min_tier,
    )


GOLD = SimpleNamespace(tier_id="g", name="Gold", min_points=100)


@pytest.mark.parametrize("pts", [None, 0, 99])
def test_a_members_only_coupon_refuses_anyone_below_the_level(pts):
    with pytest.raises(CouponValidationError, match="Gold members"):
        CouponService.validate(_coupon(GOLD), D("5000"), user_points=pts)


@pytest.mark.parametrize("pts", [100, 400])
def test_members_at_or_above_the_level_may_use_it(pts):
    CouponService.validate(_coupon(GOLD), D("5000"), user_points=pts)


def test_an_ordinary_coupon_does_not_ask_for_points():
    CouponService.validate(_coupon(), D("5000"))


# ---------------------------------------------------------------------------
# Level rewards
# ---------------------------------------------------------------------------


def test_a_fixed_reward_bigger_than_its_minimum_order_is_refused():
    with pytest.raises(ValidationError, match="Minimum order"):
        LoyaltyTierCreate(name="Gold", min_points=100, reward_discount_type="fixed",
                          reward_discount_value=D("500"), reward_min_order_value=D("200"),
                          reward_valid_days=30)


def test_a_reward_needs_its_value_and_lifetime():
    with pytest.raises(ValidationError, match="discount value"):
        LoyaltyTierCreate(name="Gold", min_points=100, reward_discount_type="percentage")


def test_a_percentage_reward_is_a_percentage():
    with pytest.raises(ValidationError):
        LoyaltyTierCreate(name="Gold", min_points=100, reward_discount_type="percentage",
                          reward_discount_value=D("150"), reward_valid_days=30)


def test_a_level_without_a_reward_is_fine():
    assert LoyaltyTierCreate(name="Member", min_points=0).reward_discount_type is None
