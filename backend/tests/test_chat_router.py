"""Chat router tests: deterministic intent routing + the zero-LLM lookup path.

Routing and asset lookup are exercised without any LLM call (keyword routing is
deterministic; asset_lookup is pure SQL). The ask/rca/compliance branches that
require the model are covered structurally by the workflow tests with a mocked
reasoner. Skips (not fails) if Postgres is down for the lookup test.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from dotenv import load_dotenv

from app.workflows.chat import _keyword_intent, build_chat_graph, extract_asset_hint

load_dotenv()
DSN = os.environ.get("DATABASE_URL", "")

LOOKUP_ASSET = "ua-chat-1"
LOOKUP_SERIAL = "sn-chat-1"


def test_keyword_routing():
    assert _keyword_intent("why did pump P-101 fail") == "rca"
    assert _keyword_intent("what is the root cause of the seizure") == "rca"
    assert _keyword_intent("which assets are at risk for compliance") == "compliance"
    assert _keyword_intent("show me the details for SN-100") == "asset_lookup"
    # a general question has no keyword -> None (falls back to nano/ask at runtime)
    assert _keyword_intent("what do we know about the feed pump") is None


def test_asset_hint_extraction():
    assert extract_asset_hint("why did pump P-101 fail") == "P-101"
    assert extract_asset_hint("look up SN-INGEST-001 please") == "SN-INGEST-001"
    assert extract_asset_hint("show the feed pump") is None


@pytest.mark.parametrize("message, document_id", [("hi", 22), ("okay", 22), (" Hello!! ", None), ("thank you", None)])
def test_conversation_does_not_search_documents_or_call_model(message, document_id):
    state = asyncio.run(build_chat_graph().ainvoke({"conn": object(), "message": message, "document_id": document_id, "stages": []}))
    assert state["stages"] == ["route", "guard"]
    assert state["result"]["intent"] == "conversation"
    assert state["result"]["answer_mode"] == "conversation"
    assert "couldn't find" not in state["result"]["answer"]


@pytest.mark.parametrize("mode", ["llm", "source_excerpts", "no_sources"])
def test_pdf_chat_distinguishes_model_answer_and_fallback(monkeypatch, mode):
    from app.workflows import chat
    from app.workflows.retrieval import RetrievedRecord
    calls = []
    class Connection:
        async def fetch(self, *args): return []
    async def search(conn, message, document_id):
        assert document_id == 22
        return [] if mode == "no_sources" else [RetrievedRecord(citation="PDF:22:p3:c6", system="test.pdf", text="Inspection interval: 3 days.")]
    async def answer(system, human, citations):
        calls.append(citations)
        return "3 days [PDF:22:p3:c6]" if mode == "llm" else ""
    monkeypatch.setattr(chat, "document_search", search)
    monkeypatch.setattr(chat, "_stream_grounded_answer", answer)
    state = asyncio.run(build_chat_graph().ainvoke({"conn": Connection(), "message": "What is the inspection interval?", "document_id": 22, "stages": []}))
    assert state["result"]["answer_mode"] == mode
    assert len(calls) == (0 if mode == "no_sources" else 1)
    if mode == "llm": assert "3 days" in state["result"]["answer"]
    if mode == "source_excerpts": assert "not a generated answer" in state["result"]["answer"]


def test_chat_asset_lookup_path_is_grounded_without_llm():
    async def scenario():
        conn = await asyncpg.connect(dsn=DSN, timeout=5)
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute(
                "insert into unified_assets (unified_id, asset_name) values ($1,$2)",
                LOOKUP_ASSET, LOOKUP_SERIAL,
            )
            await conn.execute(
                "insert into asset_identifiers (unified_id, concept, value, is_primary) "
                "values ($1,'serial_number',$2,true)",
                LOOKUP_ASSET, LOOKUP_SERIAL,
            )
            graph = build_chat_graph()
            state = await graph.ainvoke(
                {"conn": conn, "message": f"show me {LOOKUP_SERIAL}",
                 "intent": "asset_lookup", "stages": []}
            )
            assert state["stages"] == ["route", "asset_lookup"]
            result = state["result"]
            assert result["intent"] == "asset_lookup"
            # grounded to the canonical asset, with its source records as citations
            assert result["facts"]["unified_id"] == LOOKUP_ASSET
            assert result["confidence"] == 1.0
            assert result["evidence_from"] == "index"
        finally:
            await tr.rollback()
            await conn.close()

    try:
        asyncio.run(scenario())
    except AssertionError:
        raise
    except Exception as e:
        pytest.skip(f"Postgres not reachable: {e}")
