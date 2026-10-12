"""Accumulative, incremental ingestion pipeline.

Records arrive from connectors (named sources). Ingest is:

  * INCREMENTAL — only records whose (system, record_id) is not already stored
    are added; re-syncing the same source adds nothing.
  * ACCUMULATIVE — after storing the new delta, the WHOLE corpus of stored
    source_records is re-resolved and the derived projection rebuilt. Syncing one
    source therefore never wipes another; the knowledge graph grows.

Only NEW records are embedded (the NVIDIA rate budget is respected); stored
embeddings are reused. Raw records are saved durably BEFORE the projection is
rebuilt, so a processing failure never loses the source (chain-of-custody).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from app.config import get_settings
from app.db import get_pool
from app.engine.graph import project_physical_graph
from app.engine.graph.model import Edge
from app.engine.resolution import ResolutionResult, SourceRecord, resolve
from app.engine.resolution.extractors import SimilarityFn
from app.engine.schema_discovery import (
    FieldRole,
    SchemaDiscoveryResult,
    discover_schema,
)
from app.knowledge.identity import weight_of

from .embeddings import build_similarity, get_embedding_client
from .persistence import _vector_literal, rebuild_projection

_IDENTIFIER_ROLES = {FieldRole.ANCHOR}
_DESCRIPTIVE_ROLES = {FieldRole.DESCRIPTIVE}
_UNTRUSTED_CONCEPTS = {"alphanumeric_id", "numeric_id", "system_internal_id", "unknown"}


def normalize_records(
    system: str, raw_records: list[dict[str, Any]], discovery: SchemaDiscoveryResult
) -> list[SourceRecord]:
    """Turn raw records into typed SourceRecords using the discovered schema."""
    id_map = {
        f.field_name: f.detected_concept.value
        for f in discovery.fields
        if f.role in _IDENTIFIER_ROLES
        and f.field_name != "record_id"
        and weight_of(f.detected_concept) > 0
        and f.detected_concept.value not in _UNTRUSTED_CONCEPTS
    }
    # `record_id` is our meta key for citation, not content — keep it out of
    # the semantic text used for embedding/naming.
    desc_fields = [
        f.field_name
        for f in discovery.fields
        if f.role in _DESCRIPTIVE_ROLES and f.field_name != "record_id"
    ]

    out: list[SourceRecord] = []
    for i, rec in enumerate(raw_records):
        identifiers: dict[str, list[str]] = {}
        for field, concept in id_map.items():
            val = rec.get(field)
            if val is not None and str(val).strip():
                identifiers.setdefault(concept, []).append(str(val).strip())
        if rec.get("_source") == "document":
            from .doc_extract import _ident_concept
            for concept in ("serial_number", "vin", "mac_address", "imei", "udi", "uuid"):
                value = str(rec.get(concept) or "").strip()
                if value and _ident_concept(value) == concept and value not in identifiers.get(concept, []):
                    identifiers.setdefault(concept, []).append(value)
        semantic = " ".join(str(rec[f]) for f in desc_fields if rec.get(f) is not None)
        rid = str(rec.get("record_id", i))
        out.append(
            SourceRecord(
                record_id=f"{system}:{rid}",
                system=system,
                identifiers=identifiers,
                semantic_text=semantic,
                entity_kind=str(rec.get("_entity_kind") or "unspecified"),
                identity_namespace=str(rec.get("_identity_namespace") or ""),
            )
        )
    return out


def _parse_vector(text: str) -> list[float]:
    body = text.strip().lstrip("[").rstrip("]")
    if not body:
        return []
    try:
        return [float(x) for x in body.split(",")]
    except ValueError:
        return []


def _derive_names(
    all_records: list[SourceRecord],
    result: ResolutionResult,
    prefer_systems: set[str] | None = None,
    clean_name_by_id: dict[str, str] | None = None,
) -> dict[str, str]:
    """Human display name per cluster: the richest descriptive text of its members.

    Identity/registry sources (``prefer_systems``) are preferred over operational
    text (incidents/work orders), so an asset is named after what it IS, not after
    the longest failure description linked to it. When a member carries an explicit
    clean asset name (``clean_name_by_id`` — e.g. a document-extracted mention's
    `name`), that is preferred over the longest free-text, so an asset isn't named
    after an incident sentence.
    """
    prefer_systems = prefer_systems or set()
    clean_name_by_id = clean_name_by_id or {}
    text_by_id = {r.record_id: r.semantic_text for r in all_records}
    system_by_id = {r.record_id: r.system for r in all_records}
    names: dict[str, str] = {}
    for c in result.clusters:
        # 1. Prefer an explicit clean asset name if any member has one (shortest,
        #    to avoid a run-on). This fixes document-derived name pollution.
        clean = [
            clean_name_by_id[m].strip()
            for m in c.members
            if clean_name_by_id.get(m, "").strip()
        ]
        if clean:
            best = min(clean, key=len)
            names[c.cluster_id] = best[:60] + "…" if len(best) > 60 else best
            continue
        preferred = [
            text_by_id.get(m, "").strip()
            for m in c.members
            if system_by_id.get(m) in prefer_systems and text_by_id.get(m, "").strip()
        ]
        pool = preferred or [
            text_by_id.get(m, "").strip()
            for m in c.members
            if text_by_id.get(m, "").strip()
        ]
        if pool:
            name = max(pool, key=len)
            names[c.cluster_id] = name[:60] + "…" if len(name) > 60 else name
    return names


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


_pipeline_lock = asyncio.Lock()


async def initialize_source_revisions(pool):
    async with pool.acquire() as conn:
        await conn.execute("""create table if not exists source_record_revisions (
            id bigserial primary key,
            source_record_id bigint not null references source_records(id) on delete cascade,
            raw_json jsonb not null,
            replaced_at timestamptz not null default now()
        )""")


async def store_source_delta(conn, systems):
    """Atomically keep current payloads and archive replaced versions."""
    counts = {"new": 0, "updated": 0, "skipped": 0}
    async with conn.transaction():
        for system, recs in systems.items():
            for i, rec in enumerate(recs or []):
                rid = str(rec.get("record_id") or f"r{i}")
                full = f"{system}:{rid}"
                raw = json.dumps({**rec, "record_id": rid})
                old = await conn.fetchrow("select id, raw_json <> $3::jsonb as changed from source_records where system=$1 and record_id=$2 for update", system, full, raw)
                if old is None:
                    await conn.execute("insert into source_records(system,record_id,raw_json) values($1,$2,$3::jsonb)", system, full, raw)
                    counts["new"] += 1
                elif old["changed"]:
                    await conn.execute("insert into source_record_revisions(source_record_id,raw_json) select id,raw_json from source_records where id=$1", old["id"])
                    await conn.execute("update source_records set raw_json=$2::jsonb,semantic_text=null,embedding=null,ingest_ts=now() where id=$1", old["id"], raw)
                    await conn.execute("delete from operational_extractions where record_id=$1", full)
                    counts["updated"] += 1
                else:
                    counts["skipped"] += 1
    return counts


async def ingest_records(systems: dict[str, list[dict[str, Any]]]) -> AsyncIterator[dict[str, Any]]:
    async with _pipeline_lock:
        async for event in _ingest_records(systems):
            yield event


async def _ingest_records(
    systems: dict[str, list[dict[str, Any]]],
) -> AsyncIterator[dict[str, Any]]:
    """Store the new delta, re-resolve the full corpus, rebuild the projection."""
    yield {"step": "start", "message": f"Ingesting into {len(systems)} source(s)"}
    pool = get_pool()
    settings = get_settings()
    warnings: list[str] = []

    async with pool.acquire() as conn:
        counts = await store_source_delta(conn, systems)
        new_count, skip_count = counts["new"], counts["skipped"]
        yield {
            "step": "delta",
            "message": f"{new_count} new; {counts['updated']} updated; {skip_count} unchanged record(s)",
            "new": new_count,
            "skipped": skip_count,
            "updated": counts["updated"],
        }

        # 2. Load the FULL accumulated corpus.
        rows = await conn.fetch(
            "select id, system, record_id, raw_json, embedding::text as emb "
            "from source_records order by system, id"
        )
        if not rows:
            # No records remain (e.g. every connector was removed / data cleared)
            # — wipe ALL derived state so nothing stale lingers. Previously only
            # physical edges + assets were cleared, leaving operational edges, OKF
            # bundles, extraction caches, source_systems and discovered_fields
            # orphaned (the graph then still showed ghost nodes).
            async with conn.transaction():
                await conn.execute("delete from unified_assets")  # cascades identifiers + links
                await conn.execute("delete from edges")           # physical AND operational
                await conn.execute("delete from okf_documents")
                await conn.execute("delete from operational_extractions")
                await conn.execute("delete from source_systems")
                await conn.execute("delete from discovered_fields")
                await conn.execute("delete from review_queue where kind <> 'manual'")
            yield {"step": "complete", "message": "Nothing to resolve", "summary": _empty_summary()}
            return

        raw_by_system: dict[str, list[dict[str, Any]]] = {}
        raw_by_record: dict[str, dict[str, Any]] = {}
        db_id: dict[str, int] = {}
        emb_by_id: dict[str, list[float]] = {}
        for r in rows:
            raw = r["raw_json"]
            if isinstance(raw, str):
                raw = json.loads(raw)
            raw_by_system.setdefault(r["system"], []).append(raw)
            raw_by_record[r["record_id"]] = raw
            db_id[r["record_id"]] = r["id"]
            if r["emb"]:
                emb_by_id[r["record_id"]] = _parse_vector(r["emb"])

        # 3. Discover + normalize per system over the full corpus.
        discoveries: dict[str, SchemaDiscoveryResult] = {}
        all_records: list[SourceRecord] = []
        for system, raws in raw_by_system.items():
            disc = discover_schema(raws)
            discoveries[system] = disc
            all_records.extend(normalize_records(system, raws, disc))
        yield {
            "step": "normalize",
            "message": f"Normalized {len(all_records)} record(s) across "
            f"{len(discoveries)} source(s)",
        }

        # Keep stored semantic_text in sync with the current normalization.
        await conn.executemany(
            "update source_records set semantic_text=$3 where system=$1 and record_id=$2",
            [(r.system, r.record_id, r.semantic_text) for r in all_records],
        )

        # 4. Embed only records lacking a stored embedding; reuse the rest.
        to_embed = [
            r for r in all_records
            if r.record_id not in emb_by_id and r.semantic_text.strip()
        ]
        similarity: SimilarityFn | None = None
        if settings.embeddings_enabled and to_embed:
            client = get_embedding_client()
            vectors = await asyncio.to_thread(
                client.embed, [r.semantic_text for r in to_embed]
            )
            if vectors:
                await conn.executemany(
                    "update source_records set embedding=$3::vector "
                    "where system=$1 and record_id=$2",
                    [
                        (r.system, r.record_id, _vector_literal(v))
                        for r, v in zip(to_embed, vectors)
                    ],
                )
                for r, v in zip(to_embed, vectors):
                    emb_by_id[r.record_id] = v
                yield {
                    "step": "embedding",
                    "message": f"Embedded {len(vectors)} new record(s); "
                    f"reused {len(emb_by_id) - len(vectors)}",
                }
            else:
                warnings.append("Source embeddings unavailable; exact-identifier matching only")
                yield {
                    "step": "embedding",
                    "message": "Embedding service unavailable — exact-identifier matching only",
                }
        elif emb_by_id:
            yield {
                "step": "embedding",
                "message": f"Reused {len(emb_by_id)} stored embedding(s)",
            }

        if settings.embeddings_enabled and emb_by_id:
            similarity = build_similarity(emb_by_id)

        # 5. Resolve the full corpus, honoring any human review decisions.
        dec_rows = await conn.fetch(
            "select kind, record_a, record_b from manual_decisions"
        )
        forced_merge = {
            frozenset((r["record_a"], r["record_b"]))
            for r in dec_rows if r["kind"] == "merge"
        }
        forced_separate = {
            frozenset((r["record_a"], r["record_b"]))
            for r in dec_rows if r["kind"] == "separate"
        }
        result: ResolutionResult = resolve(
            all_records,
            similarity=similarity,
            forced_merge=forced_merge,
            forced_separate=forced_separate,
        )
        previous = await conn.fetch("select a.unified_id,array_agg(s.record_id order by s.record_id) as members from asset_source_records a join source_records s on s.id=a.source_record_id group by a.unified_id")
        previous_ids = {tuple(r["members"]): r["unified_id"] for r in previous}
        for cluster in result.clusters:
            cluster.cluster_id = previous_ids.get(tuple(sorted(cluster.members)), cluster.cluster_id)
        yield {
            "step": "resolve",
            "message": f"Resolved into {len(result.clusters)} unified asset(s); "
            f"{len(result.review_items)} item(s) need review",
            "clusters": len(result.clusters),
            "review": len(result.review_items),
        }

        # Connector relation per system — drives both naming preference and which
        # records get operational extraction.
        from app.workflows.extract_ops import (
            OPERATIONAL_RELATIONS,
            rebuild_operational_edges,
        )
        from app.workflows.retrieval import relation_from_description

        conn_rows = await conn.fetch("select name, description from connectors")
        relation_by_system = {
            r["name"]: relation_from_description(r["description"], r["name"])
            for r in conn_rows
        }
        identity_systems = {
            s for s, rel in relation_by_system.items() if rel not in OPERATIONAL_RELATIONS
        }

        # 6. Project + rebuild the derived graph (source_records preserved).
        edges: list[Edge] = project_physical_graph(result)
        yield {"step": "project", "message": f"Projected {len(edges)} physical edge(s)"}

        # Explicit asset-name fields (a registry's asset_name/name/title, or a
        # document mention's name) are preferred for the display name, so an
        # asset is named after the thing itself — not the longest incident or
        # topology sentence linked to it.
        def _clean_name(raw: dict[str, Any]) -> str:
            for key in ("asset_name", "name", "title", "equipment_name"):
                v = raw.get(key)
                if v and str(v).strip():
                    return str(v).strip()
            return ""

        clean_name_by_id = {
            r.record_id: _clean_name(raw_by_record.get(r.record_id, {}))
            for r in all_records
            if _clean_name(raw_by_record.get(r.record_id, {}))
        }
        names = _derive_names(all_records, result, identity_systems, clean_name_by_id)
        await rebuild_projection(conn, result, edges, discoveries, db_id, names)
        yield {"step": "persist", "message": "Rebuilt unified assets, identifiers and graph"}

        # 7. Operational-graph extraction — pull failures / work orders / rules
        #    out of operational-source records (grounded; one cached LLM call per
        #    record, never told the root cause) and link them to their resolved
        #    asset on the OPERATIONAL layer. Best-effort: never breaks ingest.
        member_to_cluster: dict[str, str] = {}
        for c in result.clusters:
            for m in c.members:
                member_to_cluster[m] = c.cluster_id
        try:
            op = await rebuild_operational_edges(
                conn, all_records, member_to_cluster, relation_by_system, raw_by_record
            )
            if op["fallbacks"]:
                warnings.append(f"{op['fallbacks']} operational record(s) used deterministic fallback; model extraction remains retryable")
            yield {
                "step": "operational",
                "message": (
                    f"Operational graph: {op['failures']} failure(s), "
                    f"{op['work_orders']} work order(s), {op['rules']} rule(s), "
                    f"{op['sensors']} sensor link(s) extracted from content"
                ),
                **op,
            }
        except Exception as exc:  # noqa: BLE001 - enrichment must not break ingest
            warnings.append("Operational extraction incomplete")
            yield {"step": "operational", "message": f"Operational extraction skipped ({type(exc).__name__})"}

        # 8. Materialize the OKF bundles (the queryable "second brain"): one
        #    human-readable + agent-searchable knowledge doc per canonical asset.
        try:
            from app.workflows.retrieval import materialize_okf_bundles

            n_emb = await materialize_okf_bundles(conn)
            missing = await conn.fetchval("select count(*) from okf_documents where embedding is null")
            if settings.embeddings_enabled and missing:
                warnings.append(f"{missing} OKF dossier(s) lack embeddings; semantic dossier search is incomplete")
            yield {
                "step": "bundle",
                "message": f"Materialized OKF knowledge bundles ({n_emb} (re)embedded)",
            }
        except Exception as exc:  # noqa: BLE001 - enrichment must not break ingest
            warnings.append("Knowledge bundles incomplete")
            yield {"step": "bundle", "message": f"Bundle materialization skipped ({type(exc).__name__})"}

        yield {
            "step": "complete",
            "message": "Ingestion complete" if not warnings else "Ingestion completed with incomplete enrichment",
            "warnings": warnings,
            "summary": {
                "new": new_count,
                "updated": counts["updated"],
                "skipped": skip_count,
                "unified_assets": len(result.clusters),
                "edges": len(edges),
                "review_items": len(result.review_items),
                "clusters": [
                    {
                        "unified_id": c.cluster_id,
                        "members": c.members,
                        "decision": c.decision.value,
                        "confidence": c.confidence,
                        "identifiers": c.identifiers,
                    }
                    for c in result.clusters
                ],
            },
        }


def _empty_summary() -> dict[str, Any]:
    return {
        "new": 0, "skipped": 0, "unified_assets": 0, "edges": 0,
        "review_items": 0, "clusters": [],
    }


async def ingest_events(
    systems: dict[str, list[dict[str, Any]]],
) -> AsyncIterator[str]:
    """SSE wrapper over ingest_records (used by POST /api/ingest)."""
    async for ev in ingest_records(systems):
        yield sse(ev)


def run_discovery(records: list[dict[str, Any]]) -> SchemaDiscoveryResult:
    return discover_schema(records)


def run_resolution(records: list[SourceRecord]) -> ResolutionResult:
    return resolve(records)
