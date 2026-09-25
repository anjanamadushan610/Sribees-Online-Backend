"""
Tests for the photos a customer attaches to a return, and for the name that
goes on the parcel.

Both fix the same shape of bug: a field that looked filled in and was not.
The app used to write "Attached Photos: 3 photo(s) submitted with return."
into the comments and discard the files, so the Branch Manager deciding the
claim read a sentence promising evidence and had none. The courier label fell
back to the literal string "Customer", so riders carried parcels addressed to
nobody.

DB-free, in this file's neighbouring style.
"""
from types import SimpleNamespace

import pytest

from app.schemas.order import ReturnRequest
from app.services.order_service import resolve_recipient_name

# --- the request schema --------------------------------------------------


def test_a_return_request_must_carry_at_least_one_photo():
    """The claim is decided by looking at the goods, so the app cannot submit
    without them. This is the guard that makes the requirement real -- the
    screen's own validation can be skipped by anything that is not the app."""
    with pytest.raises(ValueError):
        ReturnRequest(reason="Damaged", images=[])


def test_a_return_request_accepts_up_to_four_photos():
    req = ReturnRequest(reason="Damaged", images=[f"/uploads/returns/o/{i}.jpg" for i in range(4)])
    assert len(req.images) == 4

    with pytest.raises(ValueError):
        ReturnRequest(reason="Damaged", images=[f"/uploads/returns/o/{i}.jpg" for i in range(5)])


def test_photos_keep_the_order_the_customer_added_them_in():
    """The first photo is usually the one they framed as the problem."""
    paths = ["/uploads/returns/o/c.jpg", "/uploads/returns/o/a.jpg", "/uploads/returns/o/b.jpg"]
    assert ReturnRequest(reason="Damaged", images=paths).images == paths


# --- the name on the parcel ----------------------------------------------


class _FakeSession:
    """Returns a user row for `get`, which is all resolve_recipient_name uses."""

    def __init__(self, user=None) -> None:
        self._user = user
        self.gets = 0

    async def get(self, _model, _pk):
        self.gets += 1
        return self._user


def _order(**extra):
    fields = {
        "order_number": "SB-0001",
        "user_id": "22222222-2222-2222-2222-222222222222",
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


@pytest.mark.asyncio
async def test_an_explicit_recipient_beats_the_account_holder():
    """"Deliver this to my mother" is a deliberate instruction and outranks
    whose account placed the order."""
    db = _FakeSession(SimpleNamespace(full_name="Nimal Perera"))
    address = SimpleNamespace(recipient_name="Kamala Perera", phone="0771234567")

    assert await resolve_recipient_name(db, _order(), address) == "Kamala Perera"
    assert db.gets == 0, "the user row should not be loaded when the address names someone"


@pytest.mark.asyncio
async def test_a_blank_recipient_falls_back_to_the_customers_full_name():
    """recipient_name is optional on the address form and most people leave it
    empty, so this is the common path, not the edge case. It used to produce
    the literal string "Customer"."""
    db = _FakeSession(SimpleNamespace(full_name="Nimal Perera"))

    for address in (
        SimpleNamespace(recipient_name=None),
        SimpleNamespace(recipient_name=""),
        SimpleNamespace(recipient_name="   "),
        None,
    ):
        db.gets = 0
        assert await resolve_recipient_name(db, _order(), address) == "Nimal Perera"


@pytest.mark.asyncio
async def test_the_name_is_never_the_word_customer():
    """Even with nothing to go on, the parcel gets something that identifies
    one delivery. "Customer" identifies nobody."""
    db = _FakeSession(None)
    name = await resolve_recipient_name(db, _order(), SimpleNamespace(recipient_name=None))

    assert name != "Customer"
    assert name == "SB-0001"


@pytest.mark.asyncio
async def test_a_nameless_user_row_does_not_produce_an_empty_label():
    db = _FakeSession(SimpleNamespace(full_name="  "))
    assert await resolve_recipient_name(db, _order(), None) == "SB-0001"
