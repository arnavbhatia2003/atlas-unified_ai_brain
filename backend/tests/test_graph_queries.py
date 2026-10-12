"""Integration tests: recursive-CTE graph traversal in Postgres.

Seeds the architecture doc's Section 9 RCA example into the `edges` table inside
a transaction, runs the RCA and compliance queries, then rolls back so the DB is
left clean. Skips (does not fail) if Postgres is not reachable.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from dotenv import load_dotenv

from app.engine.graph.model import (
    Edge,
    GraphLayer,
    RelationType,
    asset_node,
    cause_node,
    failure_node,
    rule_node,
    sensor_node,
    work_order_node,
)
from app.engine.graph.store import GraphStore

load_dotenv()
DSN = os.environ.get("DATABASE_URL", "")

ASSET = asset_node("ua-4521987")
OTHER = asset_node("ua-9999")


def _fixture_edges() -> list[Edge]:
    op, phys = GraphLayer.OPERATIONAL, GraphLayer.PHYSICAL
    R = RelationType
    return [
        Edge(source_id=ASSET, relation_type=R.HAS_FAILURE,
             target_id=failure_node("BearingSeizure"), layer=op,
             metadata={"downtime_hours": 8}),
        Edge(source_id=failure_node("BearingSeizure"), relation_type=R.MAY_BE_CAUSED_BY,
             target_id=cause_node("MissedLubrication"), layer=op, metadata={"confidence": 0.95}),
        Edge(source_id=failure_node("BearingSeizure"), relation_type=R.MAY_BE_CAUSED_BY,
             target_id=cause_node("VibrationSpike"), layer=op, metadata={"confidence": 0.90}),
        Edge(source_id=cause_node("MissedLubrication"), relation_type=R.VIOLATES,
             target_id=rule_node("RULE-LUBE-001"), layer=op, metadata={}),
        Edge(source_id=cause_node("VibrationSpike"), relation_type=R.VIOLATES,
             target_id=rule_node("RULE-VIB-001"), layer=op, metadata={}),
        Edge(source_id=ASSET, relation_type=R.HAS_WORK_ORDER,
             target_id=work_order_node("WO-2024-3156"), layer=op, metadata={"status": "cancelled"}),
        Edge(source_id=ASSET, relation_type=R.HAS_SENSOR,
             target_id=sensor_node("vibration"), layer=phys, metadata={}),
        # asset ua-4521987 IS subject to the lube rule but HAS a completed WO -> not at risk
        Edge(source_id=ASSET, relation_type=R.SUBJECT_TO,
             target_id=rule_node("RULE-LUBE-001"), layer=phys, metadata={}),
        Edge(source_id=ASSET, relation_type=R.HAS_WORK_ORDER,
             target_id=work_order_node("WO-DONE"), layer=op, metadata={"status": "completed", "rule_id": "RULE-LUBE-001"}),
        # asset ua-9999 subject to same rule, NO completed WO -> at risk
        Edge(source_id=OTHER, relation_type=R.SUBJECT_TO,
             target_id=rule_node("RULE-LUBE-001"), layer=phys, metadata={}),
    ]


def _run(scenario):
    try:
        asyncio.run(scenario())
    except AssertionError:
        raise
    except (OSError, asyncpg.CannotConnectNowError) as e:
        pytest.skip(f"Postgres not reachable: {e}")


def test_rca_traversal_reaches_causal_rules():
    async def scenario():
        conn = await asyncpg.connect(dsn=DSN, timeout=5)
        tr = conn.transaction()
        await tr.start()
        try:
            store = GraphStore()
            await store.write_edges(conn, _fixture_edges())
            rows = await store.rca(conn, ASSET)
            targets = {r["target_id"] for r in rows}
            # the causal chain reaches both violated rules
            assert rule_node("RULE-LUBE-001") in targets
            assert rule_node("RULE-VIB-001") in targets
            # intermediate nodes present
            assert failure_node("BearingSeizure") in targets
            assert cause_node("MissedLubrication") in targets
            assert cause_node("VibrationSpike") in targets
            # rules are 3 hops from the asset (asset->failure->cause->rule)
            rule_depths = [r["depth"] for r in rows
                           if r["target_id"].startswith("rule:")]
            assert rule_depths and min(rule_depths) == 3
        finally:
            await tr.rollback()
            await conn.close()

    _run(scenario)


def test_compliance_propagation_finds_at_risk_asset():
    async def scenario():
        conn = await asyncpg.connect(dsn=DSN, timeout=5)
        tr = conn.transaction()
        await tr.start()
        try:
            store = GraphStore()
            await store.write_edges(conn, _fixture_edges())
            at_risk = await store.compliance_at_risk(conn, rule_node("RULE-LUBE-001"))
            # ua-9999 has no completed WO -> at risk; ua-4521987 has one -> excluded
            assert at_risk == [OTHER]
        finally:
            await tr.rollback()
            await conn.close()

    _run(scenario)
