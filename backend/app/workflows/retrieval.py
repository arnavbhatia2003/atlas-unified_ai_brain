"""Grounded retrieval over the canonical model + knowledge graph.

Everything here returns source-tagged evidence so answers can cite where each
fact came from (grounding is non-negotiable). Three retrieval modes:

  - semantic_search: pgvector top-k over source_records (fuzzy questions)
  - asset_facts:     structured facts for one resolved asset (identifiers + records)
  - graph_neighborhood: edges around an asset (physical + operational context)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import asyncpg
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services.embeddings import get_embedding_client
from app.services.persistence import _vector_literal


class RetrievedRecord(BaseModel):
    """One source record retrieved as evidence, with its citation handle."""

    citation: str            # "SAP:a1" — the source-record handle to cite
    unified_id: str | None = None
    system: str
    text: str = ""
    similarity: float = 0.0  # 1.0 = identical direction (cosine)
    raw: dict[str, Any] = Field(default_factory=dict)


class AssetFacts(BaseModel):
    unified_id: str
    asset_name: str | None = None
    needs_review: bool = False
    review_reason: str = ""
    identifiers: dict[str, list[str]] = Field(default_factory=dict)
    source_records: list[str] = Field(default_factory=list)  # citations


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value or {}


async def semantic_search(
    conn: asyncpg.Connection, query: str, k: int = 5
) -> list[RetrievedRecord]:
    """Embed `query` and return the k nearest source records by cosine distance.

    Returns [] if the embedding service is unavailable — callers then fall back
    to structured/graph retrieval rather than guessing. Costs one embedding call
    against the shared NVIDIA budget.
    """
    query = (query or "").strip()
    if not query:
        return []
    client = get_embedding_client()
    vector = await asyncio.to_thread(client.embed_query, query)
    literal = _vector_literal(vector)
    if literal is None:
        return []

    rows = await conn.fetch(
        """
        select sr.system, sr.record_id, sr.semantic_text, sr.raw_json,
               asr.unified_id,
               1 - (sr.embedding <=> $1::vector) as similarity
        from source_records sr
        left join asset_source_records asr on asr.source_record_id = sr.id
        where sr.embedding is not null
        order by sr.embedding <=> $1::vector
        limit $2
        """,
        literal, k,
    )
    return [
        RetrievedRecord(
            citation=r["record_id"],
            unified_id=r["unified_id"],
            system=r["system"],
            text=r["semantic_text"] or "",
            similarity=round(float(r["similarity"]), 4),
            raw=_loads(r["raw_json"]),
        )
        for r in rows
    ]


async def asset_facts(conn: asyncpg.Connection, unified_id: str) -> AssetFacts | None:
    """Structured, grounded facts for one canonical asset."""
    asset = await conn.fetchrow(
        "select unified_id, asset_name, needs_review, review_reason "
        "from unified_assets where unified_id = $1",
        unified_id,
    )
    if asset is None:
        return None
    ident_rows = await conn.fetch(
        "select concept, value from asset_identifiers where unified_id = $1 "
        "order by concept, value",
        unified_id,
    )
    identifiers: dict[str, list[str]] = {}
    for r in ident_rows:
        identifiers.setdefault(r["concept"], []).append(r["value"])
    rec_rows = await conn.fetch(
        """
        select sr.record_id
        from asset_source_records asr
        join source_records sr on sr.id = asr.source_record_id
        where asr.unified_id = $1
        order by sr.record_id
        """,
        unified_id,
    )
    return AssetFacts(
        unified_id=asset["unified_id"],
        asset_name=asset["asset_name"],
        needs_review=asset["needs_review"],
        review_reason=asset["review_reason"],
        identifiers=identifiers,
        source_records=[r["record_id"] for r in rec_rows],
    )


async def graph_neighborhood(
    conn: asyncpg.Connection, node_id: str
) -> list[dict[str, Any]]:
    """Immediate edges touching a node (both directions), physical + operational."""
    rows = await conn.fetch(
        """
        select source_id, relation_type, target_id, layer, metadata
        from edges
        where source_id = $1 or target_id = $1
        order by layer, relation_type
        """,
        node_id,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["metadata"] = _loads(d.get("metadata"))
        out.append(d)
    return out


# --- OKF projection --------------------------------------------------------
# We present each canonical asset as an Open Knowledge Format (OKF) concept
# document: YAML-style frontmatter (title/type/aliases) + a human-readable body
# grouped by source, + asset-to-asset cross-links + citations. Postgres stays the
# source of truth; this is a read-time serializer, never a second datastore.

_RELATION_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("work order", "workorder", "cmms", "maximo"), "Work order"),
    (("maintenance", "service history", "repair"), "Maintenance record"),
    (("inspection", "checklist", "condition"), "Inspection log"),
    (("drawing", "p&id", "schematic", "diagram", "engineering register"), "Engineering drawing"),
    (("incident", "failure", "breakdown", "outage"), "Incident report"),
    (("sop", "procedure", "standard operating", "manual"), "SOP / procedure"),
    (("sensor", "signal", "telemetry", "reading", "measurement"), "Sensor reading"),
    (("permit", "compliance", "regulation", "rule", "audit"), "Compliance / permit"),
    (("registry", "register", "catalog", "master", "crosswalk", "asset list"), "Asset register"),
    (("sharepoint", "document", "file", "doc"), "Document"),
]


def relation_from_description(description: str, system: str = "") -> str:
    """Derive a human relation label for a source from its connector description.

    Deterministic keyword match (no LLM). Falls back to a generic label so a
    source without a descriptive connector still reads sensibly.
    """
    blob = f"{description} {system}".lower()
    for keywords, label in _RELATION_KEYWORDS:
        if any(k in blob for k in keywords):
            return label
    return "Source record"


def _clean_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop meta keys; keep human-meaningful source fields for display."""
    return {
        k: v
        for k, v in (raw or {}).items()
        if k != "record_id" and v not in (None, "", [])
    }


async def asset_okf(conn: asyncpg.Connection, unified_id: str) -> dict[str, Any] | None:
    """Project one canonical asset into an OKF-shaped concept document.

    Returns frontmatter (title/type/aliases-with-provenance), a body of
    consolidated per-source knowledge, asset-to-asset relations (grounded shared
    identifiers + clearly-labeled semantic neighbors), citations, and a rendered
    OKF markdown document. All grounded — every fact traces to a source record.
    """
    asset = await conn.fetchrow(
        "select unified_id, asset_name, entity_type, needs_review, review_reason "
        "from unified_assets where unified_id = $1",
        unified_id,
    )
    if asset is None:
        return None

    # Aliases (identifiers) + which source systems asserted each value.
    ident_rows = await conn.fetch(
        """
        select ai.concept, ai.value, coalesce(ai.source_system, '') as source_system
        from asset_identifiers ai
        where ai.unified_id = $1
        order by ai.concept, ai.value
        """,
        unified_id,
    )

    # Source records contributing knowledge, joined to their connector metadata.
    src_rows = await conn.fetch(
        """
        select sr.record_id, sr.system, sr.raw_json, sr.semantic_text, sr.ingest_ts,
               coalesce(c.description, '') as connector_description
        from asset_source_records asr
        join source_records sr on sr.id = asr.source_record_id
        left join connectors c on c.name = sr.system
        where asr.unified_id = $1
        order by sr.system, sr.record_id
        """,
        unified_id,
    )

    # Which systems asserted each identifier value (for inline alias provenance).
    value_systems: dict[tuple[str, str], set[str]] = {}
    for s in src_rows:
        raw = _loads(s["raw_json"])
        for v in raw.values():
            if isinstance(v, str):
                key = v.strip().lower()
                for ir in ident_rows:
                    if ir["value"].strip().lower() == key:
                        value_systems.setdefault(
                            (ir["concept"], ir["value"]), set()
                        ).add(s["system"])

    aliases = [
        {
            "concept": r["concept"],
            "value": r["value"],
            "sources": sorted(value_systems.get((r["concept"], r["value"]), set()))
            or ([r["source_system"]] if r["source_system"] else []),
        }
        for r in ident_rows
    ]

    sources = []
    for s in src_rows:
        raw = _loads(s["raw_json"])
        sources.append(
            {
                "record_id": s["record_id"],
                "system": s["system"],
                "connector": s["system"],
                "connector_description": s["connector_description"],
                "relation": relation_from_description(
                    s["connector_description"], s["system"]
                ),
                "captured_at": s["ingest_ts"].isoformat() if s["ingest_ts"] else None,
                "fields": _clean_fields(raw),
                "text": s["semantic_text"] or "",
            }
        )

    related = await _related_assets(conn, unified_id)
    citations = [s["record_id"] for s in sources]

    payload = {
        "unified_id": asset["unified_id"],
        "title": asset["asset_name"] or asset["unified_id"],
        "type": asset["entity_type"] or "Asset",
        "needs_review": asset["needs_review"],
        "review_reason": asset["review_reason"],
        "aliases": aliases,
        "sources": sources,
        "related_assets": related,
        "citations": citations,
    }
    payload["markdown"] = _render_okf_markdown(payload)
    return payload


async def _related_assets(
    conn: asyncpg.Connection, unified_id: str
) -> list[dict[str, Any]]:
    """Asset-to-asset relations.

    1. GROUNDED hard links: another asset that shares an identifier value
       (same concept + value). This is exact evidence, not a guess.
    2. SIMILAR (clearly labeled, never asserted as a confirmed relationship):
       nearest assets by source-record embedding similarity, above a threshold.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    shared = await conn.fetch(
        """
        select distinct b.unified_id, ua.asset_name, a.concept, a.value
        from asset_identifiers a
        join asset_identifiers b
          on a.concept = b.concept
         and lower(a.value) = lower(b.value)
         and b.unified_id <> a.unified_id
        join unified_assets ua on ua.unified_id = b.unified_id
        where a.unified_id = $1
        order by a.concept, a.value
        """,
        unified_id,
    )
    for r in shared:
        out.append(
            {
                "unified_id": r["unified_id"],
                "asset_name": r["asset_name"],
                "kind": "shared_identifier",
                "via": f"{r['concept']} = {r['value']}",
                "confidence": 1.0,
            }
        )
        seen.add(r["unified_id"])

    try:
        neighbors = await conn.fetch(
            """
            select asr2.unified_id, ua.asset_name,
                   max(1 - (sr1.embedding <=> sr2.embedding)) as sim
            from asset_source_records asr1
            join source_records sr1
              on sr1.id = asr1.source_record_id and sr1.embedding is not null
            join asset_source_records asr2 on asr2.unified_id <> $1
            join source_records sr2
              on sr2.id = asr2.source_record_id and sr2.embedding is not null
            join unified_assets ua on ua.unified_id = asr2.unified_id
            where asr1.unified_id = $1
            group by asr2.unified_id, ua.asset_name
            having max(1 - (sr1.embedding <=> sr2.embedding)) >= 0.62
            order by sim desc
            limit 4
            """,
            unified_id,
        )
    except Exception:
        neighbors = []
    for r in neighbors:
        if r["unified_id"] in seen:
            continue
        out.append(
            {
                "unified_id": r["unified_id"],
                "asset_name": r["asset_name"],
                "kind": "similar",
                "via": f"description similarity {round(float(r['sim']), 2)}",
                "confidence": round(float(r["sim"]), 2),
            }
        )
        seen.add(r["unified_id"])

    return out


def _render_okf_markdown(payload: dict[str, Any]) -> str:
    """Render the asset as an OKF concept document (frontmatter + body + citations)."""
    fm: list[str] = ["---"]
    fm.append(f"type: {payload['type']}")
    fm.append(f"title: {payload['title']}")
    fm.append(f"unified_id: {payload['unified_id']}")
    if payload["aliases"]:
        fm.append("aliases:")
        for a in payload["aliases"]:
            src = ", ".join(a["sources"]) if a["sources"] else "unknown source"
            fm.append(f"  - {a['concept']}: {a['value']}  # from {src}")
    if payload["needs_review"]:
        fm.append("needs_review: true")
    fm.append("---")

    body: list[str] = ["", f"# {payload['title']}", ""]
    if payload["sources"]:
        # Group source knowledge by its relation label for readable sections.
        by_rel: dict[str, list[dict[str, Any]]] = {}
        for s in payload["sources"]:
            by_rel.setdefault(s["relation"], []).append(s)
        for relation, items in by_rel.items():
            body.append(f"## {relation}")
            for s in items:
                when = f" ({s['captured_at'][:10]})" if s.get("captured_at") else ""
                body.append(f"- **{s['connector']}**{when} — `{s['record_id']}`")
                if s["text"]:
                    body.append(f"  - {s['text']}")
                for k, v in s["fields"].items():
                    body.append(f"  - {k}: {v}")
            body.append("")

    if payload["related_assets"]:
        body.append("## Related assets")
        for r in payload["related_assets"]:
            tag = "linked" if r["kind"] == "shared_identifier" else "similar"
            body.append(
                f"- [{r['asset_name'] or r['unified_id']}]"
                f"(/assets/{r['unified_id']}) — {tag} ({r['via']})"
            )
        body.append("")

    if payload["citations"]:
        body.append("# Citations")
        for i, cid in enumerate(payload["citations"], 1):
            body.append(f"[{i}] {cid}")

    return "\n".join(fm + body)


# --- workflow run history --------------------------------------------------
async def save_workflow_run(
    conn: asyncpg.Connection,
    kind: str,
    question: str,
    result: dict[str, Any] | None,
    asset: str | None = None,
    rule: str | None = None,
) -> None:
    """Persist a completed RCA/Compliance run so users can reopen it later."""
    if not result:
        return
    if kind == "rca":
        report = result.get("report") or {}
        summary = report.get("summary", "") if isinstance(report, dict) else ""
        posture = None
        resolved = bool(result.get("resolved"))
    else:
        narrative = result.get("narrative") or {}
        summary = narrative.get("summary", "") if isinstance(narrative, dict) else ""
        posture = narrative.get("posture") if isinstance(narrative, dict) else None
        resolved = result.get("scope") not in (None, "none")
    try:
        await conn.execute(
            """
            insert into workflow_runs (kind, question, asset, rule, summary,
                posture, resolved, result)
            values ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
            """,
            kind, question or "", asset, rule, summary[:2000], posture,
            resolved, json.dumps(result, default=str),
        )
    except Exception:
        # History is best-effort; never break the workflow response over it.
        pass


async def list_workflow_runs(
    conn: asyncpg.Connection, kind: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    if kind:
        rows = await conn.fetch(
            "select id, kind, question, asset, rule, summary, posture, resolved, "
            "created_at from workflow_runs where kind = $1 "
            "order by created_at desc limit $2",
            kind, limit,
        )
    else:
        rows = await conn.fetch(
            "select id, kind, question, asset, rule, summary, posture, resolved, "
            "created_at from workflow_runs order by created_at desc limit $1",
            limit,
        )
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "question": r["question"],
            "asset": r["asset"],
            "rule": r["rule"],
            "summary": r["summary"],
            "posture": r["posture"],
            "resolved": r["resolved"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


async def get_workflow_run(
    conn: asyncpg.Connection, run_id: int
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "select id, kind, question, asset, rule, summary, posture, resolved, "
        "result, created_at from workflow_runs where id = $1",
        run_id,
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "kind": row["kind"],
        "question": row["question"],
        "asset": row["asset"],
        "rule": row["rule"],
        "summary": row["summary"],
        "posture": row["posture"],
        "resolved": row["resolved"],
        "result": _loads(row["result"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


# --- OKF bundle store ("second brain") + hybrid retrieval ------------------
async def materialize_okf_bundles(conn: asyncpg.Connection) -> int:
    """(Re)build the queryable OKF bundle for every canonical asset.

    Each asset's OKF concept document is stored as human-readable markdown AND
    made agent-queryable (embedding + full-text tsv). Re-embeds only when the
    body changed (body_hash); prunes bundles for assets that no longer exist.
    Returns the number of bundles embedded this run.
    """
    settings = get_settings()
    assets = await conn.fetch("select unified_id from unified_assets order by unified_id")
    current: list[str] = []
    embedded = 0
    for a in assets:
        uid = a["unified_id"]
        current.append(uid)
        doc = await asset_okf(conn, uid)
        if doc is None:
            continue
        body = doc["markdown"]
        summary = doc["title"]
        h = hashlib.sha1(body.encode("utf-8")).hexdigest()
        prev = await conn.fetchrow(
            "select body_hash, embedding is not null as has_embedding from okf_documents where unified_id = $1", uid
        )
        emb_literal = None
        if (prev is None or prev["body_hash"] != h or not prev["has_embedding"]) and settings.embeddings_enabled and body.strip():
            try:
                vectors = await asyncio.to_thread(get_embedding_client().embed, [body])
                vec = vectors[0] if vectors else None
                emb_literal = _vector_literal(vec)
                embedded += int(vec is not None)
            except Exception:
                emb_literal = None
        await conn.execute(
            """
            insert into okf_documents (unified_id, title, summary, body, markdown,
                body_hash, embedding, tsv, updated_at)
            values ($1,$2,$3,$4,$5,$6,$7::vector, to_tsvector('english', $4), now())
            on conflict (unified_id) do update set
                title = excluded.title, summary = excluded.summary,
                body = excluded.body, markdown = excluded.markdown,
                body_hash = excluded.body_hash,
                embedding = case when excluded.body_hash = okf_documents.body_hash then coalesce(excluded.embedding, okf_documents.embedding) else excluded.embedding end,
                tsv = excluded.tsv, updated_at = now()
            """,
            uid, doc["title"], summary, body, body, h, emb_literal,
        )
    if current:
        await conn.execute(
            "delete from okf_documents where unified_id <> all($1::text[])", current
        )
    else:
        await conn.execute("delete from okf_documents")
    return embedded


async def get_okf_bundle(conn: asyncpg.Connection, unified_id: str) -> dict[str, Any] | None:
    """Fetch a stored OKF bundle; fall back to building it on the fly."""
    row = await conn.fetchrow(
        "select unified_id, title, summary, body, markdown from okf_documents "
        "where unified_id = $1",
        unified_id,
    )
    if row is not None and row["markdown"]:
        return dict(row)
    doc = await asset_okf(conn, unified_id)
    if doc is None:
        return None
    return {
        "unified_id": unified_id,
        "title": doc["title"],
        "summary": doc["title"],
        "body": doc["markdown"],
        "markdown": doc["markdown"],
    }


def _rrf_fuse(*ranked_lists: list[str], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion: combine several ranked id-lists into one score map."""
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, key in enumerate(lst):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    return scores


async def hybrid_search(
    conn: asyncpg.Connection, query: str, k: int = 6
) -> list[RetrievedRecord]:
    """Hybrid retrieval + reranking (production-RAG pattern).

    Fuses three retrievers with Reciprocal Rank Fusion (deterministic, no extra
    model): dense vector over source records (semantic recall), full-text over
    source records (exact terms/codes embeddings miss), and dense vector over OKF
    bundles (the consolidated per-asset knowledge). Returns the top-k fused
    evidence, source records first, with any matched bundle appended as context.
    """
    query = (query or "").strip()
    if not query:
        return []
    settings = get_settings()

    literal = None
    if settings.embeddings_enabled:
        try:
            vec = await asyncio.to_thread(get_embedding_client().embed_query, query)
            literal = _vector_literal(vec)
        except Exception:
            literal = None

    # Retriever 1 — dense vector over source records.
    vec_rows: list[Any] = []
    if literal is not None:
        vec_rows = await conn.fetch(
            """
            select sr.record_id, sr.system, sr.semantic_text, sr.raw_json,
                   asr.unified_id, 1 - (sr.embedding <=> $1::vector) as sim
            from source_records sr
            left join asset_source_records asr on asr.source_record_id = sr.id
            where sr.embedding is not null
            order by sr.embedding <=> $1::vector limit 12
            """,
            literal,
        )
    # Retriever 2 — full-text over source records (keyword / exact-code recall).
    ft_rows = await conn.fetch(
        """
        select sr.record_id, sr.system, sr.semantic_text, sr.raw_json,
               asr.unified_id,
               ts_rank(to_tsvector('english', coalesce(sr.semantic_text,'')),
                       plainto_tsquery('english', $1)) as rank
        from source_records sr
        left join asset_source_records asr on asr.source_record_id = sr.id
        where to_tsvector('english', coalesce(sr.semantic_text,'')) @@ plainto_tsquery('english', $1)
        order by rank desc limit 12
        """,
        query,
    )
    # Retriever 3 — dense vector over OKF bundles (consolidated knowledge).
    bundle_rows: list[Any] = []
    if literal is not None:
        bundle_rows = await conn.fetch(
            """
            select unified_id, title, body, 1 - (embedding <=> $1::vector) as sim
            from okf_documents
            where embedding is not null
            order by embedding <=> $1::vector limit 4
            """,
            literal,
        )

    rec_by_id: dict[str, Any] = {}
    for r in list(vec_rows) + list(ft_rows):
        rec_by_id.setdefault(r["record_id"], r)
    sim_by_id = {r["record_id"]: float(r["sim"]) for r in vec_rows}

    fused = _rrf_fuse(
        [r["record_id"] for r in vec_rows],
        [r["record_id"] for r in ft_rows],
    )
    # Broaden to a candidate pool, then a cross-encoder reranker restores
    # precision (retrieve-broad / rerank-precise). Falls back to RRF order.
    pool = [rid for rid, _s in sorted(fused.items(), key=lambda kv: kv[1], reverse=True)][:15]
    final_ids = pool[:k]
    if settings.embeddings_enabled and len(pool) > 1:
        try:
            from langchain_core.documents import Document

            from app.services.llm import get_reranker

            docs = [
                Document(
                    page_content=(rec_by_id[rid]["semantic_text"] or rid),
                    metadata={"rid": rid},
                )
                for rid in pool
            ]
            from app.services.rate_limit import get_rate_limiter
            await asyncio.to_thread(get_rate_limiter().acquire, blocking=True)
            reranked = await asyncio.to_thread(
                get_reranker(k).compress_documents, documents=docs, query=query
            )
            rr_ids = [d.metadata["rid"] for d in reranked if d.metadata.get("rid")]
            if rr_ids:
                final_ids = rr_ids[:k]
        except Exception:
            pass  # reranker unavailable → keep the fused order

    out: list[RetrievedRecord] = []
    for rid in final_ids:
        r = rec_by_id[rid]
        out.append(
            RetrievedRecord(
                citation=r["record_id"], unified_id=r["unified_id"],
                system=r["system"], text=r["semantic_text"] or "",
                similarity=round(sim_by_id.get(rid, 0.0), 4),
                raw=_loads(r["raw_json"]),
            )
        )
    # Append the single most relevant OKF bundle as consolidated context.
    if bundle_rows:
        b = bundle_rows[0]
        out.append(
            RetrievedRecord(
                citation=f"okf:{b['unified_id']}", unified_id=b["unified_id"],
                system="OKF", text=(b["body"] or "")[:1500],
                similarity=round(float(b["sim"]), 4), raw={},
            )
        )
    return out


async def most_referenced_asset(conn: asyncpg.Connection) -> str | None:
    """The canonical asset with the most linked source records (the hub)."""
    row = await conn.fetchrow(
        "select unified_id, count(*) as c from asset_source_records "
        "group by unified_id order by c desc, unified_id limit 1"
    )
    return row["unified_id"] if row else None


async def document_search(conn: Any, query: str, document_id: int | None = None, k: int = 12) -> list[RetrievedRecord]:
    """Read a bounded whole manual, or search the GIN-indexed passage corpus."""
    rows = []
    if document_id:
        size = await conn.fetchval("select coalesce(sum(length(text)),0) from document_chunks where document_id=$1", document_id)
        # shortcut: whole-document context is bounded to 60k characters; long PDFs use lexical retrieval.
        if 0 < size <= 60000:
            rows = await conn.fetch("""
                select d.id,d.filename,d.status,d.parsed->'raw'->'unreadable_pages' as unreadable_pages,
                       c.chunk_index,c.page,c.text,c.section,0::real as rank
                from document_chunks c join document_parses d on d.id=c.document_id
                where c.document_id=$1 order by c.chunk_index
            """, document_id)
    method = "whole_document" if rows else "indexed_keyword"
    if not rows:
        import re
        stop = set("a an the is are was were do does did of to in for and or on with what which who how when where can could would should please tell me give about from this that it its have has had according document manual pdf section compare consistent".split())
        terms = [t for t in re.findall(r"[^\W_]+(?:[-_][^\W_]+)*", query, re.UNICODE)[:80] if t.casefold() not in stop]
        search = ' OR '.join(terms)
        if search:
            rows = await conn.fetch("""
                with terms as (
                    select distinct unnest(tsvector_to_array(to_tsvector('english',$1) || to_tsvector('simple',$1))) as term
                ), frequencies as (
                    select term, count(c.document_id)::real as frequency
                    from terms left join document_chunks c
                      on ($2::bigint is null or c.document_id=$2)
                     and c.tsv @@ plainto_tsquery('simple',term)
                    group by term
                )
                select d.id,d.filename,d.status,d.parsed->'raw'->'unreadable_pages' as unreadable_pages,
                    c.chunk_index,c.page,c.text,c.section,
                    (select coalesce(sum(1.0/(frequency+1)),0) from frequencies
                     where c.tsv @@ plainto_tsquery('simple',term)) as rank
                from document_chunks c join document_parses d on d.id=c.document_id
                where ($2::bigint is null or c.document_id=$2)
                  and c.tsv @@ (websearch_to_tsquery('english',$1) || websearch_to_tsquery('simple',$1))
                order by rank desc,
                    ts_rank_cd(c.tsv,websearch_to_tsquery('english',$1) || websearch_to_tsquery('simple',$1)) desc,
                    c.document_id,c.chunk_index limit $3
            """, search, document_id, k)
    return [RetrievedRecord(
        citation=f"PDF:{r['id']}:p{r['page']}:c{r['chunk_index']}", system=r['filename'],
        text=r['text'], similarity=float(r['rank']),
        raw={'document_id':r['id'],'page':r['page'],'filename':r['filename'],'retrieval_method':method,'document_status':r['status'],
             'unreadable_pages': json.loads(r['unreadable_pages']) if isinstance(r['unreadable_pages'],str) else r['unreadable_pages']},
    ) for r in rows]
