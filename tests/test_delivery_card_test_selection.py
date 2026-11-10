import pytest

from scripts.inspect_delivery_card_test_accounts import validate_selection


def test_selected_customer_code_is_normalized():
    assert validate_selection({"customer_code": " sr205803 ", "rider_name": " Test Rider One "}) == (
        "SR205803", "Test Rider One",
    )


@pytest.mark.parametrize("code", ["", "205803", "SR205803%", "SRZZZZZZ", "SR205803' OR true"])
def test_customer_selection_rejects_patterns_and_sql(code):
    with pytest.raises(ValueError):
        validate_selection({"customer_code": code, "rider_name": "Test Rider One"})


@pytest.mark.parametrize("rider", ["", "a" * 151, "Test\nRider"])
def test_rider_selection_rejects_invalid_names(rider):
    with pytest.raises(ValueError):
        validate_selection({"customer_code": "SR205803", "rider_name": rider})
