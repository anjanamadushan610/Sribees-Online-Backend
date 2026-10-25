"""
Customer catalog search: normalisation, typo tolerance and ranking.

Runs entirely in memory against the demo catalog seeded by migrations 021
and 029, indexed exactly as the app indexes it. The query list is the one
measured against the old trigram search on dev (2026-09-24), where 8 of these
17 found nothing.
"""
import json
import uuid

import pytest

from app.services import catalog_search_service as cs
from app.services.keyword_suggestion_service import _flatten
from app.services.search_text import (
    index_tokens,
    normalize_keywords,
    parse_query,
    tokenize,
)

CATALOG = {
    "Ambul Banana (1 kg)": "kesel, ambul kesel, කෙසෙල්, අඹුල් කෙසෙල්, வாழைப்பழம், vazhaipazham, banana",
    "Tomato (1 kg)": "thakkali, තක්කාලි, தக்காளி",
    "Carrot (500 g)": "carrot, kaarot, karat, කැරට්, கேரட்",
    "Green Chilli (250 g)": "miris, kola miris, අමු මිරිස්, කොළ මිරිස්, பச்சை மிளகாய், milagai",
    "King Coconut (each)": "thambili, තැඹිලි, இளநீர், elaneer, king coconut",
    "Fresh Milk (1 L)": "kiri, kiri milk, කිරි, பால், paal, milk",
    "Buffalo Curd (400 g)": "kiri dosi, mee kiri, මී කිරි, தயிர், thayir, curd",
    "Farm Eggs (10 pack)": "biththara, බිත්තර, முட்டை, muttai, eggs",
    "Set Yoghurt (4 cups)": "yogurt, yoghurt, යෝගට්, தயிர், thayir",
    "Butter (200 g)": "batter, batar, බටර්, வெண்ணெய், vennai, butter",
    "Red Raw Rice (5 kg)": "haal, rathu haal, හාල්, රතු හාල්, அரிசி, arisi, rice",
    "White Sugar (1 kg)": "seeni, සීනි, சர்க்கரை, sarkkarai, sugar",
    "Red Lentils (1 kg)": "parippu, dhal, mysoor dhal, පරිප්පු, பருப்பு, paruppu, lentils",
    "Coconut Oil (750 ml)": "pol thel, පොල් තෙල්, தேங்காய் எண்ணெய், thengai ennai, coconut oil",
    "Curry Powder (250 g)": "kari kudu, curry kudu, කරි කුඩු, மசாலா தூள், masala podi, curry powder",
}


def _rows():
    rows, names = [], {}
    for name, keywords in CATALOG.items():
        pid = uuid.uuid4()
        tokens, name_tokens, phrases = index_tokens(name, None, None, keywords)
        rows.append((pid, tokens, name_tokens, phrases, False, 0, name))
        names[pid] = name
    return rows, names


ROWS, NAMES = _rows()
VOCAB = cs.Vocabulary(t for row in ROWS for t in row[1])


def search(query: str) -> list[str]:
    plan = cs.make_plan(VOCAB, query)
    if plan is None:
        return []
    terms = set(plan.terms)
    # What the GIN && lookup would return, then the real ranking.
    candidates = [row for row in ROWS if terms & set(row[1])]
    return [NAMES[pid] for pid in cs.rank(candidates, plan)]


# ---------------------------------------------------------------------------
# Normalisation: every script and spelling of a word lands on one key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spellings", [
    ["බිත්තර", "biththara", "bittara", "bithara"],
    ["සීනි", "seeni", "sini"],
    ["හාල්", "haal", "hal"],
    ["කෙසෙල්", "kesel"],
    ["තක්කාලි", "thakkali", "thakali"],
    ["පරිප්පු", "parippu", "parripu"],
    ["වට්ටක්කා", "wattakka", "vattakka"],
    ["பால்", "paal", "pal"],
    ["முட்டை", "muttai", "mutai"],
    ["வாழைப்பழம்", "vazhaipazham", "valaipalam"],
    ["சர்க்கரை", "sarkkarai", "sarkarai"],
    ["carrot", "karot", "karrot"],
])
def test_spellings_share_one_key(spellings):
    keys = {tuple(tokenize(s)) for s in spellings}
    assert len(keys) == 1, keys


def test_tamil_nasal_and_diphthong_meet_tamilish():
    assert tokenize("தேங்காய் எண்ணெய்") == tokenize("thengai ennai")


def test_zero_width_joiner_is_ignored():
    zwj = chr(0x200D)
    assert tokenize("ශ්" + zwj + "රී") == tokenize("ශ්රී") == ["sri"]


def test_pack_sizes_split_and_stay_soft():
    parsed = parse_query("arisi 5kg")
    assert parsed.required == ("arisi",)
    assert parsed.soft == ("5", "kg")


def test_number_alone_is_still_searchable():
    assert parse_query("500").required == ("500",)


def test_keywords_are_deduplicated_and_trimmed():
    raw = "kesel,  Kesel ; කෙසෙල්\nbanana,, "
    assert normalize_keywords(raw) == "kesel, කෙසෙල්, banana"
    assert normalize_keywords(" , ") is None


def test_run_together_words_are_indexed():
    tokens, _, _ = index_tokens("Coconut Oil (750 ml)", None, None, "pol thel")
    assert "poltel" in tokens and "kokonutoil" in tokens


# ---------------------------------------------------------------------------
# Typo tolerance
# ---------------------------------------------------------------------------

def test_short_words_get_no_typo_matching():
    # 3 letters: "pol" (coconut) must not drift to "pal" (milk).
    assert set(VOCAB.expand("pol")) - {"pol"} == {"poltel"}


def test_transposition_counts_as_one_edit():
    assert cs.osa_distance("mlik", "milk", 1) == 1


def test_four_letter_word_is_not_typo_matched():
    # "kiri" (milk) must not reach "kirim" (the Tamil for cream).
    vocab = cs.Vocabulary(["kirim", "kari"])
    assert set(vocab.expand("kiri")) == {"kiri", "kirim"}  # prefix only
    assert vocab.expand("kiri")["kirim"] >= cs.W_PREFIX_BASE


def test_known_word_is_not_fuzzed():
    assert all(w == 1.0 or w >= cs.W_PREFIX_BASE for w in VOCAB.expand("kiri").values())


# ---------------------------------------------------------------------------
# End to end over the demo catalog
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query, expected", [
    ("bananna", "Ambul Banana (1 kg)"),
    ("kehel", "Ambul Banana (1 kg)"),
    ("කෙසල්", "Ambul Banana (1 kg)"),
    ("thakali", "Tomato (1 kg)"),
    ("parupu", "Red Lentils (1 kg)"),
    ("bittara", "Farm Eggs (10 pack)"),
    ("biththra", "Farm Eggs (10 pack)"),
    ("kiri", "Fresh Milk (1 L)"),
    ("pol tel", "Coconut Oil (750 ml)"),
    ("polthel", "Coconut Oil (750 ml)"),
    ("rathu hal", "Red Raw Rice (5 kg)"),
    ("sini", "White Sugar (1 kg)"),
    ("suger", "White Sugar (1 kg)"),
    ("cocunut oil", "Coconut Oil (750 ml)"),
    ("හාල", "Red Raw Rice (5 kg)"),
    ("பால", "Fresh Milk (1 L)"),
    ("arisi 5kg", "Red Raw Rice (5 kg)"),
    ("egg", "Farm Eggs (10 pack)"),
    ("ban", "Ambul Banana (1 kg)"),
    ("coconut oil", "Coconut Oil (750 ml)"),
    ("தேங்காய் எண்ணெய்", "Coconut Oil (750 ml)"),
])
def test_query_finds_product_first(query, expected):
    results = search(query)
    assert results, f"{query!r} found nothing"
    assert results[0] == expected, results


def test_all_words_must_match_when_possible():
    # Rice is the only product with both words; the other "red" products
    # must not ride along.
    assert search("rathu haal") == ["Red Raw Rice (5 kg)"]


def test_falls_back_to_best_partial_match():
    # Nothing matches "packet" — show what matches "kiri" rather than nothing.
    results = search("kiri packet")
    assert results[:2] == ["Fresh Milk (1 L)", "Buffalo Curd (400 g)"]


def test_whole_phrase_beats_contained_word():
    results = search("kiri")
    assert results.index("Fresh Milk (1 L)") < results.index("Buffalo Curd (400 g)")


def test_two_letter_word_cannot_match_alone():
    # "mee" folds to "mi", which is also on Buffalo Curd ("mee kiri"). A
    # shopper after "kottu mee" must see nothing rather than curd.
    assert search("kottu mee") == []
    # ...while a real word still carries it.
    assert search("mee kiri")[0] == "Buffalo Curd (400 g)"


def test_nonsense_finds_nothing():
    assert search("xyzzyq") == []


def test_product_saved_elsewhere_matches_before_vocabulary_refresh():
    empty = cs.Vocabulary(())
    plan = cs.make_plan(empty, "kottu")
    assert "kotu" in plan.terms


# ---------------------------------------------------------------------------
# Gemini answer handling
# ---------------------------------------------------------------------------

def test_flatten_structured_answer():
    answer = json.dumps({
        "sinhala": ["කෙසෙල්"], "tamil": ["வாழைப்பழம்"],
        "singlish": ["kesel", "Kesel", "kehel"], "tamilish": [], "english": ["plantain"],
    })
    assert _flatten(answer) == "කෙසෙල්, வாழைப்பழம், kesel, kehel, plantain"


def test_flatten_falls_back_to_plain_text():
    assert _flatten("kesel, kehel,\nplantain") == "kesel, kehel, plantain"


# --- 2026-10-25: rarer typos, short words, words typed apart -----------------


def test_a_word_on_one_label_does_not_block_typo_matching():
    # One product reads "Suger Added"; "suger" must still find the sugar.
    vocab = cs.Vocabulary({"suger": 1, "sugar": 9})
    words = vocab.expand("suger")
    assert words["sugar"] == cs.W_TYPO[1]
    assert words["suger"] == cs.W_TYPO[1]  # level, so popularity decides


def test_an_established_word_is_still_not_fuzzed():
    vocab = cs.Vocabulary({"miris": 12, "maris": 4})
    assert "maris" not in vocab.expand("miris")


def test_unknown_four_letter_word_gets_one_careful_edit():
    vocab = cs.Vocabulary({"milk": 30, "kream": 4, "kari": 6, "nilk": 1})
    assert "milk" in vocab.expand("mlik")          # swapped letters
    assert "kream" in vocab.expand("krem")         # a missing letter
    assert "kari" not in vocab.expand("kiri")      # a vowel changes the word
    assert "milk" not in vocab.expand("bilk")      # first letter must agree


def test_words_typed_apart_match_the_joined_catalogue_word():
    vocab = cs.Vocabulary({"ratakiri": 1, "kiri": 40, "lakspray": 2, "spray": 9})
    plan = cs.make_plan(vocab, "rata kiri")
    assert "ratakiri" in plan.expansions["rata"] and "ratakiri" in plan.expansions["kiri"]
    plan = cs.make_plan(vocab, "lak spray")
    assert "lakspray" in plan.expansions["lak"]


def test_search_benchmark_on_the_production_catalogue_holds():
    """Offline: 258 real-world queries over the 2026-10-25 catalogue snapshot."""
    from scripts.search_benchmark import run

    summary = run()["summary"]
    assert summary["top3_rate"] >= 0.97, summary


def test_a_rare_word_yields_only_to_a_more_common_spelling_of_itself():
    vocab = cs.Vocabulary({"suger": 1, "sugar": 9, "super": 9, "mutai": 2, "muti": 8, "detol": 2, "detox": 1})
    suger = vocab.expand("suger")
    assert "sugar" in suger and "super" not in suger   # a changed consonant is a new word
    assert "muti" not in vocab.expand("mutai")          # Tamil egg is not hair
    assert "detox" not in vocab.expand("detol")         # not more common, not a slip


def test_short_typos_add_or_drop_only_a_vowel():
    vocab = cs.Vocabulary({"pents": 3, "kream": 4})
    assert "pents" not in vocab.expand("pens")
    assert "kream" in vocab.expand("krem")
