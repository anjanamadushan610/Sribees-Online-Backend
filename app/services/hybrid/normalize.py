"""
A customer message, taken apart the way the fast path needs it.

Each word keeps its original spelling (that is what goes to catalogue search,
which folds it again) next to its folded tokens (what the lexicon and the
catalogue vocabulary are compared with). Quantities ("2k", "2ක්", "x3") and
order references ("SB-10423") are lifted out first, so they are never
mistaken for product words.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional

from app.services.search_text import tokenize

MAX_WORDS = 14  # longer messages are explanations, not lookups

_ORDER_REF = re.compile(r"\b(?:sb|ord)[-\s]?\d{3,}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
# "2k", "2kk", "2ක්", "x2", "2x" -- a count of packs, not a size.
_QUANTITY = re.compile(r"^(?:x(\d{1,2})|(\d{1,2})(?:k|kk|x|ක්|ක|pcs|pc|packets?|pkts?)?)$", re.I)
_SIZE = re.compile(r"^\d+(?:\.\d+)?(?:g|kg|ml|l|ltr|mg)$", re.I)
_WORD = re.compile(r",|[^\s,;!?।\"'()]+")
LIST_BREAK = "and"  # a comma between products reads as "and"


@dataclass
class Word:
    raw: str
    tokens: List[str]


@dataclass
class Message:
    raw: str
    words: List[Word] = field(default_factory=list)
    quantity: Optional[int] = None
    order_reference: Optional[str] = None
    has_question_mark: bool = False

    @property
    def tokens(self) -> List[str]:
        return [t for w in self.words for t in w.tokens]


def parse(text: str) -> Optional[Message]:
    """None when the message is empty or too long to be a simple lookup."""
    raw = unicodedata.normalize("NFC", (text or "").strip())
    if not raw:
        return None
    msg = Message(raw=raw, has_question_mark="?" in raw)
    ref = _ORDER_REF.search(raw)
    if ref:
        msg.order_reference = ref.group(0).upper().replace(" ", "-")
        raw = raw[: ref.start()] + " " + raw[ref.end():]
    pieces = _WORD.findall(raw)
    if len(pieces) > MAX_WORDS:
        return None
    for piece in pieces:
        if piece == ",":
            msg.words.append(Word(raw=",", tokens=[LIST_BREAK]))
            continue
        clean = piece.strip(".:-")
        if not clean:
            continue
        quantity = _QUANTITY.match(clean)
        if quantity and not _SIZE.match(clean) and msg.quantity is None:
            msg.quantity = int(quantity.group(1) or quantity.group(2))
            continue
        tokens = tokenize(clean)
        if tokens:
            msg.words.append(Word(raw=clean, tokens=tokens))
    return msg if msg.words or msg.order_reference else None
