"""GraphStore — persist and traverse the graph in Postgres via recursive CTEs.

This is the capability that replaces a native graph database: multi-hop
traversal (RCA causal chains, compliance propagation) expressed as recursive
CTEs over the `edges` table. All methods take an asyncpg connection so callers
control the transaction.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from .model import Edge, RelationType

# Relations followed by an RCA walk: Asset -> Failure -> Cause -> Rule,
# plus the asset's work orders and sensors for corroborating evidence.
RCA_RELATIONS = [
    RelationType.HAS_FAILURE.value,
    RelationType.MAY_BE_CAUSED_BY.value,
    RelationType.VIOLATES.value,
    RelationType.HAS_WORK_ORDER.value,
    RelationType.HAS_SENSOR.value,
]

_TRAVERSE_SQL = """
WITH RECURSIVE walk AS (
    SELECT e.source_id, e.relation_type, e.target_id, e.metadata, 1 AS depth,
           ARRAY[e.source_id, e.target_id] AS path
    FROM edges e
    WHERE e.source_id = $1
      AND ($2::text[] IS NULL OR e.relation_type = ANY($2))
    UNION ALL
    SELECT e.source_id, e.relation_type, e.target_id, e.metadata, w.depth + 1,
           w.path || e.target_id
    FROM edges e
    JOIN walk w ON e.source_id = w.target_id
    WHERE w.depth < $3
      AND ($2::text[] IS NULL OR e.relation_type = ANY($2))
      AND NOT e.target_id = ANY(w.path)     -- guard against cycles
)
SELECT depth, source_id, relation_type, target_id, metadata, path
FROM walk
ORDER BY depth, source_id, target_id;
"""

_COMPLIANCE_SQL = """
SELECT DISTINCT s.source_id AS asset
FROM edges s
WHERE s.relation_type = 'SUBJECT_TO' AND s.target_id = $1
  AND NOT EXISTS (
      SELECT 1 FROM edges w
      WHERE w.source_id = s.source_id
        AND w.relation_type = 'HAS_WORK_ORDER'
        AND (w.metadata ->> 'status') = 'completed'
        and (w.metadata->>'rule_id') in ($1, substring($1 from 6))
        and (s.metadata->>'period' is null or w.metadata->>'period' = s.metadata->>'period')
  )
ORDER BY asset;
"""


def _row(record: asyncpg.Record) -> dict[str, Any]:
    d = dict(record)
    meta = d.get("metadata")
    if isinstance(meta, str):
        d["metadata"] = json.loads(meta)
    return d


class GraphStore:
    """Thin async wrapper over the edges table (recursive-CTE graph queries)."""

    async def write_edges(self, conn: asyncpg.Connection, edges: list[Edge]) -> int:
        if not edges:
            return 0
        await conn.executemany(
            """
            insert into edges (source_id, relation_type, target_id, layer, metadata)
            values ($1, $2, $3, $4, $5::jsonb)
            """,
            [
                (
                    e.source_id,
                    e.relation_type.value,
                    e.target_id,
                    e.layer.value,
                    json.dumps(e.metadata),
                )
                for e in edges
            ],
        )
        return len(edges)

    async def traverse(
        self,
        conn: asyncpg.Connection,
        start: str,
        relations: list[str] | None = None,
        max_depth: int = 6,
    ) -> list[dict[str, Any]]:
        rows = await conn.fetch(_TRAVERSE_SQL, start, relations, max_depth)
        return [_row(r) for r in rows]

    async def rca(
        self, conn: asyncpg.Connection, asset_node_id: str, max_depth: int = 6
    ) -> list[dict[str, Any]]:
        """Walk the causal chain from an asset (Asset -> Failure -> Cause -> Rule)."""
        return await self.traverse(conn, asset_node_id, RCA_RELATIONS, max_depth)

    async def compliance_at_risk(
        self, conn: asyncpg.Connection, rule_node_id: str
    ) -> list[str]:
        """Assets subject to a rule that lack a completed work order -> at risk."""
        rows = await conn.fetch(_COMPLIANCE_SQL, rule_node_id)
        return [r["asset"] for r in rows]
