"""Unified Assets Brain HTTP API (Layer 5, minus the chat/AI surface).

Sync endpoints for discovery/resolution/queries; an SSE endpoint for ingestion
that streams real per-step status (ui-ux "wait UX" requirement).
"""

from __future__ import annotations

import json
from typing import Any
from typing import Literal

from fastapi import APIRouter, File, Query, UploadFile, HTTPException
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel, Field

from app.db import get_pool
from app.engine.graph.model import asset_node, rule_node
from app.engine.graph.store import GraphStore
from app.engine.resolution import SourceRecord
from app.services import connectors, pipeline
from app.services.document_ingest import (
    _clean_system_name,
    ingest_documents_events,
)
from app.services.document_jobs import registry as job_registry
from app.workflows.chat import run_chat_events
from app.workflows.compliance import run_compliance_events
from app.workflows.rca import run_rca_events
from app.workflows.retrieval import (
    asset_okf,
    get_workflow_run,
    list_workflow_runs,
)


async def _sse(gen) -> Any:
    """Format a dict-event async generator as an SSE byte stream."""
    async for event in gen:
        yield pipeline.sse(event)

router = APIRouter(prefix="/api", tags=["unified-assets"])
_graph = GraphStore()

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class DiscoverRequest(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list)


class ResolveRequest(BaseModel):
    records: list[SourceRecord] = Field(default_factory=list)


class IngestRequest(BaseModel):
    systems: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=2000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    document_id: int | None = Field(default=None, gt=0)
    intent: str | None = None  # optional override (ask|rca|compliance|asset_lookup)
    history: list[ChatTurn] = Field(default_factory=list, max_length=6)


class RCARequest(BaseModel):
    question: str = ""
    asset: str | None = None   # asset id / identifier / name


class ComplianceRequest(BaseModel):
    question: str = ""
    rule: str | None = None
    asset: str | None = None


class ReviewAction(BaseModel):
    action: str  # merge | separate | dismiss


class ConnectorCreate(BaseModel):
    name: str
    description: str = ""
    kind: Literal["manual", "api"] = "manual"
    endpoint: str | None = None
    payload: Any = None


class SyncRequest(BaseModel):
    payload: Any = None  # optional inline records to store before syncing


@router.post("/discover")
async def discover(req: DiscoverRequest) -> dict[str, Any]:
    result = pipeline.run_discovery(req.records)
    return result.model_dump()


@router.post("/resolve")
async def resolve_endpoint(req: ResolveRequest) -> dict[str, Any]:
    result = pipeline.run_resolution(req.records)
    return result.model_dump()


@router.post("/ingest")
async def ingest(req: IngestRequest) -> StreamingResponse:
    """Run the full pipeline, streaming per-step status as SSE."""
    return StreamingResponse(
        pipeline.ingest_events(req.systems),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class IngestPathRequest(BaseModel):
    path: str  # a server-side PDF file OR a folder of PDFs (local dev only)
    extract_assets: bool = False


@router.post("/documents/ingest")
async def documents_ingest(files: list[UploadFile] = File(...), extract_assets: bool = Query(False)) -> dict[str, Any]:
    """Upload one or more PDFs. Reads the files within the request, then starts a
    BACKGROUND job (parse -> durable save -> extract -> resolve). Returns a job id
    immediately; the job keeps running even if the browser navigates away. Tail
    status via GET /documents/jobs/{id}/stream."""
    items: list[tuple[str, str, bytes]] = []
    if not 1 <= len(files) <= 5:
        raise HTTPException(400, "Upload between one and five PDFs")
    if job_registry.running() is not None:
        raise HTTPException(409, "An ingestion job is already running")
    total = 0
    for f in files:
        data = await f.read(25 * 1024 * 1024 + 1)  # must read while the request is open
        name = (f.filename or "document.pdf").replace("\\", "/").split("/")[-1]
        total += len(data)
        if len(data) > 25 * 1024 * 1024 or total > 50 * 1024 * 1024:
            raise HTTPException(413, "Limit: 25 MB per PDF, 50 MB per upload")
        if not name.lower().endswith(".pdf") or b"%PDF-" not in data[:1024]:
            raise HTTPException(415, "Only PDF files are accepted")
        items.append((_clean_system_name(name), name, data))
    label = (
        items[0][1] if len(items) == 1 else f"{len(items)} documents"
    ) if items else "documents"
    job = job_registry.start(
        "upload", label, lambda: ingest_documents_events(items, extract_assets=extract_assets)
    )
    return {"job_id": job.id, "status": job.status, "label": job.label}


def _read_path_items(path: str) -> tuple[list[tuple[str, str, bytes]], str | None]:
    """Read PDFs from a server-side file/folder into (system, name, bytes) items.
    Returns (items, error). Local-dev convenience (reads the server filesystem)."""
    import os
    from pathlib import Path

    if not os.path.exists(path):
        return [], f"Path not found: {path}"
    if os.path.isdir(path):
        pdfs = sorted(
            os.path.join(path, n) for n in os.listdir(path) if n.lower().endswith(".pdf")
        )
    else:
        pdfs = [path]
    if not pdfs:
        return [], "No PDF files found"
    items: list[tuple[str, str, bytes]] = []
    if len(pdfs) > 5:
        return [], "Ingest at most five PDFs at a time"
    total = 0
    for fp in pdfs:
        if not Path(fp).resolve().is_relative_to(Path(__file__).resolve().parents[3]):
            return [], "PDF paths must stay inside the project folder"
        if not fp.lower().endswith(".pdf") or os.path.getsize(fp) > 25 * 1024 * 1024:
            return [], "Only PDFs up to 25 MB are accepted"
        name = os.path.basename(fp)
        try:
            with open(fp, "rb") as fh:
                data = fh.read(25 * 1024 * 1024 + 1)
                total += len(data)
                if len(data) > 25 * 1024 * 1024 or total > 50 * 1024 * 1024 or b"%PDF-" not in data[:1024]:
                    return [], "Invalid PDF or upload size limit exceeded"
                items.append((_clean_system_name(name), name, data))
        except OSError:
            return [], f"Cannot read PDF: {name}"
    return items, None


@router.post("/documents/ingest-path")
async def documents_ingest_path(req: IngestPathRequest) -> dict[str, Any]:
    """Ingest PDFs from a server-side path (file or folder) as a BACKGROUND job.
    Returns a job id; tail via GET /documents/jobs/{id}/stream. Local-dev only."""
    from pathlib import Path
    candidate = Path(req.path).resolve()
    root = Path(__file__).resolve().parents[3]
    if not candidate.is_relative_to(root):
        raise HTTPException(403, "Server paths must stay inside the project folder; use PDF upload for other files")
    if job_registry.running() is not None:
        raise HTTPException(409, "An ingestion job is already running")
    items, err = _read_path_items(str(candidate))
    if err:
        return {"error": err}
    import os

    label = os.path.basename(req.path.rstrip("/\\")) or req.path
    job = job_registry.start(
        "path", f"{label} ({len(items)} PDF)", lambda: ingest_documents_events(items, extract_assets=req.extract_assets)
    )
    return {"job_id": job.id, "status": job.status, "label": job.label, "count": len(items)}


@router.get("/documents/jobs")
async def documents_jobs() -> dict[str, Any]:
    """List ingestion jobs (so the UI can reconnect to one still running)."""
    return {"jobs": job_registry.list(), "running": job_registry.running() is not None}


@router.get("/documents/jobs/{job_id}/stream")
async def documents_job_stream(job_id: str) -> StreamingResponse:
    """Tail a background ingestion job's status (SSE). Reconnect-safe: replays the
    full buffer then follows the live tail. Disconnecting does NOT stop the job."""
    job = job_registry.get(job_id)
    if job is None:

        async def _missing():
            yield {"step": "error", "message": "Job not found (it may have expired)"}

        return StreamingResponse(_sse(_missing()), media_type="text/event-stream", headers=_SSE_HEADERS)

    return StreamingResponse(
        _sse(job_registry.stream(job)),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/documents/jobs/{job_id}/cancel")
async def documents_job_cancel(job_id: str) -> dict[str, Any]:
    ok = await job_registry.cancel(job_id)
    return {"ok": ok}


@router.get("/documents")
async def list_documents() -> list[dict[str, Any]]:
    """List durably-stored document parses (chain-of-custody records)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "select id, system, filename, doc_type, parser, page_count, title, "
            "status, error, parsed_at, processed_at, parsed->'raw'->'unreadable_pages' as unreadable_pages, "
            "jsonb_array_length(coalesce(parsed->'chunks','[]'::jsonb)) as passages, "
            "parsed->'raw'->'asset_extraction' as extraction from document_parses "
            "order by parsed_at desc, id desc"
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "system": r["system"],
                "filename": r["filename"],
                "doc_type": r["doc_type"],
                "parser": r["parser"],
                "page_count": r["page_count"],
                "title": r["title"],
                "status": r["status"],
                "passages": r["passages"],
                "extraction": json.loads(r["extraction"]) if isinstance(r["extraction"], str) else r["extraction"],
                "unreadable_pages": json.loads(r["unreadable_pages"]) if isinstance(r["unreadable_pages"], str) else r["unreadable_pages"] or [],
                "error": r["error"],
                "parsed_at": r["parsed_at"].isoformat() if r["parsed_at"] else None,
                "processed_at": r["processed_at"].isoformat() if r["processed_at"] else None,
            }
        )
    return out


@router.get("/documents/{document_id}/file")
async def document_file(document_id: int) -> Response:
    import base64
    pool = get_pool()
    async with pool.acquire() as conn:
        raw = await conn.fetchval("select parsed->'raw'->>'original_pdf_b64' from document_parses where id=$1", document_id)
    if not raw:
        raise HTTPException(404, "Original PDF unavailable; re-upload the document")
    return Response(base64.b64decode(raw, validate=True), media_type="application/pdf", headers={"Content-Disposition": "inline"})


class ReprocessRequest(BaseModel):
    extract_assets: bool = False


@router.post("/documents/{document_id}/reprocess")
async def reprocess_document(document_id: int, req: ReprocessRequest) -> dict[str, Any]:
    import base64
    if job_registry.running() is not None:
        raise HTTPException(409, "An ingestion job is already running")
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("select system,filename,parsed->'raw'->>'original_pdf_b64' as original from document_parses where id=$1", document_id)
    if row is None or not row["original"]:
        raise HTTPException(404, "Original PDF unavailable; re-upload to reprocess")
    items = [(row["system"], row["filename"], base64.b64decode(row["original"], validate=True))]
    job = job_registry.start("reprocess", row["filename"], lambda: ingest_documents_events(items, extract_assets=req.extract_assets, force=True))
    return {"job_id": job.id, "status": job.status, "label": job.label}


@router.get("/assets")
async def list_assets() -> list[dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        assets = await conn.fetch(
            "select unified_id, asset_name, needs_review, review_reason "
            "from unified_assets order by unified_id"
        )
        idents = await conn.fetch(
            "select unified_id, concept, value, is_primary from asset_identifiers"
        )
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for r in idents:
        by_asset.setdefault(r["unified_id"], []).append(
            {"concept": r["concept"], "value": r["value"], "is_primary": r["is_primary"]}
        )
    return [
        {
            "unified_id": a["unified_id"],
            "asset_name": a["asset_name"],
            "needs_review": a["needs_review"],
            "review_reason": a["review_reason"],
            "identifiers": by_asset.get(a["unified_id"], []),
        }
        for a in assets
    ]


@router.get("/review")
async def review_queue() -> list[dict[str, Any]]:
    """Open review items, each enriched with the CANDIDATE records under review
    (system, text, identifiers, and which asset they resolved to) so a human can
    see exactly what is being compared — not just a bare confidence number."""
    pool = get_pool()
    out: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "select id, kind, payload, reason, status from review_queue "
            "where status = 'open' order by id"
        )
        for r in rows:
            payload = r["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            records = payload.get("records", []) if isinstance(payload, dict) else []
            candidates: list[dict[str, Any]] = []
            if records:
                crows = await conn.fetch(
                    """
                    select sr.system, sr.record_id, sr.semantic_text, sr.raw_json,
                           asr.unified_id, ua.asset_name
                    from source_records sr
                    left join asset_source_records asr on asr.source_record_id = sr.id
                    left join unified_assets ua on ua.unified_id = asr.unified_id
                    where sr.record_id = any($1::text[])
                    """,
                    records,
                )
                for c in crows:
                    raw = c["raw_json"]
                    if isinstance(raw, str):
                        try:
                            raw = json.loads(raw)
                        except json.JSONDecodeError:
                            raw = {}
                    # surface identifier-ish fields (skip meta + free text)
                    idents = {
                        k: v
                        for k, v in (raw or {}).items()
                        if k not in ("record_id", "description", "asset_name")
                        and not k.startswith("_")
                        and v not in (None, "", [])
                    }
                    candidates.append(
                        {
                            "record_id": c["record_id"],
                            "system": c["system"],
                            "text": (c["semantic_text"] or "")[:240],
                            "unified_id": c["unified_id"],
                            "asset_name": c["asset_name"],
                            "fields": idents,
                        }
                    )
            out.append(
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "payload": payload,
                    "reason": r["reason"],
                    "status": r["status"],
                    "candidates": candidates,
                }
            )
    return out


@router.post("/review/{item_id}/resolve")
async def resolve_review(item_id: int, req: ReviewAction) -> dict[str, Any]:
    """Act on a review item.

    dismiss  -> just close it.
    merge    -> record a durable 'merge' decision for its records and re-resolve.
    separate -> record a durable 'separate' decision and re-resolve.
    Decisions live in manual_decisions and are re-applied on every sync.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "select payload from review_queue where id=$1 and status='open'", item_id
        )
        if row is None:
            return {"ok": False, "error": "not found or already resolved"}

        if req.action == "dismiss":
            await conn.execute(
                "update review_queue set status='dismissed' where id=$1", item_id
            )
            return {"ok": True, "status": "dismissed"}

        if req.action not in ("merge", "separate"):
            return {"ok": False, "error": "invalid action"}

        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        records = payload.get("records", []) if isinstance(payload, dict) else []
        for i in range(len(records)):
            for j in range(i + 1, len(records)):
                a, b = sorted((records[i], records[j]))
                await conn.execute("delete from manual_decisions where record_a=$1 and record_b=$2 and kind<>$3", a, b, req.action)
                await conn.execute(
                    "insert into manual_decisions (kind, record_a, record_b) "
                    "values ($1,$2,$3) on conflict do nothing",
                    req.action, a, b,
                )
        await conn.execute(
            "update review_queue set status='resolved' where id=$1", item_id
        )

    # Re-resolve the corpus so the decision takes effect immediately.
    async for _ in pipeline.ingest_records({}):
        pass
    return {"ok": True, "status": "resolved", "action": req.action}


@router.get("/graph/rca")
async def rca(asset: str = Query(..., description="unified asset id, e.g. ua-0001")) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await _graph.rca(conn, asset_node(asset))
    return {"asset": asset, "chain": rows}


@router.get("/graph/compliance")
async def compliance(rule: str = Query(..., description="rule id, e.g. RULE-LUBE-001")) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        at_risk = await _graph.compliance_at_risk(conn, rule_node(rule))
    return {"rule": rule, "at_risk_assets": at_risk}


@router.get("/overview")
async def overview() -> dict[str, Any]:
    """Dashboard counts + recent evidence for the Home screen."""
    pool = get_pool()
    async with pool.acquire() as conn:
        review_open = await conn.fetchval(
            "select count(*) from review_queue where status = 'open'"
        )
        assets = await conn.fetchval("select count(*) from unified_assets")
        needs_review = await conn.fetchval(
            "select count(*) from unified_assets where needs_review"
        )
        records = await conn.fetchval("select count(*) from source_records")
        edges_total = await conn.fetchval("select count(*) from edges")
        edges_op = await conn.fetchval(
            "select count(*) from edges where layer = 'operational'"
        )
        recent = await conn.fetch(
            """
            select sr.system, sr.record_id, sr.semantic_text, sr.ingest_ts,
                   asr.unified_id
            from source_records sr
            left join asset_source_records asr on asr.source_record_id = sr.id
            order by sr.ingest_ts desc, sr.id desc
            limit 6
            """
        )
    return {
        "review_open": review_open or 0,
        "unified_assets": assets or 0,
        "assets_needing_review": needs_review or 0,
        "source_records": records or 0,
        "edges_total": edges_total or 0,
        "edges_operational": edges_op or 0,
        "recent_evidence": [
            {
                "citation": r["record_id"],
                "system": r["system"],
                "text": r["semantic_text"] or "",
                "unified_id": r["unified_id"],
                "ingested_at": r["ingest_ts"].isoformat() if r["ingest_ts"] else None,
            }
            for r in recent
        ],
    }


def _node_type(node_id: str) -> tuple[str, str]:
    """Map a prefixed node id to (type, display label)."""
    prefix, _, rest = node_id.partition(":")
    mapping = {
        "asset": "asset",
        "sr": "record",
        "fm": "failure",
        "cf": "cause",
        "rule": "rule",
        "wo": "work_order",
        "sensor": "signal",
    }
    if prefix in mapping:
        return mapping[prefix], rest or node_id
    # identifier concepts (serial_number:..., mac_address:..., uuid:...)
    if prefix:
        return "identifier", rest or node_id
    return "concept", node_id


@router.get("/graph")
async def graph(
    asset: str | None = Query(default=None, description="scope to an asset's neighborhood"),
    limit: int = Query(default=500, le=2000),
) -> dict[str, Any]:
    """Return nodes + links for the knowledge-graph visualization."""
    pool = get_pool()
    async with pool.acquire() as conn:
        if asset:
            rows = await _graph.traverse(conn, asset_node(asset), None, max_depth=4)
            links_raw = [
                (r["source_id"], r["relation_type"], r["target_id"], r.get("layer", "physical"))
                for r in rows
            ]
        else:
            edge_rows = await conn.fetch(
                "select source_id, relation_type, target_id, layer from edges limit $1",
                limit,
            )
            links_raw = [
                (r["source_id"], r["relation_type"], r["target_id"], r["layer"])
                for r in edge_rows
            ]
        # asset display names
        names = {
            r["unified_id"]: r["asset_name"]
            for r in await conn.fetch("select unified_id, asset_name from unified_assets")
        }

    node_ids: set[str] = set()
    links: list[dict[str, Any]] = []
    for src, rel, tgt, layer in links_raw:
        node_ids.add(src)
        node_ids.add(tgt)
        links.append({"source": src, "target": tgt, "relation": rel, "layer": layer})

    nodes: list[dict[str, Any]] = []
    for nid in sorted(node_ids):
        ntype, label = _node_type(nid)
        if ntype == "asset":
            label = names.get(label, label)
        nodes.append({"id": nid, "type": ntype, "label": label})

    # Derived asset-to-asset relations so the graph shows how the canonical
    # assets we resolved relate to EACH OTHER, not just asset->record spokes:
    #   RELATED_TO  — grounded, exact: the two assets share an identifier value.
    #   SIMILAR_TO  — grounded in descriptive text (labeled, not a hard link).
    if not asset:
        pair_seen: set[frozenset[str]] = set()
        async with pool.acquire() as conn:
            shared = await conn.fetch(
                """
                select distinct a.unified_id as a_id, b.unified_id as b_id,
                       a.concept, a.value
                from asset_identifiers a
                join asset_identifiers b
                  on a.concept = b.concept
                 and lower(a.value) = lower(b.value)
                 and a.unified_id < b.unified_id
                """
            )
            try:
                similar = await conn.fetch(
                    """
                    select a_id, b_id, sim from (
                      select asr1.unified_id as a_id, asr2.unified_id as b_id,
                             max(1 - (sr1.embedding <=> sr2.embedding)) as sim
                      from asset_source_records asr1
                      join source_records sr1
                        on sr1.id = asr1.source_record_id and sr1.embedding is not null
                      join asset_source_records asr2 on asr2.unified_id > asr1.unified_id
                      join source_records sr2
                        on sr2.id = asr2.source_record_id and sr2.embedding is not null
                      group by asr1.unified_id, asr2.unified_id
                    ) p
                    where sim >= 0.72
                    order by sim desc
                    limit 60
                    """
                )
            except Exception:
                similar = []
        for r in shared:
            an, bn = asset_node(r["a_id"]), asset_node(r["b_id"])
            if an in node_ids and bn in node_ids:
                pair_seen.add(frozenset((an, bn)))
                links.append(
                    {
                        "source": an,
                        "target": bn,
                        "relation": "RELATED_TO",
                        "layer": "physical",
                        "metadata": {"via": f"{r['concept']} = {r['value']}"},
                    }
                )
        for r in similar:
            an, bn = asset_node(r["a_id"]), asset_node(r["b_id"])
            if an in node_ids and bn in node_ids and frozenset((an, bn)) not in pair_seen:
                pair_seen.add(frozenset((an, bn)))
                links.append(
                    {
                        "source": an,
                        "target": bn,
                        "relation": "SIMILAR_TO",
                        "layer": "physical",
                        "metadata": {"similarity": round(float(r["sim"]), 2)},
                    }
                )

    return {
        "nodes": nodes,
        "links": links,
        "stats": {"records": len(nodes), "relationships": len(links)},
    }


@router.get("/asset/{unified_id}")
async def asset_detail(unified_id: str) -> dict[str, Any]:
    """OKF-shaped concept document for one canonical asset (frontmatter + body).

    Front matter = canonical name/type + aliases with their source provenance;
    body = consolidated per-source knowledge; plus asset-to-asset relations and
    citations. Powers the node inspector. 404 if the asset is unknown.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        doc = await asset_okf(conn, unified_id)
    if doc is None:
        return {"error": "not_found", "unified_id": unified_id}
    return doc


# --- workflow run history --------------------------------------------------
@router.get("/history")
async def history(
    kind: str | None = Query(default=None, description="rca | compliance"),
    limit: int = Query(default=50, le=200),
) -> list[dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        return await list_workflow_runs(conn, kind, limit)


@router.get("/history/{run_id}")
async def history_run(run_id: int) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        run = await get_workflow_run(conn, run_id)
    return run or {"error": "not_found", "id": run_id}


# --- AI workflows (streamed) ----------------------------------------------
@router.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    """Chatbot: routes to ask/rca/compliance/lookup, streaming status + tokens."""
    return StreamingResponse(
        run_chat_events(req.message, req.intent, req.document_id, [t.model_dump() for t in req.history]),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/rca")
async def rca_workflow(req: RCARequest) -> StreamingResponse:
    """Root-cause analysis, streaming per-stage status (SSE)."""
    return StreamingResponse(
        run_rca_events(req.question, req.asset),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/compliance")
async def compliance_workflow(req: ComplianceRequest) -> StreamingResponse:
    """Compliance posture + at-risk propagation, streaming per-stage status (SSE)."""
    return StreamingResponse(
        run_compliance_events(req.question, req.rule, req.asset),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# --- connectors (named data sources, incremental + accumulative sync) -------
@router.get("/connectors")
async def get_connectors() -> list[dict[str, Any]]:
    return await connectors.list_connectors()


@router.post("/connectors")
async def add_connector(req: ConnectorCreate) -> dict[str, Any]:
    return await connectors.create_connector(
        req.name, req.description, req.kind, req.endpoint, req.payload
    )


@router.post("/connectors/{connector_id}/sync")
async def sync_connector(connector_id: int, req: SyncRequest) -> StreamingResponse:
    async with get_pool().acquire() as conn:
        kind = await conn.fetchval("select kind from connectors where id=$1", connector_id)
    if kind is None:
        raise HTTPException(404, "Connector not found")
    if kind == "document":
        raise HTTPException(409, "PDF sources are reprocessed from Documents, not synced as JSON")
    """Fetch the source's new records and re-resolve the corpus (streamed SSE)."""
    return StreamingResponse(
        _sse(connectors.sync_connector(connector_id, req.payload)),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.delete("/connectors/{connector_id}")
async def remove_connector(connector_id: int) -> dict[str, Any]:
    """Delete a connector, drop its records, and re-resolve what remains."""
    try:
        async for _ in connectors.delete_connector(connector_id):
            pass
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"ok": True}
