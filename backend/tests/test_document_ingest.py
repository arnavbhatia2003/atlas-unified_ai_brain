"""Chain-of-custody tests for document (PDF) ingest.

Safety-critical per the testing steering: the ordering parse -> DURABLE SAVE ->
post-save-processing must guarantee two things, and we test BOTH explicitly:

  * failure BEFORE the durable save  -> nothing persisted, corpus unchanged
  * failure AFTER the durable save   -> raw parse retained + recoverable

These use an in-memory fake store (no DB, no Docling) so the invariants are
verified deterministically, not merely described.
"""

from __future__ import annotations

import asyncio

from app.services.document_ingest import DocumentStore, ingest_document
from app.services.documents import ParsedDocument, sha256_of


def _mk_parsed(sha: str) -> ParsedDocument:
    return ParsedDocument(
        filename="f.pdf",
        sha256=sha,
        parser="test",
        page_count=1,
        title="t",
        markdown="hello world",
        sections=[],
        tables=[],
        chunks=[],
        raw={},
    )


class FakeStore(DocumentStore):
    """In-memory DocumentStore that records every mutation for assertions."""

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self._next = 1
        self.save_calls = 0

    async def sha_status(self, sha256: str) -> str | None:
        for r in self.rows.values():
            if r["sha"] == sha256:
                return r["status"]
        return None

    async def save_parse(self, system, parsed, doc_type):  # type: ignore[override]
        self.save_calls += 1
        pid = self._next
        self._next += 1
        self.rows[pid] = {
            "sha": parsed.sha256,
            "system": system,
            "doc_type": doc_type,
            "status": "parsed",
            "parsed": parsed,
            "error": None,
        }
        return pid

    async def mark_processed(self, parse_id: int) -> None:
        self.rows[parse_id]["status"] = "processed"

    async def mark_error(self, parse_id: int, error: str) -> None:
        self.rows[parse_id]["status"] = "error"
        self.rows[parse_id]["error"] = error

    async def load_parse(self, parse_id: int):  # type: ignore[override]
        row = self.rows.get(parse_id)
        return row["parsed"] if row else None


def test_failure_before_durable_save_persists_nothing():
    """Parse throws -> the store is never touched; corpus unchanged."""
    store = FakeStore()

    async def boom_parser(_data: bytes, _name: str) -> ParsedDocument:
        raise RuntimeError("parse blew up")

    res = asyncio.run(
        ingest_document(store, "sys", "f.pdf", b"data", parser=boom_parser)
    )

    assert res.status == "error"
    assert res.parse_id is None
    assert res.error and "parse blew up" in res.error
    # The custody invariant: absolutely nothing was persisted.
    assert store.save_calls == 0
    assert store.rows == {}


def test_failure_after_durable_save_retains_recoverable_parse():
    """Post-save processing throws -> the parse is saved, marked error, and
    remains recoverable / re-runnable (the source is never lost)."""
    store = FakeStore()
    parsed = _mk_parsed("sha-after")

    async def ok_parser(_data: bytes, _name: str) -> ParsedDocument:
        return parsed

    async def boom_post(_store, _pid, _parsed) -> None:
        raise RuntimeError("extraction failed downstream")

    res = asyncio.run(
        ingest_document(
            store, "sys", "f.pdf", b"data", parser=ok_parser, post_save=boom_post
        )
    )

    assert res.status == "error"
    assert res.parse_id is not None
    assert store.save_calls == 1  # the durable save DID happen
    row = store.rows[res.parse_id]
    assert row["status"] == "error"  # flagged for retry
    assert row["error"] and "extraction failed" in row["error"]

    # Recoverable: the raw parse reloads, and reprocessing can complete.
    async def recover() -> None:
        again = await store.load_parse(res.parse_id)
        assert again is not None and again.sha256 == "sha-after"
        await store.mark_processed(res.parse_id)

    asyncio.run(recover())
    assert store.rows[res.parse_id]["status"] == "processed"


def test_success_saves_then_processes():
    store = FakeStore()
    seen: dict[str, int] = {}

    async def ok_parser(data: bytes, _name: str) -> ParsedDocument:
        return _mk_parsed(sha256_of(data))

    async def post(_store, pid: int, _parsed) -> None:
        seen["pid"] = pid

    res = asyncio.run(
        ingest_document(store, "sys", "f.pdf", b"data", parser=ok_parser, post_save=post)
    )

    assert res.status == "processed"
    assert res.parse_id is not None
    assert seen["pid"] == res.parse_id  # post-save ran AFTER the save
    assert store.rows[res.parse_id]["status"] == "processed"


def test_duplicate_content_is_skipped():
    store = FakeStore()

    async def ok_parser(data: bytes, _name: str) -> ParsedDocument:
        return _mk_parsed(sha256_of(data))

    first = asyncio.run(ingest_document(store, "sys", "f.pdf", b"same", parser=ok_parser))
    second = asyncio.run(ingest_document(store, "sys", "f.pdf", b"same", parser=ok_parser))

    assert first.status == "processed"
    assert second.status == "skipped"
    assert store.save_calls == 1  # the identical file was not re-saved


def test_storage_failure_is_reported_without_claiming_success():
    class UnavailableStore(FakeStore):
        async def save_parse(self, *args):
            raise RuntimeError("database unavailable")
    async def parser(data, filename):
        return _mk_parsed(sha256_of(data))
    store=UnavailableStore()
    result=asyncio.run(ingest_document(store,"test","test.pdf",b"data",parser=parser))
    assert result.status=="error"
    assert result.parse_id is None
    assert "Durable save failed" in result.error
