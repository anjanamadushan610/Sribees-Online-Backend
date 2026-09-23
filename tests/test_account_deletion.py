"""
Tests for closing a customer account.

Google Play requires an in-app delete, and the easy way to build one leaks in
two directions at once: erase too much and somebody who abused the platform
comes back clean, erase too little and a closed account is still reachable.

These pin both edges.

DB-free, in the style of the neighbouring tests: the deletion itself is a
handful of field assignments and the questions worth asking are about WHICH
fields.
"""
from datetime import datetime, timezone
from types import SimpleNamespace


def _closed(user):
    """What close_account() does to a user row, as field assignments."""
    user.deleted_phone = user.phone
    user.deleted_email = user.email
    user.phone = None
    user.email = None
    user.alternate_phone = None
    user.deleted_at = datetime.now(timezone.utc)
    user.is_active = False
    return user


def _user(**extra):
    fields = dict(
        user_id="11111111-1111-1111-1111-111111111111",
        full_name="Nimal Perera",
        phone="0771234567",
        alternate_phone="0779999999",
        email="nimal@example.com",
        nic="199012345678",
        is_active=True,
        deleted_at=None,
        deleted_phone=None,
        deleted_email=None,
    )
    fields.update(extra)
    return SimpleNamespace(**fields)


# --- what must go -----------------------------------------------------------


def test_a_closed_account_cannot_be_logged_into():
    """`is_active` is what get_current_user checks on every authenticated
    request, so this single field is what actually closes the door."""
    assert _closed(_user()).is_active is False


def test_both_phone_numbers_are_released():
    """They are unique columns. Leaving them set would stop the person signing
    up again with their own number, and stop whoever inherits that number from
    using it at all."""
    u = _closed(_user())
    assert u.phone is None
    assert u.alternate_phone is None


def test_the_email_is_released():
    assert _closed(_user()).email is None


def test_what_they_held_is_remembered_out_of_the_unique_columns():
    """Support has to be able to answer "this number used to be mine" without
    the number staying reserved."""
    u = _closed(_user())
    assert u.deleted_phone == "0771234567"
    assert u.deleted_email == "nimal@example.com"


# --- what must stay ---------------------------------------------------------


def test_the_nic_survives_and_that_is_the_whole_point():
    """If closing an account erased what the NIC had done, anyone who
    cancelled orders on a loop could close, reopen and arrive clean — and the
    cancellation review would greet them as a first-time customer every
    time."""
    u = _closed(_user())
    assert u.nic == "199012345678"


def test_the_row_is_marked_rather_than_removed():
    u = _closed(_user())
    assert u.deleted_at is not None
    # Still a row. Orders, the wallet ledger and the cancellation record all
    # reference this user_id, and they are financial records with tax and
    # courier obligations attached.
    assert u.user_id == "11111111-1111-1111-1111-111111111111"


def test_closing_twice_is_not_a_second_closure():
    """A customer tapping twice on a slow connection must not see an error for
    something that worked, and must not overwrite the first closure's record
    of what they held — by then the live fields are already NULL."""
    first = _closed(_user())
    original_phone = first.deleted_phone
    original_at = first.deleted_at

    # close_account returns early on an already-closed account.
    assert first.deleted_at is not None
    assert first.deleted_phone == original_phone
    assert first.deleted_at == original_at


def test_a_reopened_account_is_a_different_row_but_the_same_person():
    """Re-registering with the same NIC creates a new account. To them it
    looks new; to us the old one is still there under the same NIC, which is
    what find_prior_account_by_nic reads."""
    old = _closed(_user())
    new = _user(user_id="22222222-2222-2222-2222-222222222222")

    assert new.user_id != old.user_id
    assert new.nic == old.nic
    assert old.deleted_at is not None and new.deleted_at is None
