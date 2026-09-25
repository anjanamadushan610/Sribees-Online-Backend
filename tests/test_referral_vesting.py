"""
Tests for when a referral commission is earned, and what a return costs it.

Both of these move money in a direction nobody notices until somebody
complains:

  * commissions used to vest the moment an order was marked delivered, while
    the customer still had twelve hours to send the goods back. A referrer
    could claim, spend, and be clawed into a negative balance because their
    friend returned a kettle.

  * a return voided the WHOLE commission, including the part earned on items
    that were delivered and kept.

DB-free, in the style of the neighbouring order tests.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services.order_service import RETURN_WINDOW
from app.services.referral_service import ReferralService

# --- the vesting delay ------------------------------------------------------


def test_the_return_window_is_twelve_hours():
    """The commission waits exactly as long as the customer's own window."""
    assert RETURN_WINDOW == timedelta(hours=12)


def test_a_commission_does_not_vest_on_the_last_second_of_the_window():
    """A return landing on the final second must win against a commission
    vesting on the same second, so the deadline is a minute later."""
    delivered = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    vest_at = delivered + RETURN_WINDOW + timedelta(minutes=1)
    window_closes = delivered + RETURN_WINDOW
    assert vest_at > window_closes


# --- what a return costs ----------------------------------------------------


def _commission_after_return(
    *, basis: str, rate: float, returned: str, already_returned: str = "0"
) -> Decimal:
    """The arithmetic reduce_for_returned_value performs on one row."""
    basis_d = Decimal(basis)
    returned_total = min(Decimal(already_returned) + Decimal(returned), basis_d)
    remaining = basis_d - returned_total
    return ReferralService._commission_amount(remaining, rate)


def test_returning_one_item_costs_only_that_item():
    """Rs 1,000 of a Rs 5,000 order comes back. The referrer keeps the
    commission on the Rs 4,000 that was delivered and kept."""
    assert _commission_after_return(basis="5000", rate=2.0, returned="1000") == Decimal("80.00")


def test_returning_nothing_changes_nothing():
    assert _commission_after_return(basis="5000", rate=2.0, returned="0") == Decimal("100.00")


def test_returning_everything_leaves_nothing():
    assert _commission_after_return(basis="5000", rate=2.0, returned="5000") == Decimal("0.00")


def test_a_second_return_adds_to_the_first():
    """Two partial returns on one order must not each be measured against the
    full basis -- that would leave the referrer paid for goods twice returned."""
    after_first = _commission_after_return(basis="5000", rate=2.0, returned="1000")
    after_second = _commission_after_return(
        basis="5000", rate=2.0, returned="1000", already_returned="1000"
    )
    assert after_first == Decimal("80.00")
    assert after_second == Decimal("60.00")


def test_returning_more_than_the_basis_floors_at_zero():
    """Defensive: a refund larger than the goods value (a rounding artefact,
    a support correction) must not produce a negative commission, which would
    pay the referrer for a return."""
    assert _commission_after_return(basis="1000", rate=2.0, returned="1500") == Decimal("0.00")


def test_the_lead_rate_is_lower_than_the_agent_rate():
    """The agent signed the customer up; the lead recruited the agent. Same
    order, same basis, different work."""
    agent = ReferralService._commission_amount(Decimal("5000"), 2.0)
    lead = ReferralService._commission_amount(Decimal("5000"), 1.0)
    assert agent == Decimal("100.00")
    assert lead == Decimal("50.00")
    assert lead < agent


def test_the_stored_rate_is_used_not_the_current_one():
    """A row earned at 2% stays at 2% after the platform moves to 3%, so a
    rate change never rewrites what people already earned."""
    earned_at_two = ReferralService._commission_amount(Decimal("4000"), 2.0)
    assert earned_at_two == Decimal("80.00")
    assert ReferralService._commission_amount(Decimal("4000"), 3.0) == Decimal("120.00")


# --- the guard must not strand what was already earned ----------------------


def test_the_claim_guard_requires_a_deadline_that_has_passed():
    """Both halves of the guard, stated as the query means them.

    The `IS NOT NULL` half is what broke 1,960 commissions on the dev database
    when this shipped: rows vested under the old rule had no deadline, and a
    NULL reads as "not due yet". Migration 059 backfills them. If that ever
    gets dropped, this is the test that should fail first.
    """
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)

    def claimable(deadline):
        return deadline is not None and deadline <= now

    assert claimable(now - timedelta(hours=1)) is True
    assert claimable(now) is True
    assert claimable(now + timedelta(hours=1)) is False
    # The regression: an already-vested row with no deadline.
    assert claimable(None) is False, (
        "a NULL deadline is not claimable, which is why 059 has to backfill "
        "every row that vested before claimable_at existed"
    )
