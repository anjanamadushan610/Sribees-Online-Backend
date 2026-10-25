"""
The assistant answers in the customer's language, not the app's.

Rules (app/services/chat_language.py): Tamil script or romanised Tamil,
alone or mixed with English -> Tamil script. Sinhala script or Singlish,
alone or mixed with English -> Sinhala script. Plain English -> English.
Nothing to go on -> the conversation's language, then the app's.
"""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.schemas.chat import ChatPlan
from app.services import chat_language
from app.services.chat_service import ChatService
from app.services.embedding_service import GeminiAPIError


@pytest.fixture(autouse=True)
def _planner_path(monkeypatch):
    """These tests drive the single-call planner (ASSISTANT_AGENT_V2=false)."""
    from app.config.settings import settings

    monkeypatch.setattr(settings, "assistant_agent_v2", False)

TAMIL = (chr(0x0B80), chr(0x0BFF))
SINHALA = (chr(0x0D80), chr(0x0DFF))


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("මට කිරි පැකට් එකක් ඕනෙ", ("script", "si")),
        ("Anchor milk powder 400g තියෙනවද?", ("script", "si")),
        ("mata kiri packet ekak one", ("romanised", "si")),
        ("mage order eka ko", ("romanised", "si")),
        ("Anchor milk powder 400g thiyenawada", ("romanised", "si")),
        ("parippu 1kg", ("romanised", "si")),
        ("எனக்கு பால் வேண்டும்", ("script", "ta")),
        ("Anchor milk powder 400g இருக்கா?", ("script", "ta")),
        ("enakku paal venum", ("romanised", "ta")),
        ("arisi 5kg irukka", ("romanised", "ta")),
        ("Anchor milk powder irukkuma", ("romanised", "ta")),
        # Mostly Tamil letters, one Sinhala word: the larger script wins.
        ("பால் வேண்டும் කිරි", ("script", "ta")),
        ("Do you have Anchor milk powder?", ("open", None)),
        ("rice 5kg", ("open", None)),
        ("ok", ("neutral", None)),
        ("Thank you!", ("neutral", None)),
        ("2", ("neutral", None)),
        ("", ("neutral", None)),
    ],
)
def test_detects_the_customers_language(message, expected):
    assert chat_language.detect(message) == expected


def test_words_both_languages_share_do_not_decide():
    # "kudu" is Sinhala powder and Tamil "give"; "podi" and "malli" are Tamil
    # ingredients -- none of them may push a message into the wrong script.
    assert chat_language.detect("chili podi") == ("open", None)
    assert chat_language.detect("malli 100g") == ("open", None)
    assert chat_language.detect("kiri kudu") == ("romanised", "si")


def test_a_message_with_nothing_to_go_on_keeps_the_conversation_language():
    assert chat_language.fixed_language("ok", "ta") == "ta"
    assert chat_language.fixed_language("", "si") == "si"
    assert chat_language.fixed_language("rice 5kg", "si") is None


def test_settled_language_beats_the_model_and_the_model_beats_the_fallback():
    assert chat_language.final_language("si", "en", "ta") == "si"
    assert chat_language.final_language(None, "en", "si") == "en"
    assert chat_language.final_language(None, None, "ta") == "ta"
    assert chat_language.final_language(None, "fr", "si") == "si"


def test_a_missing_model_choice_is_read_from_the_script_of_its_reply():
    # Seen live: "seeni 1kg" answered in Sinhala with reply_language omitted.
    assert chat_language.final_language(None, None, "en", "සීනි සොයමින්") == "si"
    assert chat_language.final_language(None, None, "si", "மிளகாய் தூள்") == "ta"
    assert chat_language.final_language(None, None, "si", "Looking now") == "en"
    assert chat_language.final_language(None, None, "ta", "😊") == "ta"
    # A settled language is never second-guessed by the reply.
    assert chat_language.final_language("si", None, "en", "Looking now") == "si"


def test_last_language_reads_the_latest_assistant_turn_only():
    history = [
        {"role": "assistant", "text": "x", "language": "ta"},
        {"role": "user", "text": "y"},
        {"role": "assistant", "text": "z", "language": "si"},
    ]
    assert chat_language.last_language(history) == "si"
    assert chat_language.last_language([{"role": "assistant", "text": "old"}]) is None
    assert chat_language.last_language([]) is None


def test_written_in_rejects_romanised_and_wrong_script_replies():
    assert chat_language.written_in("ආයුබෝවන්", "si")
    assert not chat_language.written_in("Ow, kiyanna!", "si")
    assert chat_language.written_in("வணக்கம்", "ta")
    assert not chat_language.written_in("Vanakkam", "ta")
    assert chat_language.written_in("Hello", "en")
    assert not chat_language.written_in("ආයුබෝවන්", "en")


def test_instruction_tells_a_settled_language_and_explains_an_open_one():
    fixed = chat_language.instruction("ta", "si")
    assert "Tamil script" in fixed and "reply_language to 'ta'" in fixed
    open_ = chat_language.instruction(None, "si")
    assert "LATEST message" in open_ and "use Sinhala" in open_
    assert "never from the app's language" in open_


def test_the_model_settles_language_before_writing_the_reply():
    fields = list(ChatPlan.model_json_schema()["properties"])
    assert fields.index("reply_language") < fields.index("reply")


def _redis(history=None):
    redis = AsyncMock()
    redis.get.return_value = json.dumps(history) if history is not None else None
    return redis


@pytest.mark.asyncio
async def test_singlish_in_an_english_app_is_answered_in_sinhala(monkeypatch):
    ask = AsyncMock(
        return_value=ChatPlan(
            reply="ඔව්, මම උදව් කරන්නම්", intent="chitchat"
        ).model_dump()
    )
    monkeypatch.setattr(ChatService, "_ask_gemini", ask)
    redis = _redis()
    result = await ChatService.chat(
        AsyncMock(), redis, "guest:a", None, "mata udaw ekak one", lang="en"
    )
    assert ask.call_args.kwargs["lang"] == "si"
    assert result["language"] == "si"
    assert result["reply"] == "ඔව්, මම උදව් කරන්නම්"
    stored = json.loads(redis.set.call_args.args[1])
    assert stored[-1]["language"] == "si"


@pytest.mark.asyncio
async def test_english_in_a_sinhala_app_is_answered_in_english(monkeypatch):
    ask = AsyncMock(
        return_value=ChatPlan(
            reply_language="en", reply="Happy to help!", intent="chitchat"
        ).model_dump()
    )
    monkeypatch.setattr(ChatService, "_ask_gemini", ask)
    result = await ChatService.chat(
        AsyncMock(), _redis(), "guest:a", None, "Can you help me?", lang="si"
    )
    # Plain English has no marker to settle it, so the model decides.
    assert ask.call_args.kwargs["lang"] is None
    assert result["language"] == "en"
    assert result["reply"] == "Happy to help!"


@pytest.mark.asyncio
async def test_a_follow_up_ok_continues_in_tamil(monkeypatch):
    ask = AsyncMock(
        return_value=ChatPlan(reply="சரி 😊", intent="chitchat").model_dump()
    )
    monkeypatch.setattr(ChatService, "_ask_gemini", ask)
    history = [
        {"role": "user", "text": "paal venum"},
        {"role": "assistant", "text": "இதோ", "language": "ta"},
    ]
    result = await ChatService.chat(
        AsyncMock(), _redis(history), "guest:a", None, "ok", lang="si"
    )
    assert ask.call_args.kwargs["lang"] == "ta"
    assert result["language"] == "ta"


@pytest.mark.asyncio
async def test_shortcut_buttons_answer_in_the_conversation_language(monkeypatch):
    ask = AsyncMock()
    monkeypatch.setattr(ChatService, "_ask_gemini", ask)
    history = [{"role": "assistant", "text": "இதோ", "language": "ta"}]
    result = await ChatService.chat(
        AsyncMock(),
        _redis(history),
        "guest:a",
        None,
        "",
        lang="en",
        task="app_help",
        target="checkout",
    )
    ask.assert_not_called()
    assert result["language"] == "ta"
    assert any(TAMIL[0] <= ch <= TAMIL[1] for ch in result["actions"][0]["label"])


@pytest.mark.asyncio
async def test_outage_fallback_follows_the_customers_language(monkeypatch):
    monkeypatch.setattr(
        ChatService, "_ask_gemini", AsyncMock(side_effect=GeminiAPIError("offline"))
    )
    result = await ChatService.chat(
        AsyncMock(), _redis(), "guest:a", None, "arisi irukka", lang="en"
    )
    assert result["degraded"] and result["language"] == "ta"
    assert any(TAMIL[0] <= ch <= TAMIL[1] for ch in result["reply"])


@pytest.mark.asyncio
async def test_a_reply_in_the_wrong_script_is_replaced(monkeypatch):
    # Told Sinhala, the model answered in Singlish: the customer gets the
    # Sinhala fallback, never romanised text.
    monkeypatch.setattr(
        ChatService,
        "_ask_gemini",
        AsyncMock(
            return_value=ChatPlan(reply="Ow, kiyanna!", intent="chitchat").model_dump()
        ),
    )
    result = await ChatService.chat(
        AsyncMock(), _redis(), "guest:a", uuid4(), "mage order eka ko", lang="en"
    )
    assert result["language"] == "si"
    assert any(SINHALA[0] <= ch <= SINHALA[1] for ch in result["reply"])
