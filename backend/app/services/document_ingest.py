"""Document ingest — the chain-of-custody boundary for PDF sources.

The whole point of this module is a single, explicit, testable ordering:

    1. parse            (may fail — nothing has been persisted yet)
    2. DURABLE SAVE     ← the chain-of-custody boundary
    3. post-save work   (extraction/resolution; may fail — the parse SURVIVES)

Invariants (safety-critical, per the testing steering — verified by tests):
  * A failure BEFORE the durable save persists NOTHING and leaves the corpus
    unchanged.
  * A failure AFTER the durable save leaves the raw parse stored and recoverable
    (status='error'), so the source is never lost and processing is re-runnable.

The datastore is an injectable Protocol so the ordering can be tested with an
in-memory fake (deterministic, no DB), while production uses `PgDocumentStore`.
Parsing and post-save work are also injectable for the same reason.
"""

from __future__ import annotations

import json
import asyncio
from contextlib import suppress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .documents import (
    ParsedDocument,
    parse_pdf,
    parse_pdf_fast,
    parsed_from_dict,
    parsed_to_dict,
    sha256_of,
)

# Injection points (so both failure modes are unit-testable without Docling/DB).
Parser = Callable[[bytes, str], Awaitable[ParsedDocument]]
PostSave = Callable[["DocumentStore", int, ParsedDocument], Awaitable[None]]
DocTypeInfer = Callable[[ParsedDocument], str]


class DocumentStore(Protocol):
    """Persistence surface for document parses (the custody record)."""

    async def sha_status(self, sha256: str) -> str | None:
        """Return the stored status for this content hash, or None if unseen."""

    async def save_parse(
        self, system: str, parsed: ParsedDocument, doc_type: str | None
    ) -> int:
        """DURABLY store the parse (status='parsed') and return its id.

        This is the chain-of-custody boundary: once this returns, the source is
        recoverable even if everything after it fails.
        """

    async def mark_processed(self, parse_id: int) -> None:
        ...

    async def mark_error(self, parse_id: int, error: str) -> None:
        ...

    async def load_parse(self, parse_id: int) -> ParsedDocument | None:
        """Reload a stored parse (for recovery / reprocessing)."""


@dataclass
class IngestResult:
    status: str  # 'processed' | 'error' | 'skipped'
    parse_id: int | None
    parsed: ParsedDocument | None
    error: str | None = None


async def ingest_document(
    store: DocumentStore,
    system: str,
    filename: str,
    data: bytes,
    *,
    parser: Parser = parse_pdf,
    post_save: PostSave | None = None,
    doc_type_infer: DocTypeInfer | None = None,
) -> IngestResult:
    """Parse a PDF and durably store it before any downstream processing.

    Returns an IngestResult; never raises for the two designed failure modes —
    it reports them so the caller (SSE endpoint) can surface status while the
    custody invariants above always hold.
    """
    sha = sha256_of(data)

    # Dedupe: skip only if FULLY processed. A 'parsed' (saved but interrupted)
    # or 'error' doc is reprocessed, so an ingestion cut off mid-way (e.g. the
    # browser navigated away before background jobs existed) recovers on re-run.
    existing = await store.sha_status(sha)
    if existing == "processed":
        save_original = getattr(store, "save_original", None)
        if save_original is not None:
            await save_original(sha, data)
        return IngestResult(status="skipped", parse_id=None, parsed=None)

    # --- 1. parse (PRE-durable) — a failure here persists NOTHING ----------
    try:
        parsed = await parser(data, filename)
    except Exception as exc:  # noqa: BLE001 - report, persist nothing
        return IngestResult(status="error", parse_id=None, parsed=None, error=str(exc))

    doc_type = None
    if doc_type_infer is not None:
        try:
            doc_type = doc_type_infer(parsed)
        except Exception:
            doc_type = None

    # --- 2. DURABLE SAVE (chain-of-custody boundary) -----------------------
    try:
        parse_id = await store.save_parse(system, parsed, doc_type)
    except Exception as exc:
        return IngestResult(status="error", parse_id=None, parsed=parsed, error=f"Durable save failed: {type(exc).__name__}")

    # --- 3. post-save processing — a failure here KEEPS the stored parse ---
    if post_save is not None:
        try:
            await post_save(store, parse_id, parsed)
        except Exception as exc:  # noqa: BLE001 - parse retained + recoverable
            await store.mark_error(parse_id, str(exc))
            return IngestResult(
                status="error", parse_id=parse_id, parsed=parsed, error=str(exc)
            )

    await store.mark_processed(parse_id)
    return IngestResult(status="processed", parse_id=parse_id, parsed=parsed)


class PgDocumentStore:
    """Postgres-backed DocumentStore (the `document_parses` table).

    Pool-backed with short-lived per-call connections, so a long-running parse
    never holds a pooled connection open.
    """

    def __init__(self, pool: Any, extract_assets: bool = False, force: bool = False) -> None:
        self._pool = pool
        self._extract_assets = extract_assets
        self._force = force

    async def sha_status(self, sha256: str) -> str | None:
        if self._force:
            return None
        async with self._pool.acquire() as conn:
            status = await conn.fetchval("select status from document_parses where sha256 = $1", sha256)
            if self._extract_assets and status == "processed":
                extracted = await conn.fetchval("select exists(select 1 from doc_extractions where sha256=$1)", sha256)
                if not extracted:
                    return "parsed"
            return status

    async def save_original(self, sha256: str, data: bytes) -> None:
        import base64
        async with self._pool.acquire() as conn:
            await conn.execute("update document_parses set parsed=jsonb_set(parsed, '{raw,original_pdf_b64}', to_jsonb($2::text)) where sha256=$1", sha256, base64.b64encode(data).decode("ascii"))

    async def save_parse(
        self, system: str, parsed: ParsedDocument, doc_type: str | None
    ) -> int:
        from .document_index import replace_document_chunks
        async with self._pool.acquire() as conn, conn.transaction():
            parse_id = await conn.fetchval(
                """
                insert into document_parses
                    (system, filename, sha256, doc_type, parser, page_count,
                     title, parsed, status)
                values ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,'parsed')
                on conflict (sha256) do update set status = 'parsed', error = null, parsed = excluded.parsed, parser = excluded.parser, page_count = excluded.page_count
                returning id
                """,
                system,
                parsed.filename,
                parsed.sha256,
                doc_type,
                parsed.parser,
                parsed.page_count,
                parsed.title,
                json.dumps(parsed_to_dict(parsed)),
            )
            await replace_document_chunks(conn, parse_id, parsed)
            return parse_id

    async def mark_processed(self, parse_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "update document_parses set status='processed', "
                "processed_at=now(), error=null where id=$1",
                parse_id,
            )

    async def mark_error(self, parse_id: int, error: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "update document_parses set status='error', error=$2 where id=$1",
                parse_id,
                error[:4000],
            )

    async def load_parse(self, parse_id: int) -> ParsedDocument | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "select parsed from document_parses where id=$1", parse_id
            )
        if row is None:
            return None
        data = json.loads(row) if isinstance(row, str) else row
        return parsed_from_dict(data)


def _clean_system_name(filename: str) -> str:
    """A stable, human-ish source name for a document (the connector name)."""
    import os

    base = os.path.splitext(os.path.basename(filename))[0]
    base = base.replace("_", " ").replace("-", " ").strip()
    return " ".join(w for w in base.split() if w)[:80] or "Document"


async def _ensure_document_connector(pool: Any, system: str, doc_type: str | None) -> None:
    """Register a `kind='document'` connector so the PDF shows up as a source."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into connectors (name, description, kind, status)
            values ($1, $2, 'document', 'idle')
            on conflict (name) do nothing
            """,
            system,
            doc_type or "Document (PDF)",
        )


async def ingest_document_events(
    system: str,
    filename: str,
    data: bytes,
    *,
    post_save: PostSave | None = None,
    doc_type_infer: DocTypeInfer | None = None,
):
    """SSE-friendly document ingest: parse -> durable save -> (post-save).

    Yields dict events (parse / saved / complete / skipped / error). The heavy
    parse runs off-thread inside `ingest_document`; no pooled connection is held
    during it (PgDocumentStore acquires per call).
    """
    from app.db import get_pool

    pool = get_pool()
    store = PgDocumentStore(pool)

    yield {"step": "parse", "message": f"Parsing {filename} with Docling…", "filename": filename}
    res = await ingest_document(
        store, system, filename, data, post_save=post_save, doc_type_infer=doc_type_infer
    )

    if res.status == "skipped":
        yield {"step": "skipped", "message": f"{filename}: already ingested (identical content)", "filename": filename}
        return
    if res.status == "error":
        where = (
            "after durable save — the parse is retained and recoverable"
            if res.parse_id
            else "before durable save — nothing was persisted"
        )
        yield {
            "step": "error",
            "message": f"{filename}: {res.error} (failed {where})",
            "filename": filename,
            "parse_id": res.parse_id,
        }
        return

    stats = res.parsed.stats() if res.parsed else {}
    await _ensure_document_connector(pool, system, None)
    yield {
        "step": "complete",
        "message": (
            f"Ingested {filename}: {stats.get('pages', 0)} page(s), "
            f"{stats.get('chunks', 0)} passage(s), {stats.get('tables', 0)} table(s) — "
            f"durably stored (chain-of-custody)"
        ),
        "filename": filename,
        "parse_id": res.parse_id,
        "stats": stats,
    }


# --- generic extraction cache + corpus storage (Phase 2) -------------------
async def _extract_and_cache(pool: Any, parsed: ParsedDocument):
    """Return (records, doc_type) for a parse, using the doc_extractions cache so
    reprocessing never re-calls the LLM (respects the NIM rate budget)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "select records, doc_type from doc_extractions where sha256 = $1",
            parsed.sha256,
        )
    if row is not None:
        recs = row["records"]
        recs = json.loads(recs) if isinstance(recs, str) else recs
        parsed.raw["asset_extraction"] = {"status": "cached", "records": len(recs or []), "coverage": "unknown (legacy cache)"}
        return list(recs or []), row["doc_type"]

    from app.services.doc_extract import classify_doc_type, extract_document_records

    records = await extract_document_records(parsed)
    doc_type = await classify_doc_type(parsed)
    async with pool.acquire() as conn:
        await conn.execute(
            "insert into doc_extractions (sha256, doc_type, records) "
            "values ($1,$2,$3::jsonb) on conflict (sha256) do update set "
            "records = excluded.records, doc_type = excluded.doc_type",
            parsed.sha256,
            doc_type,
            json.dumps(records),
        )
    return records, doc_type


async def _store_source_records(pool: Any, system: str, records: list[dict]) -> int:
    """Durably insert extracted mention records into source_records (dedup by
    system+record_id), mirroring the pipeline's raw-record convention."""
    n = 0
    async with pool.acquire() as conn:
        for rec in records:
            rid = str(rec.get("record_id"))
            if not rid:
                continue
            full = f"{system}:{rid}"
            inserted = await conn.fetchval(
                "insert into source_records (system, record_id, raw_json) "
                "values ($1,$2,$3::jsonb) on conflict (system, record_id) "
                "do nothing returning id",
                system,
                full,
                json.dumps({**rec, "record_id": rid}),
            )
            if inserted is not None:
                n += 1
    return n


async def _set_parse_doc_type(pool: Any, parse_id: int, doc_type: str | None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "update document_parses set doc_type=$2 where id=$1", parse_id, doc_type
        )


async def ingest_documents_events(items: list[tuple[str, str, bytes]], *, extract_assets: bool = False, force: bool = False):
    """Batch document ingest with chain-of-custody per doc, then ONE corpus
    re-resolution over everything (so a folder of PDFs resolves together).

    Per doc: parse -> DURABLE SAVE -> extract (cached) -> store records durably.
    After all docs: re-resolve the full corpus via the existing pipeline, which
    clusters the new mention records into unified assets and projects the
    physical + operational graph (operational edges come from `_event_kind`).
    """
    from app.db import get_pool

    pool = get_pool()
    store = PgDocumentStore(pool, extract_assets, force)

    yield {"step": "start", "message": f"Ingesting {len(items)} document(s)"}
    any_ok = False
    failed = 0
    processed_ids = []

    for system, filename, data in items:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow("select system,filename from document_parses where sha256=$1", sha256_of(data))
        if existing:
            system, filename = existing["system"], existing["filename"]
        yield {
            "step": "parse",
            "message": f"Reading {filename}: fast text, Docling layout/OCR for images and scans…",
            "filename": filename,
        }
        state: dict[str, Any] = {"records": 0, "doc_type": None}
        progress = asyncio.Queue()

        async def _post(_store, parse_id: int, parsed: ParsedDocument) -> None:
            await _ensure_document_connector(pool, system, None)
            progress.put_nowait({
                "step": "searchable" if parsed.chunks else "saved",
                "parse_id": parse_id,
                "message": f"{filename}: original saved; {len(parsed.chunks)} native passages available for questions. Layout/OCR and optional asset extraction may still be running.",
            })
            if parsed.raw.get("image_pages") or extract_assets:
                enriched = await parse_pdf(data, filename, ocr=extract_assets)
                await _store.save_parse(system, enriched, None)
                parsed.__dict__.update(enriched.__dict__)
            if not parsed.chunks:
                raise ValueError("No readable text found; blank pages or unreadable scans need manual transcription")
            if not extract_assets:
                async with pool.acquire() as conn:
                    cached = await conn.fetchval("select exists(select 1 from doc_extractions where sha256=$1)", parsed.sha256)
                parsed.raw["asset_extraction"] = {"status": "cached" if cached else "not_requested", "coverage": "unknown (legacy cache)" if cached else "none"}
                async with pool.acquire() as conn:
                    await conn.execute("update document_parses set parsed=$2::jsonb where id=$1", parse_id, json.dumps(parsed_to_dict(parsed)))
                return
            try:
                records, doc_type = await _extract_and_cache(pool, parsed)
                stored = await _store_source_records(pool, system, records)
            except Exception:
                parsed.raw.setdefault("asset_extraction", {})["status"] = "error"
                raise
            finally:
                async with pool.acquire() as conn:
                    await conn.execute("update document_parses set parsed=$2::jsonb where id=$1", parse_id, json.dumps(parsed_to_dict(parsed)))
            await _set_parse_doc_type(pool, parse_id, doc_type)
            state["records"] = stored if stored else len(records)
            state["doc_type"] = doc_type

        task = asyncio.create_task(ingest_document(store, system, filename, data, parser=parse_pdf_fast, post_save=_post))
        notice = asyncio.create_task(progress.get())
        try:
            done, _ = await asyncio.wait([task, notice], return_when=asyncio.FIRST_COMPLETED)
            if notice in done:
                yield notice.result()
            res = await task
        finally:
            for pending in (task, notice):
                if not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending

        if res.status == "skipped":
            any_ok = True
            yield {"step": "skip", "message": f"{filename}: already ingested", "filename": filename}
        elif res.status == "error":
            failed += 1
            where = (
                "after durable save — parse retained + recoverable"
                if res.parse_id
                else "before durable save — nothing persisted"
            )
            yield {
                "step": "doc_error",
                "message": f"{filename}: {res.error} ({where})",
                "filename": filename,
            }
        else:
            any_ok = True
            processed_ids.append(res.parse_id)
            await _ensure_document_connector(pool, system, state["doc_type"])
            st = res.parsed.stats() if res.parsed else {}
            yield {
                "step": "extracted",
                "message": (
                    f"{filename}: ready for document questions; "
                    + (f"{state['records']} asset mention(s); " if extract_assets else "asset extraction not requested; ")
                    + f"{st.get('pages', 0)} page(s) / {st.get('chunks', 0)} passage(s); "
                    f"{st.get('ocr_pages', 0)} OCR page(s); {st.get('unreadable_pages', 0)} unreadable page(s)"
                ),
                "filename": filename,
                "records": state["records"],
                "doc_type": state["doc_type"],
            }

    if any_ok and extract_assets:
        from app.services import pipeline

        yield {"step": "resolve", "message": "Resolving documents into the knowledge graph…"}
        try:
            async for ev in pipeline.ingest_records({}):
                if ev.get("step") == "start":
                    continue
                if ev.get("step") == "error":
                    raise ValueError(ev.get("message", "Graph projection failed"))
                yield ev
        except Exception as exc:
            for parse_id in processed_ids:
                await store.mark_error(parse_id, f"PDF is searchable; graph projection needs retry: {type(exc).__name__}")
            yield {"step": "error", "message": "PDF passages retained; graph projection failed. Reprocess to retry."}
            return

    yield {"step": "error" if failed else "done", "message": f"Document ingestion finished; {failed} file(s) failed"}
