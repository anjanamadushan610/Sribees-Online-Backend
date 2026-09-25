"""
Which language the shopping assistant answers a chat turn in.

The customer's own message decides it, not the app's language setting:

  * Tamil script anywhere, alone or mixed with English or romanised Tamil -> ta
  * Sinhala script anywhere, alone or mixed with English or Singlish      -> si
  * both scripts in one message -> whichever has more letters
  * Latin letters only: romanised Tamil -> ta, Singlish -> si, English -> en
  * nothing to go on (a photo, a shortcut button, "ok", "2", an emoji) -> the
    language the conversation is already in, then the app's language.

Script, and romanised text carrying an unmistakable Singlish or Tamil word,
are decided here, deterministically, before the model runs -- the model is
then TOLD the language rather than asked. Only romanised text this module
cannot place ("parippu 1kg" vs "rice 1kg") is left to the model, which
reports its choice in the same call that plans the turn
(``ChatPlan.reply_language``), so the decision costs no extra request.

Everything here is server-side on purpose: the mobile app sends the text and
renders what comes back, so this policy can change without an app release.
"""

import re
from typing import Iterable, Optional

from app.utils.i18n import SUPPORTED_LANGUAGES

_SINHALA = (0x0D80, 0x0DFF)
_TAMIL = (0x0B80, 0x0BFF)

_WORD = re.compile(r"[a-z]+")

# Romanised words that only occur in Singlish. Anything that is also an
# English word, a common name or a brand ("api", "dan", "mama", "honda"), or
# that Tamil shares ("amma", "podi", "malli", "ayya", "ada"), is left out:
# a false positive here overrides the model, so these must be unambiguous.
_SINGLISH = frozenset(
    """
    eka ekak ekata eke ekka ekek ewa ewwa ewanna ewala
    oni ona onna onada oneda
    thiyenawa thiyenawada thiyenwada thiyenawd thiyenwd thiyanawa thiyanawada
    thiyenne thiyena thiyeda thiyed tiyenawa tiyenawada tiyanawa thibba thibuna
    nadda nedda naddo neda nane nehe naha newei nemei nathi nathnam
    mata mage mge magee oyage oyata oyala oya apita apige
    mokakda mokadda mokada monawada kohomada koheda kohed kawadada kawda kauda
    kiyada kiyak kiyakda keeyada kiyanna kiwwa kiyala
    ganna gannada gannawa gaththa gatta ganne denna dennako dunna deepan
    karanna karanawa karala karanne puluwanda puluwan puluwnda harida hari
    kalin badu bdu genna gennanna genawa dang wage wenuwata hodai hondai hoda
    hodata lassanai balanna hadanna hadala hadana yawanna yawala enawa ennako
    giya gihin ko kwda machan akke nangi thaththa
    kochchara kochchra ganan wenna wela nisa hinda thawa tawa loku aniwa
    gedara gedarata dawasa heta
    parippu haal sini lunu miris kiri kiripiti
    """.split()
)

# Romanised words that only occur in Tamil. Same discipline as above:
# "illa", "enna" and "kudu" are left out because Sinhala has them too, "sari"
# is also a garment and "naan" is bread, which a customer may well be buying.
_TANGLISH = frozenset(
    """
    venum vendum veanum venuma venumaa vennum vendam venam
    irukka irukku irukkuma irukkaa iruka iruku irukuma irukum irukkum irundha
    irukkanga illai illaya illaiya illainga ille
    sollunga sollu solla sollanum kudunga kodunga kodu kudukka
    tharunga thaanga thanga
    evvalavu evlo evalavu evvalo ennoda enakku enaku unga ungal ungaloda
    naanga nanga neenga neega vanakkam nandri romba konjam
    pannunga pannu panna pannanum panren vaanga vanga vaango
    eppadi epdi eppo enge engae inga anga
    kidaikkuma kidaikum kidaikkum kedaikuma ethana ethanai athu ithu antha intha
    vilai thevai thevaiyana kaasu panam
    sapadu saapadu samayal seiya seyya seithu vangi vaanganum vaangu
    anuppu anuppunga aamaa aama
    paruppu arisi thengai vengayam thakkali
    """.split()
)

# Acknowledgements and units every Sri Lankan types whatever language they
# are speaking. A message made only of these carries no language of its own,
# so the conversation keeps the language it is already in.
_NEUTRAL = frozenset(
    """
    ok okay okey k kk hmm hm hi hello hey thanks thank you thankyou thx ty
    yes no pls plz please kg g ml l ltr pcs pc x rs lkr
    """.split()
)

_NAMES = {"en": "English", "si": "Sinhala", "ta": "Tamil"}

_FIXED_INSTRUCTION = {
    "si": (
        "Sinhala. Write reply and every customer-facing text (recipe titles, "
        "ingredients, steps) in Sinhala Unicode script -- natural, friendly Sri "
        "Lankan Sinhala, never romanised Sinhala -- even though the customer may "
        "have typed Singlish or mixed in English. Set reply_language to 'si'."
    ),
    "ta": (
        "Tamil. Write reply and every customer-facing text (recipe titles, "
        "ingredients, steps) in Tamil script, never romanised Tamil, even though "
        "the customer may have typed in English letters or mixed in English. "
        "Set reply_language to 'ta'."
    ),
    "en": (
        "English. Write reply and every customer-facing text in English. "
        "Set reply_language to 'en'."
    ),
}


def _in(ch: str, block: "tuple[int, int]") -> bool:
    return block[0] <= ord(ch) <= block[1]


def _count(text: str, block: "tuple[int, int]") -> int:
    return sum(1 for ch in text if _in(ch, block))


def _hits(words: Iterable[str], lexicon: frozenset) -> int:
    return sum(1 for word in words if word in lexicon)


def detect(message: str) -> "tuple[str, Optional[str]]":
    """
    Classify one message as ``(how, language)``.

    ``how`` is "script" or "romanised" when the language is certain,
    "neutral" when the message has nothing to decide it by, and "open" when
    it is romanised text only the model can place.
    """
    text = message or ""
    sinhala, tamil = _count(text, _SINHALA), _count(text, _TAMIL)
    if sinhala or tamil:
        return "script", "ta" if tamil >= sinhala else "si"

    words = _WORD.findall(text.lower())
    if not words or all(word in _NEUTRAL for word in words):
        return "neutral", None

    singlish, tanglish = _hits(words, _SINGLISH), _hits(words, _TANGLISH)
    if singlish > tanglish:
        return "romanised", "si"
    if tanglish > singlish:
        return "romanised", "ta"
    return "open", None


def fixed_language(message: str, fallback: str) -> Optional[str]:
    """
    The reply language settled before the model runs, or None when the model
    must decide. ``fallback`` is the conversation's language (else the app's),
    used when the message itself says nothing.
    """
    how, language = detect(message)
    if how == "neutral":
        return fallback
    return language


def final_language(
    fixed: Optional[str],
    model_choice: Optional[str],
    fallback: str,
    reply: Optional[str] = None,
) -> str:
    """
    A settled language wins; then the model's choice; then the script the
    model actually wrote ``reply`` in; then ``fallback``.

    The model does not always fill in ``reply_language`` even when told to,
    while writing a perfectly good reply ("seeni 1kg" came back in Sinhala
    with no language). The reply's own script is then the best evidence: the
    message was romanised, so Latin letters can only mean English.
    """
    if fixed:
        return fixed
    if model_choice in SUPPORTED_LANGUAGES:
        return model_choice
    if reply:
        sinhala, tamil = _count(reply, _SINHALA), _count(reply, _TAMIL)
        if sinhala or tamil:
            return "ta" if tamil > sinhala else "si"
        if _WORD.search(reply.lower()):
            return "en"
    return fallback


def last_language(history: "list[dict]") -> Optional[str]:
    """The language the most recent assistant turn was written in, if recorded."""
    for turn in reversed(history):
        if turn.get("role") == "assistant":
            language = turn.get("language")
            return language if language in SUPPORTED_LANGUAGES else None
    return None


def written_in(text: str, language: str) -> bool:
    """
    Whether model-authored text is in the script its language needs. Sinhala
    and Tamil must use their own script (romanised text fails); English must
    not carry either script.
    """
    if language == "si":
        return _count(text, _SINHALA) > 0
    if language == "ta":
        return _count(text, _TAMIL) > 0
    return _count(text, _SINHALA) == 0 and _count(text, _TAMIL) == 0


def instruction(fixed: Optional[str], fallback: str) -> str:
    """The reply-language paragraph appended to the system prompt."""
    common = (
        " Keep brand names, pack sizes and order numbers exactly as they are."
        " Decide reply_language before writing reply."
    )
    if fixed:
        return "Reply language: " + _FIXED_INSTRUCTION[fixed] + common
    return (
        "Reply language: decide it from the customer's LATEST message, never "
        "from the app's language setting.\n"
        "- Romanised Sinhala (Singlish), alone or mixed with English: "
        "reply_language 'si', and write in Sinhala Unicode script.\n"
        "- Romanised Tamil, alone or mixed with English: reply_language 'ta', "
        "and write in Tamil script.\n"
        "- Entirely English: reply_language 'en', and write in English.\n"
        "A single local word decides it: 'parippu 1kg' is Sinhala, "
        "'paruppu 1kg' and 'arisi 5kg' are Tamil, 'dhal 1kg' and 'rice 5kg' "
        "are English. If the message is genuinely too short to tell, use "
        + _NAMES.get(fallback, "English")
        + ". Never reply in romanised Sinhala or romanised Tamil."
        + common
    )
