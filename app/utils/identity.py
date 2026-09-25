"""
SRIBEESonline - Sri Lankan identity formats.

One place for the two formats the business recognises a customer by, so the
profile endpoint, the order endpoint and the admin tools all accept and store
exactly the same thing. Normalising on the way in means a number typed as
"077 123 4567" and one typed as "+94771234567" are the same number, and a NIC
typed as "123456789v" is the same NIC as "123456789V" — which matters a great
deal for a column with a UNIQUE constraint on it.
"""

import re
from datetime import datetime, timezone
from typing import Optional

# Old format: nine digits and a V or X suffix (pre-2016 cards, still valid).
# New format: twelve digits.
_NIC_OLD = re.compile(r"^\d{9}[VX]$")
_NIC_NEW = re.compile(r"^\d{12}$")

# A NIC is issued from the age of 15, and nobody alive was born before this.
NIC_MIN_AGE = 15
NIC_OLDEST_BIRTH_YEAR = 1915

# Sri Lankan mobile numbers are 7XXXXXXXX behind the +94 country code.
_MOBILE = re.compile(r"^7\d{8}$")


def normalize_nic(value: str) -> str:
    """
    Canonical NIC, or ValueError.

    Spaces and dashes people type for readability are dropped, and the old
    format's suffix is upper-cased.
    """
    cleaned = re.sub(r"[\s-]", "", (value or "")).upper()
    if _NIC_OLD.match(cleaned):
        year, day = 1900 + int(cleaned[:2]), int(cleaned[2:5])
    elif _NIC_NEW.match(cleaned):
        year, day = int(cleaned[:4]), int(cleaned[4:7])
    else:
        raise ValueError(
            "NIC must be 9 digits followed by V or X, or 12 digits"
        )
    if nic_birth_date_error(year, day) is not None:
        raise ValueError("This is not a valid NIC number")
    return cleaned


def nic_birth_date_error(year: int, day: int, *, this_year: Optional[int] = None) -> Optional[str]:
    """
    Why the birth date inside a NIC cannot be real, or None when it can.

    Both formats carry the holder's birth year and day of the year; women's
    day numbers have 500 added. The Department for Registration of Persons
    counts every February as 29 days, so days run 1-366 in every year and
    day 60 is 29 February - which only exists in a leap year. These are the
    checks that turn away the made-up numbers people type to get past the
    form (all nines, 123456789V, a birth date in the future); the database
    can't tell whether the card is real, but it can refuse one that can't be.
    """
    this_year = this_year or datetime.now(timezone.utc).year
    if not NIC_OLDEST_BIRTH_YEAR <= year <= this_year - NIC_MIN_AGE:
        return "year"
    if day > 500:
        day -= 500
    if not 1 <= day <= 366:
        return "day"
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    if day == 60 and not leap:
        return "day"
    return None


def normalize_mobile(value: str) -> str:
    """
    Canonical +94 mobile number, or ValueError.

    Accepts the four shapes people actually type: 0771234567, 771234567,
    94771234567 and +94771234567.
    """
    digits = re.sub(r"[^\d]", "", (value or ""))
    if digits.startswith("94"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if _MOBILE.match(digits):
        return f"+94{digits}"
    raise ValueError("Enter a Sri Lankan mobile number, e.g. 0771234567")
