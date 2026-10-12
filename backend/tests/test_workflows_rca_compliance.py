"""Integration + structure tests for the RCA and Compliance LangGraph workflows.

Per the testing steering: assert on STRUCTURE (which stages ran, what state was
passed forward) — not on LLM wording. The LLM reasoner is mocked, so these tests
verify the graph wiring and grounding, not model output. Operational edges are
seeded in a transaction and rolled back; skips (not fails) if Postgres is down.
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
    work_order_node,
)
from app.engine.graph.store import GraphStore
from app.workflows.compliance import build_compliance_graph
from app.workflows.rca import build_rca_graph

load_dotenv()
DSN = os.environ.get("DATABASE_URL", "")

RCA_ASSET = "ua-wf-rca"
RCA_SERIAL = "sn-wf-rca-1"
COMP_ASSET = "ua-wf-comp"
RULE = "RULE-WF-TEST"


async def _seed_asset(conn, unified_id: str, serial: str) -> None:
    await conn.execute(
        "insert into unified_assets (unified_id, asset_name) values ($1, $2)",
        unified_id, serial,
    )
    await conn.execute(
        "insert into asset_identifiers (unified_id, concept, value, is_primary) "
        "values ($1, 'serial_number', $2, true)",
        unified_id, serial,
    )


def _rca_edges() -> list[Edge]:
    op = GraphLayer.OPERATIONAL
    R = RelationType
    node = asset_node(RCA_ASSET)
    return [
        Edge(source_id=node, relation_type=R.HAS_FAILURE,
             target_id=failure_node("WFSeizure"), layer=op, metadata={}),
        Edge(source_id=failure_node("WFSeizure"), relation_type=R.MAY_BE_CAUSED_BY,
             target_id=cause_node("WFMissedLube"), layer=op, metadata={"confidence": 0.9}),
        Edge(source_id=cause_node("WFMissedLube"), relation_type=R.VIOLATES,
             target_id=rule_node(RULE), layer=op, metadata={}),
    ]


def _run(scenario):
    try:
        asyncio.run(scenario())
    except AssertionError:
        raise
    except (OSError, asyncpg.CannotConnectNowError) as e:
        pytest.skip(f"Postgres not reachable: {e}")


def test_rca_workflow_reasons_over_seeded_failure_evidence():
    captured: dict = {}

    async def fake_reasoner(question, evidence):
        captured["question"] = question
        captured["evidence"] = evidence
        return {
            "summary": "seeded-cause",
            "hypotheses": [{"cause": "WFMissedLube", "explanation": "e",
                            "evidence": [cause_node("WFMissedLube")], "confidence": 0.9}],
            "contradictions": [],
            "unresolved": [],
        }

    async def scenario():
        conn = await asyncpg.connect(dsn=DSN, timeout=5)
        tr = conn.transaction()
        await tr.start()
        try:
            await _seed_asset(conn, RCA_ASSET, RCA_SERIAL)
            await GraphStore().write_edges(conn, _rca_edges())

            graph = build_rca_graph(reasoner=fake_reasoner)
            state = await graph.ainvoke(
                {"conn": conn, "question": "why did it fail",
                 "asset_hint": RCA_SERIAL, "stages": []}
            )

            # structure: all four stages ran, in the pipeline
            assert state["stages"] == [
                "resolve_asset", "gather_evidence", "reason", "assemble"
            ]
            # resolution grounded to the canonical asset
            assert state["unified_id"] == RCA_ASSET
            assert state["result"]["resolved"] is True
            # failure evidence was detected, so the reasoner WAS invoked
            assert state["has_failure_evidence"] is True
            assert state["result"]["report"]["summary"] == "seeded-cause"
            # state passed forward: reasoner received the causal chain reaching the rule
            chain_targets = {r["target_id"] for r in captured["evidence"]["causal_chain"]}
            assert rule_node(RULE) in chain_targets
            assert state["result"]["evidence_from"] == "index"
        finally:
            await tr.rollback()
            await conn.close()

    _run(scenario)


def test_rca_workflow_unknown_asset_is_flagged_not_fabricated():
    async def fake_reasoner(question, evidence):  # must NOT be called
        raise AssertionError("reasoner called for an unresolved asset")

    async def scenario():
        conn = await asyncpg.connect(dsn=DSN, timeout=5)
        tr = conn.transaction()
        await tr.start()
        try:
            graph = build_rca_graph(reasoner=fake_reasoner)
            state = await graph.ainvoke(
                {"conn": conn, "question": "why did it fail",
                 "asset_hint": "no-such-asset-zzz", "stages": []}
            )
            # skips evidence/reason; goes resolve -> assemble
            assert state["stages"] == ["resolve_asset", "assemble"]
            assert state["result"]["resolved"] is False
            assert state["result"]["report"] is None
        finally:
            await tr.rollback()
            await conn.close()

    _run(scenario)


def test_compliance_workflow_at_risk_is_deterministic():
    async def fake_reasoner(question, evidence):
        # narrative only; at-risk list must come from SQL, not from here
        return {"summary": "n", "posture": "at_risk",
                "contradictions": [], "unresolved": []}

    async def scenario():
        conn = await asyncpg.connect(dsn=DSN, timeout=5)
        tr = conn.transaction()
        await tr.start()
        try:
            await _seed_asset(conn, COMP_ASSET, "sn-wf-comp-1")
            # subject to the rule, with NO completed work order -> at risk
            await GraphStore().write_edges(conn, [
                Edge(source_id=asset_node(COMP_ASSET),
                     relation_type=RelationType.SUBJECT_TO,
                     target_id=rule_node(RULE), layer=GraphLayer.PHYSICAL, metadata={}),
            ])

            graph = build_compliance_graph(reasoner=fake_reasoner)
            state = await graph.ainvoke(
                {"conn": conn, "question": "who is at risk",
                 "rule_hint": RULE, "stages": []}
            )

            assert state["stages"] == ["resolve_scope", "gather", "reason", "assemble"]
            assert state["result"]["scope"] == "rule"
            # deterministic at-risk list contains the seeded asset node
            at_risk_assets = [a["asset"] for a in state["result"]["at_risk_assets"]]
            assert asset_node(COMP_ASSET) in at_risk_assets
            assert state["has_compliance_evidence"] is True
        finally:
            await tr.rollback()
            await conn.close()

    _run(scenario)


def test_compliance_workflow_no_rules_is_honest():
    async def fake_reasoner(question, evidence):  # must NOT be called
        raise AssertionError("reasoner called with no compliance evidence")

    async def scenario():
        conn = await asyncpg.connect(dsn=DSN, timeout=5)
        tr = conn.transaction()
        await tr.start()
        try:
            graph = build_compliance_graph(reasoner=fake_reasoner)
            state = await graph.ainvoke(
                {"conn": conn, "question": "at risk?",
                 "rule_hint": "RULE-DOES-NOT-EXIST", "stages": []}
            )
            assert state["has_compliance_evidence"] is False
            assert state["result"]["narrative"]["posture"] == "unknown"
            assert state["result"]["at_risk_assets"] == []
        finally:
            await tr.rollback()
            await conn.close()

    _run(scenario)
