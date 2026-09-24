"""
Search text normalisation — the one function both sides of search agree on.

Sri Lankan shoppers type the same product four ways: Sinhala script, Tamil
script, Singlish/Tamilish (the same words in Latin letters, spelled however
the thumb feels like it), and English. "බිත්තර", "biththara", "bittara" and
"bithara" are one word. Matching them is not an AI problem, it is a spelling
problem, and it is solved deterministically here, for free, in microseconds:

1. **Transliterate** Sinhala and Tamil script to Latin, the way Singlish is
   written — "කෙසෙල්" → "kesel", "பால்" → "paal".
2. **Fold** the spellings Singlish writers disagree on onto one key — th/t,
   dh/d, sh/s, w/v, ee/i, oo/u, aa/a, doubled letters — so "biththara" and
   "bittara" both become "bitara", and "seeni", "sini" and "සීනි" all become
   "sini".
3. **Tokenise** into words, splitting "5kg" into "5" "kg".

The same pipeline runs over catalogue text when a product is saved (into
``products.search_tokens``) and over the shopper's query at search time, so a
query in any script meets the catalogue in the same key space. Genuine typos
("suger", "kehel") are left to the edit-distance matching in
``catalog_search_service``; alternate *words* ("kesel" for banana) come from
the admin's search keywords, which Gemini helps draft.

Bump ``INDEX_VERSION`` whenever the output of ``index_tokens`` changes for the
same input — every product is re-tokenised on the next app start.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

INDEX_VERSION = 1

# Zero-width joiners are part of how Sinhala conjuncts are typed (the
# rakaransaya and yansaya forms) and vary by keyboard; they carry no meaning for matching.
_ZERO_WIDTH = dict.fromkeys((0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF), None)

# ---------------------------------------------------------------------------
# Script tables. Sinhala and Tamil occupy disjoint code-point ranges, so one
# set of tables serves both.
# ---------------------------------------------------------------------------

# Consonants carry an inherent "a" unless a vowel sign or a virama follows.
_CONSONANTS = {
    # Sinhala
    "ක": "k", "ඛ": "kh", "ග": "g", "ඝ": "gh", "ඞ": "ng", "ඟ": "ng",
    "ච": "ch", "ඡ": "chh", "ජ": "j", "ඣ": "jh", "ඤ": "ny", "ඥ": "gn", "ඦ": "nj",
    "ට": "t", "ඨ": "th", "ඩ": "d", "ඪ": "dh", "ණ": "n", "ඬ": "nd",
    "ත": "th", "ථ": "th", "ද": "d", "ධ": "dh", "න": "n", "ඳ": "nd",
    "ප": "p", "ඵ": "ph", "බ": "b", "භ": "bh", "ම": "m", "ඹ": "mb",
    "ය": "y", "ර": "r", "ල": "l", "ව": "v",
    "ශ": "sh", "ෂ": "sh", "ස": "s", "හ": "h", "ළ": "l", "ෆ": "f",
    # Tamil
    "க": "k", "ங": "ng", "ச": "s", "ஞ": "ny", "ட": "t", "ண": "n",
    "த": "th", "ந": "n", "ப": "p", "ம": "m", "ய": "y", "ர": "r",
    "ல": "l", "வ": "v", "ழ": "zh", "ள": "l", "ற": "r", "ன": "n",
    "ஜ": "j", "ஷ": "sh", "ஸ": "s", "ஹ": "h",
}

# Vowel signs replace a consonant's inherent "a". Long e/o are written with a
# single letter on purpose: Singlish "ee" means ī (seeni = සීනි) and "oo"
# means ū, so spelling ේ as "ee" would collide with ී after folding.
_VOWEL_SIGNS = {
    # Sinhala
    "ා": "aa", "ැ": "ae", "ෑ": "ae", "ි": "i", "ී": "ii", "ු": "u", "ූ": "uu",
    "ෘ": "ru", "ෲ": "ruu", "ෟ": "lu", "ෳ": "luu",
    "ෙ": "e", "ේ": "e", "ෛ": "ai", "ො": "o", "ෝ": "o", "ෞ": "au",
    # Tamil
    "ா": "aa", "ி": "i", "ீ": "ii", "ு": "u", "ூ": "uu",
    "ெ": "e", "ே": "e", "ை": "ai", "ொ": "o", "ோ": "o", "ௌ": "au",
}

_VIRAMAS = {chr(0x0DCA), chr(0x0BCD)}  # Sinhala al-lakuna, Tamil pulli

_STANDALONE = {
    # Sinhala independent vowels and modifiers
    "අ": "a", "ආ": "aa", "ඇ": "ae", "ඈ": "ae", "ඉ": "i", "ඊ": "ii",
    "උ": "u", "ඌ": "uu", "ඍ": "ri", "ඎ": "ruu", "ඏ": "li", "ඐ": "lii",
    "එ": "e", "ඒ": "e", "ඓ": "ai", "ඔ": "o", "ඕ": "o", "ඖ": "au",
    "ං": "n", "ඃ": "h",
    # Tamil independent vowels and aytham
    "அ": "a", "ஆ": "aa", "இ": "i", "ஈ": "ii", "உ": "u", "ஊ": "uu",
    "எ": "e", "ஏ": "e", "ஐ": "ai", "ஒ": "o", "ஓ": "o", "ஔ": "au", "ஃ": "",
}

# ---------------------------------------------------------------------------
# Singlish folding. Order matters: digraphs before the doubled-letter
# collapse, "ee"/"oo" before it too (they change the vowel, not just length).
# ---------------------------------------------------------------------------
_FOLDS = (
    ("zh", "l"),   # Tamil ழ: vazhai / valai
    ("th", "t"),
    ("dh", "d"),
    ("sh", "s"),
    ("kh", "k"),
    ("gh", "g"),
    ("bh", "b"),
    ("jh", "j"),
    ("ph", "f"),
    ("ngk", "ng"),  # Tamil ங்க: தேங்காய் / thengai
    ("ay", "ai"),   # தேங்காய் / thengai
    ("ey", "ai"),   # எண்ணெய் / ennai
    ("ae", "a"),   # ඇ: aepal / apal
    ("ee", "i"),   # seeni / sini
    ("oo", "u"),
    ("w", "v"),    # ව: wattakka / vattakka
    ("q", "k"),
)
_C_AS_K = re.compile(r"c(?!h)")  # carrot / karot, but keep "ch"
_REPEATS = re.compile(r"(.)\1+")
_PHRASE_SEPARATORS = re.compile(r"[,;|\s]*[,;|][,;|\s]*|[\r\n]+")
_TOKEN = re.compile(r"[^\W\d_]+|\d+")

# Tokens that describe a pack rather than a product. They still rank (a
# "5kg" query prefers the 5 kg bag) but never decide whether a product matches.
SOFT_TOKENS = frozenset({
    "kg", "g", "gr", "gm", "grm", "ml", "l", "lt", "ltr", "litre", "liter",
    "pcs", "pc", "pk", "pkt", "pack", "packet", "bottle", "botal", "x",
    "and", "with", "of", "the", "for", "a", "an", "saha", "ekka",
})


def _transliterate(text: str, final_inherent: bool = True) -> str:
    """
    Sinhala/Tamil → Latin; anything else passes through.

    *final_inherent* False drops the inherent "a" of a word's last consonant:
    "හාල" (still being typed, al-lakuna not yet added) → "haal", not
    "haala".
    """
    out: list[str] = []
    pending = False  # a consonant is still waiting to learn its vowel
    for ch in text:
        latin = _CONSONANTS.get(ch)
        if latin is not None:
            if pending:
                out.append("a")
            out.append(latin)
            pending = True
            continue
        sign = _VOWEL_SIGNS.get(ch)
        if sign is not None:
            out.append(sign)
            pending = False
            continue
        if ch in _VIRAMAS:
            pending = False
            continue
        if pending:
            standalone = ch in _STANDALONE
            if standalone or final_inherent:
                out.append("a")
            pending = False
        out.append(_STANDALONE.get(ch, ch))
    if pending and final_inherent:
        out.append("a")
    return "".join(out)


def _strip_latin_accents(text: str) -> str:
    # Only safe after transliteration: on raw Sinhala/Tamil this would drop
    # the vowel signs, which are combining marks too.
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def fold_word(word: str) -> str:
    """Fold one lower-case Latin word onto its Singlish spelling key."""
    word = _C_AS_K.sub("k", word)
    for src, dst in _FOLDS:
        word = word.replace(src, dst)
    return _REPEATS.sub(r"\1", word)


def tokenize(text: str | None, final_inherent: bool = True) -> list[str]:
    """Any-script text → folded search tokens, in order, duplicates kept."""
    if not text:
        return []
    text = unicodedata.normalize("NFC", text).translate(_ZERO_WIDTH).lower()
    text = _strip_latin_accents(_transliterate(text, final_inherent))
    return [
        tok if tok.isdigit() else fold_word(tok)
        for tok in _TOKEN.findall(text)
    ]


def split_keywords(raw: str | None) -> list[str]:
    """
    The admin's keyword field → clean, de-duplicated phrases.

    Commas, semicolons, pipes and newlines all separate phrases; case and
    surrounding space never make a duplicate.
    """
    if not raw:
        return []
    seen: set[str] = set()
    phrases: list[str] = []
    for part in _PHRASE_SEPARATORS.split(unicodedata.normalize("NFC", raw)):
        phrase = " ".join(part.translate(_ZERO_WIDTH).split()).strip(" .\"'")
        key = phrase.casefold()
        if phrase and key not in seen:
            seen.add(key)
            phrases.append(phrase)
    return phrases


def normalize_keywords(raw: str | None, max_phrases: int = 60) -> str | None:
    """Canonical storage form of ``Product.search_keywords``."""
    phrases = split_keywords(raw)[:max_phrases]
    return ", ".join(phrases) if phrases else None


def _is_word(token: str) -> bool:
    """A token that says *what* the product is, not how much of it."""
    return len(token) > 1 and not token.isdigit() and token not in SOFT_TOKENS


def _phrase_tokens(phrase: str) -> tuple[list[str], str]:
    """
    A phrase's tokens plus each adjacent pair of words run together, and the
    phrase key (all its words run together).

    Shoppers drop the space as often as they keep it ("polthel",
    "ambulkesel"), so the run-together forms are indexed alongside the words.
    """
    tokens = [t for t in tokenize(phrase) if len(t) > 1 or t.isdigit()]
    words = [t for t in tokens if _is_word(t)]
    pairs = [a + b for a, b in zip(words, words[1:], strict=False)]
    return tokens + pairs, "".join(words)


NAME_PHRASE_MARK = "="


def index_tokens(
    name: str | None,
    name_si: str | None = None,
    name_ta: str | None = None,
    search_keywords: str | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """
    A product's search index: (all tokens, name-only tokens, phrase keys).

    Name tokens are kept apart so a hit on the product's own name outranks a
    hit on one of its alternate keywords. Phrase keys let a query that *is*
    the whole name or a whole keyword ("kiri", "rathu haal") beat products
    that merely contain the word; name phrase keys carry
    ``NAME_PHRASE_MARK`` in front.
    """
    name_tokens: set[str] = set()
    phrases: set[str] = set()
    for phrase in (name, name_si, name_ta):
        if phrase:
            tokens, key = _phrase_tokens(phrase)
            name_tokens.update(tokens)
            if key:
                phrases.add(NAME_PHRASE_MARK + key)
    all_tokens = set(name_tokens)
    for phrase in split_keywords(search_keywords):
        tokens, key = _phrase_tokens(phrase)
        all_tokens.update(tokens)
        if key:
            phrases.add(key)
    return sorted(all_tokens), sorted(name_tokens), sorted(phrases)


@dataclass(frozen=True)
class ParsedQuery:
    required: tuple[str, ...]   # the words; each should match, ranking degrades gracefully
    soft: tuple[str, ...]       # pack sizes and filler: rank, never filter
    phrase: str                 # the words run together, to compare with phrase keys
    # Word → the same word without its final inherent vowel, for Sinhala/Tamil
    # typed up to a bare consonant ("හාල" → "hala", stem "hal").
    stems: dict[str, str] = field(default_factory=dict)


def parse_query(query: str) -> ParsedQuery:
    required: list[str] = []
    soft: list[str] = []
    stems: dict[str, str] = {}
    tokens = tokenize(query)
    bare = tokenize(query, final_inherent=False)
    if len(bare) != len(tokens):
        bare = tokens
    for tok, stem in zip(tokens, bare, strict=True):
        bucket = required if _is_word(tok) else soft
        if tok not in bucket:
            bucket.append(tok)
        if stem != tok and _is_word(stem):
            stems[tok] = stem
    if not required:
        # "500" or "kg" alone: nothing else to go on, so let them decide.
        required, soft = soft, []
    return ParsedQuery(tuple(required), tuple(soft), "".join(required), stems)
