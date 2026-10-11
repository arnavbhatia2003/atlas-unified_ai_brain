"""Persist the resolution projection into Postgres.

Design: `source_records` accumulate across connectors and syncs — they are the
durable ground truth and are NEVER wiped here. The unified assets, identifiers,
source-record links, physical graph edges, and (non-manual) review items are a
DERIVED projection of the canonical model, so they are rebuilt in full from the
resolution result on every sync. Operational edges (failures, work orders) are
left untouched.
"""

from __future__ import annotations

import json

from app.engine.graph.model import Edge
from app.engine.resolution import ResolutionResult
from app.engine.resolution.models import MergeDecision
from app.engine.schema_discovery import SchemaDiscoveryResult
from app.knowledge.identity import CanonicalConcept, is_anchor_eligible

_NEEDS_REVIEW = {MergeDecision.SUGGESTED, MergeDecision.REVIEW}


def _vector_literal(vector: list[float] | None) -> str | None:
    """Format a Python float list as a pgvector literal (e.g. "[0.1,0.2]")."""
    if not vector:
        return None
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


async def rebuild_projection(
    conn,
    result: ResolutionResult,
    edges: list[Edge],
    discoveries: dict[str, SchemaDiscoveryResult],
    db_id: dict[str, int],
    names: dict[str, str],
) -> None:
    """Rebuild the derived projection from a full-corpus resolution result.

    `db_id` maps a resolution record_id (system-prefixed, e.g. "IBM Maximo:a1")
    to its persisted source_records.id. `names` maps cluster_id to a human display
    name. source_records themselves are left in place (accumulated elsewhere).
    """
    async with conn.transaction():
        # Derived projection only — source_records are preserved.
        await conn.execute("delete from unified_assets")  # cascades identifiers + links
        await conn.execute("delete from edges where layer = 'physical'")
        await conn.execute("delete from review_queue where kind <> 'manual' and status = 'open'")

        # discovered fields (per system)
        for system, disc in discoveries.items():
            await conn.execute(
                "insert into source_systems (name, industry) values ($1, $2) "
                "on conflict (name) do update set industry = excluded.industry",
                system, disc.industry,
            )
            for f in disc.fields:
                await conn.execute(
                    """
                    insert into discovered_fields (system, field_name, detected_concept,
                        role, weight, confidence, cardinality_ratio, pattern_concept,
                        semantic_concept, semantic_score)
                    values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    on conflict (system, field_name) do update set
                        detected_concept = excluded.detected_concept,
                        role = excluded.role, weight = excluded.weight,
                        confidence = excluded.confidence
                    """,
                    system, f.field_name, f.detected_concept.value, f.role.value,
                    f.pattern_weight, f.confidence, f.cardinality_ratio,
                    f.pattern_concept.value, f.semantic_concept.value, f.semantic_score,
                )

        # unified assets + identifiers + source-record links
        for c in result.clusters:
            has_anchor = any(is_anchor_eligible(CanonicalConcept(k)) for k in c.identifiers if k in CanonicalConcept._value2member_map_)
            needs_review = c.decision in _NEEDS_REVIEW or (c.decision == MergeDecision.SINGLETON and not has_anchor)
            primary_serial = (c.identifiers.get("serial_number") or [None])[0]
            await conn.execute(
                """
                insert into unified_assets (unified_id, asset_name, entity_type,
                    status, needs_review, review_reason)
                values ($1,$2,'Asset','active',$3,$4)
                """,
                c.cluster_id,
                names.get(c.cluster_id) or primary_serial or c.cluster_id,
                needs_review,
                (f"decision={c.decision.value}" if c.decision in _NEEDS_REVIEW else "No validated instance identifier") if needs_review else "",
            )
            for concept, values in c.identifiers.items():
                for v in values:
                    await conn.execute(
                        """
                        insert into asset_identifiers (unified_id, concept, value, is_primary)
                        values ($1,$2,$3,$4)
                        """,
                        c.cluster_id, concept, v,
                        concept == "serial_number" and v == primary_serial,
                    )
            method = c.methods[0] if c.methods else "manual"
            for member in c.members:
                sid = db_id.get(member)
                if sid is None:
                    continue
                await conn.execute(
                    """
                    insert into asset_source_records (unified_id, source_record_id,
                        confidence, method)
                    values ($1,$2,$3,$4)
                    on conflict (unified_id, source_record_id) do nothing
                    """,
                    c.cluster_id, sid, float(c.confidence), method,
                )

        # projected physical edges
        if edges:
            await conn.executemany(
                """
                insert into edges (source_id, relation_type, target_id, layer, metadata)
                values ($1,$2,$3,$4,$5::jsonb)
                """,
                [
                    (e.source_id, e.relation_type.value, e.target_id,
                     e.layer.value, json.dumps(e.metadata))
                    for e in edges
                ],
            )

        # review queue (rebuilt from resolution; manual items preserved)
        for r in result.review_items:
            await conn.execute(
                "insert into review_queue (kind, payload, reason) values ($1,$2::jsonb,$3)",
                r.kind, json.dumps({"records": r.records, "detail": r.detail}), r.reason,
            )
