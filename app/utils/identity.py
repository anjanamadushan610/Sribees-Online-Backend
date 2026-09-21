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

# Old format: nine digits and a V or X suffix (pre-2016 cards, still valid).
# New format: twelve digits.
_NIC_OLD = re.compile(r"^\d{9}[VX]$")
_NIC_NEW = re.compile(r"^\d{12}$")

# Sri Lankan mobile numbers are 7XXXXXXXX behind the +94 country code.
_MOBILE = re.compile(r"^7\d{8}$")


def normalize_nic(value: str) -> str:
    """
    Canonical NIC, or ValueError.

    Spaces and dashes people type for readability are dropped, and the old
    format's suffix is upper-cased.
    """
    cleaned = re.sub(r"[\s-]", "", (value or "")).upper()
    if _NIC_OLD.match(cleaned) or _NIC_NEW.match(cleaned):
        return cleaned
    raise ValueError(
        "NIC must be 9 digits followed by V or X, or 12 digits"
    )


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
