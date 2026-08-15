"""
Locale resolution for customer-facing catalog content.

Products and categories carry their translations in flat per-language columns
(``name_si``/``name_ta``, ``description_si``/``description_ta``, …) with the
untagged column holding English — see migration 035.

Customer endpoints resolve those down to a single value before responding, so
the mobile client receives a plain ``name``/``description`` in the requested
language and needs no per-locale branching of its own. Admin endpoints do the
opposite: they return the raw columns so every language can be edited.

The fallback is deliberately per-field rather than per-row: a product with a
Sinhala name but no Sinhala description shows the Sinhala name and the English
description, instead of dropping back to English wholesale.
"""
from collections.abc import Mapping
from typing import Optional

# Languages the app ships. English is the fallback and therefore never carries
# a column suffix.
SUPPORTED_LANGUAGES = ("en", "si", "ta")
DEFAULT_LANGUAGE = "en"


def resolve_language(header_value: Optional[str]) -> str:
    """
    Pick a supported language from an ``Accept-Language`` header.

    Handles the common forms the mobile client and browsers send —
    ``"si"``, ``"si-LK"``, ``"si-LK,en;q=0.9"`` — by walking the entries in the
    order given and taking the first whose base tag we support. Anything
    unrecognised (or a missing header) resolves to English, so a client that
    sends nothing keeps today's behaviour exactly.

    Quality values are intentionally not sorted on: clients here send a single
    preferred language, and honouring order-of-appearance keeps this cheap and
    predictable.
    """
    if not header_value:
        return DEFAULT_LANGUAGE

    for entry in header_value.split(","):
        # "si-LK;q=0.9" -> "si"
        tag = entry.split(";")[0].strip().lower()
        if not tag:
            continue
        base = tag.split("-")[0]
        if base in SUPPORTED_LANGUAGES:
            return base

    return DEFAULT_LANGUAGE


def localized(obj, field: str, lang: str) -> Optional[str]:
    """
    Read *field* from *obj* in *lang*, falling back to the English column.

    ``localized(product, "name", "si")`` returns ``product.name_si`` when it
    holds a non-blank value, otherwise ``product.name``. Whitespace-only
    translations are treated as missing — an admin who clears the box by
    typing spaces should get the fallback, not a blank product name.

    Accepts either an ORM row or a plain mapping, because the catalog services
    hand back both shapes (``CategoryService.get_all`` returns row ``__dict__``
    snapshots while ``get_hierarchical`` works on model objects). Keeping one
    implementation for both means the fallback rule cannot drift between them.
    """
    def _read(name: str):
        if isinstance(obj, Mapping):
            return obj.get(name)
        return getattr(obj, name, None)

    if lang and lang != DEFAULT_LANGUAGE:
        translated = _read(f"{field}_{lang}")
        if translated and translated.strip():
            return translated

    return _read(field)
