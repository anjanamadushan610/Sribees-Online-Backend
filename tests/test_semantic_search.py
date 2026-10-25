"""Meaning-based lookup: ranking, staleness, and how the agent uses it."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import assistant_agent, semantic_search
from app.services.chat_service import ChatService


def row(pid, name, key=None, **kw):
    return SimpleNamespace(
        product_id=pid, name=name, name_si=kw.get("si"), name_ta=None, search_keywords=kw.get("kw"),
        category="Health", subcategory="Medicines & Balms", search_embedding_key=key,
    )


def test_rank_keeps_only_hits_near_the_best():
    vectors = {"a": [1.0, 0.0], "b": [0.9, 0.436], "c": [0.0, 1.0]}
    assert semantic_search.rank([1.0, 0.0], vectors, limit=5) == ["a", "b"]


def test_key_changes_with_text_and_model(monkeypatch):
    doc = semantic_search.document_text(row("1", "Siddhalepa Balm 2.5g", si="Siddhalepa බාම් 2.5g", kw="balm"))
    assert "Siddhalepa බාම්" in doc and "Medicines & Balms" in doc and "balm" in doc
    key = semantic_search.embedding_key(doc)
    assert key == semantic_search.embedding_key(doc)
    assert key != semantic_search.embedding_key(doc + " | new keyword")
    monkeypatch.setattr(semantic_search.settings, "gemini_embedding_model", "other-model")
    assert key != semantic_search.embedding_key(doc)


class FakeSession:
    def __init__(self, rows, locked=True):
        self.rows, self.locked, self.saved, self.commits = rows, locked, [], 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, params=None):
        sql = str(statement)
        result = SimpleNamespace()
        if "pg_try_advisory_lock" in sql:
            result.scalar = lambda: self.locked
        elif sql.strip().startswith("UPDATE"):
            self.saved.append(params)
        elif "FROM products p" in sql:
            result.all = lambda: self.rows
        return result

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_refresh_embeds_only_changed_products(monkeypatch):
    fresh_doc = semantic_search.document_text(row("p1", "Balm"))
    rows = [row("p1", "Balm", key=semantic_search.embedding_key(fresh_doc)), row("p2", "Cough Syrup")]
    session = FakeSession(rows)
    embed = AsyncMock(return_value=[[1.0, 0.0]])
    monkeypatch.setattr(semantic_search, "embed", embed)
    done = await semantic_search.refresh_embeddings(lambda: session)
    assert done == 1
    assert [s["product_id"] for s in session.saved] == ["p2"]
    assert embed.call_args.args[1] == "RETRIEVAL_DOCUMENT"


@pytest.mark.asyncio
async def test_refresh_skips_when_another_worker_holds_the_lock(monkeypatch):
    session = FakeSession([row("p2", "Cough Syrup")], locked=False)
    monkeypatch.setattr(semantic_search, "embed", AsyncMock())
    assert await semantic_search.refresh_embeddings(lambda: session) == 0
    assert not session.saved


SYRUP = {"product_id": "p-syrup", "name": "Siddhalepa Cough Syrup 100ml", "price": 450, "discount_price": None, "is_on_sale": False, "variants": []}


async def search_once(monkeypatch, query="cold medicine", enabled=True, **args):
    monkeypatch.setattr(assistant_agent.settings, "semantic_search_enabled", enabled)
    monkeypatch.setattr(ChatService, "_match_products", staticmethod(AsyncMock(return_value=([], []))))
    lookup = AsyncMock(return_value=["p-syrup", "p-elsewhere"])
    monkeypatch.setattr(semantic_search, "related_product_ids", lookup)
    from app.services import assistant_shopping

    monkeypatch.setattr(
        assistant_shopping, "catalog_product",
        AsyncMock(side_effect=lambda db, b, pid, lang: SYRUP if pid == "p-syrup" else None),
    )
    turn = assistant_agent.Turn("en")
    ctx = {"db": None, "redis": None, "user_id": None, "branch_id": "b", "history": [], "emit": AsyncMock()}
    out = await assistant_agent._run_tool("search_products", {"query": query, **args}, ctx, turn)
    return out, turn, lookup


@pytest.mark.asyncio
async def test_thin_keyword_search_adds_products_related_by_meaning(monkeypatch):
    out, turn, _ = await search_once(monkeypatch)
    assert [p["product_id"] for p in out["related_by_meaning"]] == ["p-syrup"]  # p-elsewhere: not at this branch
    assert "note" not in out and "really fit" in out["related_note"]
    assert turn.search_order == ["p-syrup"]


@pytest.mark.asyncio
async def test_meaning_lookup_respects_switch_and_price_cap(monkeypatch):
    out, _, lookup = await search_once(monkeypatch, enabled=False)
    assert "related_by_meaning" not in out and not lookup.called
    out, _, _ = await search_once(monkeypatch, max_price=300)
    assert "related_by_meaning" not in out and "Try once more" in out["note"]
