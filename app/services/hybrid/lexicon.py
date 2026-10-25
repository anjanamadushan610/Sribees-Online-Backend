"""
The words the fast path recognises, in every script shoppers use.

Entries are written the way people type them -- Singlish, Sinhala, Tamil,
English -- and folded at import through the search tokenizer, so "තියෙනවද",
"thiyenawada" and "thiyenwda" meet as one key, exactly as product names do.
A word belongs here only when it means the same thing in every context the
fast path acts on; anything doubtful stays out and the message goes to the
model.
"""

from app.services.search_text import tokenize


def _fold(*phrases: str) -> frozenset:
    out = set()
    for phrase in phrases:
        out.update(tokenize(phrase))
    return frozenset(out)


# Words that carry no meaning for routing: pronouns, counters, politeness.
FILLER = _fold(
    "mata mage mge api apita oya oyage ekak eka ekk ek ekath ekata ekta kiyak karanna karala",
    "packet packets pakat pkt kilo kg grams bottle tin box",
    "please pls plz ane aney ah ow ok okay hari harida",
    "da d ne neh nam nm ko kho",
    "i me my we a an the some any for please is are am do does you your can could of to in at lot",
    "ekak dekak thunak hatarak pahak deka thuna hathara paha tika tikak dan thama",
    "babata lamainta lamayinta lamayata gedarata me mee app ap eken nana nanna",
    "මට මගේ අපිට එකක් එක පැකට් කිලෝ ද නේ අනේ තාම දැන් මේ ඇප් එකෙන්",
    "எனக்கு ஒரு கிலோ பாக்கெட் தயவுசெய்து என் என்னுடைய எனது இருக்கிறது உள்ளது இன்னும்",
)

# Words only one of the two Latin-script languages uses, for messages whose
# language chat_language cannot settle on its own.
SINGLISH = _fold(
    "one ona oni ganna denna dennako thiyenawada thiyenwada thiyanawada thiyeda kiyada gana ganan mila",
    "mata mage mge apita oyage ekak eka ekk ekata ekta koheda kohada awe aawe kalin gaththa gatta badu",
    "tika pennanna balanna danna dannako kohomada karanne karanna saha naha nehe wenas sthuthi ayubowan",
    "dekak thunak dan thama babata",
)
ENGLISH = _fold(
    "need want buy have show price cost how much where status track order orders open page",
    "thanks thank you is are do does the my me please what when which help add use change",
)

# "I want / is there / how much" around a product: a shopping question.
WANT = _fold(
    "one onee ona oni onei ganna gannawa ganna one gannada denna dennako dennda",
    "thiyenawada thiyenwada thiyanawada thiyeda tiyenawada thiyenawa thiyena",
    "need want buy have got available show pennanna pennapan balanna",
    "ඕන ඕනෙ ඕනේ ගන්න දෙන්න තියෙනවද තියනවද තියෙනවා පෙන්නන්න",
    "வேண்டும் வேணும் இருக்கா இருக்கிறதா வாங்க காட்டு",
)
PRICE = _fold(
    "kiyada kiyda keeyada ganan mila price cost how much rate",
    "කීයද ගාන ගණන මිල",
    "விலை எவ்வளவு",
)

# Orders.
ORDER = _fold(
    "order orders oder odar ordar orderek parcel badu items purchases",
    "ඇණවුම ඇණවුම් ඕඩරය ඕඩර් බඩු",
    "ஆர்டர் ஆர்டர்கள்",
)
TRACK = _fold(
    "koheda kohada koo where status track tracking awe aawe ena enne kawadda when delivery deliver",
    "කොහෙද ආවේ එන්නේ කවදද",
    "எங்கே எப்போது நிலை",
)
HISTORY = _fold(
    "kalin kalinma previous past history old list okkoma all mama gaththa gatta gattha",
    "කලින් පරණ ගත්ත සියලු",
    "முந்தைய பழைய",
)
NOT_ARRIVED = _fold("naha nehe nathi", "නැහැ නෑ", "இல்லை")

# App help and pages.
HOW = _fold("kohomada kohomd kohomda how karanne hadanne", "කොහොමද කරන්නේ", "எப்படி")
# Neutral action words, allowed only inside a how-to question about a page.
HELP_VERBS = _fold(
    "wenas change update use edit set danne dhanne denne balanne place",
    "වෙනස් බලන්නේ දාන්නේ",
    "மாற்றுவது மாற்ற பயன்படுத்த",
)
OPEN = _fold("open page yanna yamu go pennanna", "විවෘත පිටුව යන්න", "திற பக்கம்")
TOPICS = {
    "cart": _fold("cart basket kart karta", "කරත්තය කාට්", "கூடை"),
    "checkout": _fold("checkout pay payment gewanna gewana gewanne", "ගෙවන්න ගෙවීම", "செலுத்த"),
    "addresses": _fold("address addresses lipinaya location", "ලිපිනය", "முகவரி"),
    "wallet": _fold("wallet balance salli", "පසුම්බිය", "பணப்பை"),
    "offers": _fold("offer offers coupon coupons discount deal deals", "වට්ටම් කූපන්", "தள்ளுபடி கூப்பன்"),
    "settings": _fold("settings setting language basha", "සැකසුම් භාෂාව", "அமைப்புகள் மொழி"),
    "profile": _fold("profile account gini", "ගිණුම", "சுயவிவரம்"),
    "orders": _fold("return returns reorder aapahu", "ආපසු", "திருப்பி"),
    "support": _fold("cancel cancellation refund help support complain complaint", "අවලංගු සහාය", "ரத்து உதவி"),
}

# Social messages answered without tools.
GREETING = _fold(
    "hi hii hello helo hey ayubowan ayubovan good morning evening kohomada oyata",
    "ආයුබෝවන් හායි හෙලෝ",
    "வணக்கம் ஹாய் இருக்கீங்க இருக்கிறீர்கள்",
)
THANKS = _fold("thanks thank thankyou thx ty sthuthi stuti", "ස්තූතියි ස්තුතියි", "நன்றி")

# Putting something in the basket.
CART_ADD = _fold(
    "add danna dannako dapan ekathu cart kartekata basket",
    "දාන්න එකතු",
    "சேர் சேர்க்க",
)
CART_WORDS = TOPICS["cart"]

# Words that mean the message needs judgement the fast path does not have:
# comparisons, negations, preferences, recipes, complaints. These always go
# to the model.
NEEDS_MODEL = _fold(
    "not no dont nathi epa natha wena wenuwata instead compare vs versus aduwen adu wadi wadiya",
    "cheaper cheapest best hoda honda recommend suggest recipe hadanna uyanna",
    "because why mokada ai wrong waradi complaint",
    "ain remove delete",
    "නැති එපා වෙනුවට අඩු වැඩි හොඳ ඇයි",
    # Not "ஏன்" (why): it folds to "en", the same key as "என்" (my).
    "இல்லாத வேண்டாம் மலிவான சிறந்த",
)
# Never add "gana" (count/price) to NEEDS_MODEL: it folds to the same key as
# "ganna" (to buy). Quantities are handled by a rule instead (see rules.py).

CONJUNCTION = _fold("saha and ekka", "සහ", "மற்றும்")
