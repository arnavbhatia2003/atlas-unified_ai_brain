"""Compliance workflow (LangGraph).

A controlled 4-stage graph over the knowledge graph:

    resolve_scope -> gather -> reason -> assemble

Two scopes:
  - rule scope   (rule_hint):  which assets subject to a rule are at risk?
  - asset scope  (asset_hint): which rules does an asset owe, and is it at risk?

The at-risk determination is DETERMINISTIC SQL (a compliance verdict must be
auditable, not model-generated). The single LLM call only writes the narrative
posture summary and surfaces contradictions; it never changes the authoritative
at-risk list. If no compliance rules are linked, we say so plainly (no LLM call).

Cost per run: ~1 LLM call (0 if no compliance evidence). Per-stage SSE status.
"""

from __future__ import annotations

import json
import operator
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app.db import get_pool
from app.engine.graph.model import RelationType, asset_node, rule_node
from app.engine.graph.store import GraphStore
from app.services.llm import get_fast_chat_model, resilient

from .base import emit, sse
from .resolve_mention import resolve_mention
from .retrieval import asset_facts, graph_neighborhood, save_workflow_run

_graph_store = GraphStore()

Reasoner = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class ComplianceNarrative(BaseModel):
    """LLM-written narrative ONLY. The at-risk list stays deterministic."""

    summary: str = Field(description="plain-language compliance posture summary")
    posture: str = Field(description="one of: compliant | at_risk | unknown")
    contradictions: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


_SYSTEM = (
    "You are a compliance analyst. Summarize the compliance posture using ONLY "
    "the evidence provided. The at-risk asset list is authoritative and computed "
    "from records — do not add to or remove from it; only explain it. Cite the "
    "evidence ids you rely on and surface any contradicting or unresolved evidence "
    "explicitly. Set posture to 'at_risk' if any asset is at risk, 'compliant' if "
    "none are, or 'unknown' if evidence is insufficient."
)


_JSON_SHAPE = (
    '\n\nRespond with ONLY a JSON object (no prose, no markdown fences) of exactly '
    'this shape:\n'
    '{"summary": "plain-language posture summary", '
    '"posture": "compliant | at_risk | unknown", '
    '"contradictions": ["..."], "unresolved": ["..."]}'
)


def _text_of(raw: Any) -> str:
    c = getattr(raw, "content", raw)
    return c if isinstance(c, str) else str(c)


def _reasoning_of(raw: Any) -> str:
    ak = getattr(raw, "additional_kwargs", {}) or {}
    return str(ak.get("reasoning_content") or ak.get("reasoning") or "")


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if "```" in text:
        text = text.replace("```json", "```").split("```")[1] if text.count("```") >= 2 else text.strip("`")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


async def _default_reason(question: str, evidence: dict[str, Any]) -> dict[str, Any]:
    # Manual JSON (ChatNVIDIA structured output is unreliable). The at-risk list is
    # authoritative/deterministic; the model only writes the narrative posture.
    model = resilient(get_fast_chat_model())
    q = question.strip() or (
        "No specific question was asked. Summarize the compliance posture strictly "
        "from the linked evidence and the authoritative at-risk list."
    )
    human = (
        f"Question: {q}\n\nEvidence (JSON):\n"
        f"{json.dumps(evidence, indent=2, default=str)}{_JSON_SHAPE}"
    )
    at_risk = evidence.get("at_risk") or []
    data: dict[str, Any] | None = None
    for _ in range(2):
        raw = await model.ainvoke(
            [SystemMessage(content=_SYSTEM), HumanMessage(content=human)]
        )
        data = _extract_json(_text_of(raw)) or _extract_json(_reasoning_of(raw))
        if data:
            break
    if not data:
        return {
            "summary": "Posture summary unavailable this run; see the at-risk list.",
            "posture": "at_risk" if at_risk else "unknown",
            "contradictions": [],
            "unresolved": [],
        }
    posture = str(data.get("posture", "")).strip().lower()
    if at_risk:
        posture = "at_risk"
    if posture not in ("compliant", "at_risk", "unknown"):
        posture = "at_risk" if at_risk else "unknown"
    return {
        "summary": str(data.get("summary", "")),
        "posture": posture,
        "contradictions": [str(c) for c in (data.get("contradictions") or [])],
        "unresolved": [str(u) for u in (data.get("unresolved") or [])],
    }


class ComplianceState(TypedDict, total=False):
    conn: Any
    question: str
    rule_hint: str | None
    asset_hint: str | None
    scope: str                       # "rule" | "asset" | "none"
    rule_id: str | None
    unified_id: str | None
    resolution: dict[str, Any]
    at_risk: list[dict[str, Any]]    # deterministic, authoritative
    rules: list[dict[str, Any]]      # SUBJECT_TO edges in scope
    has_compliance_evidence: bool
    narrative: dict[str, Any] | None
    stages: Annotated[list[str], operator.add]
    result: dict[str, Any] | None


async def _asset_rule_status(
    conn: Any, asset_node_id: str
) -> list[dict[str, Any]]:
    """For an asset: each rule it is SUBJECT_TO + whether a completed WO exists."""
    rows = await conn.fetch(
        """
        select s.target_id as rule,
               exists (
                   select 1 from edges w
                   where w.source_id = s.source_id
                     and w.relation_type = 'HAS_WORK_ORDER'
                     and (w.metadata ->> 'status') = 'completed'
        and (w.metadata->>'rule_id') in (s.target_id, substring(s.target_id from 6))
        and (s.metadata->>'period' is null or w.metadata->>'period' = s.metadata->>'period')
               ) as satisfied
        from edges s
        where s.source_id = $1 and s.relation_type = $2
        order by s.target_id
        """,
        asset_node_id, RelationType.SUBJECT_TO.value,
    )
    return [{"rule": r["rule"], "satisfied": r["satisfied"]} for r in rows]


def build_compliance_graph(reasoner: Reasoner = _default_reason) -> Any:
    async def resolve_scope(state: ComplianceState) -> dict[str, Any]:
        conn = state["conn"]
        rule_hint = (state.get("rule_hint") or "").strip()
        asset_hint = (state.get("asset_hint") or "").strip()
        if rule_hint:
            emit("resolve", f"Scope: rule '{rule_hint}'")
            return {"stages": ["resolve_scope"], "scope": "rule", "rule_id": rule_hint}
        if asset_hint:
            emit("resolve", f"Resolving asset '{asset_hint}'")
            res = await resolve_mention(conn, asset_hint)
            uid = res.asset.unified_id if res.asset else None
            emit(
                "resolve",
                f"Resolved to {uid}" if uid else (res.reason or "Asset not resolved"),
            )
            return {
                "stages": ["resolve_scope"],
                "scope": "asset" if uid else "none",
                "unified_id": uid,
                "resolution": res.model_dump(),
            }
        emit("resolve", "No rule or asset scope provided")
        return {"stages": ["resolve_scope"], "scope": "none"}

    async def gather(state: ComplianceState) -> dict[str, Any]:
        conn = state["conn"]
        scope = state.get("scope")
        at_risk: list[dict[str, Any]] = []
        rules: list[dict[str, Any]] = []

        if scope == "rule":
            rule_node_id = rule_node(state["rule_id"])
            asset_nodes = await _graph_store.compliance_at_risk(conn, rule_node_id)
            at_risk = [
                {"asset": a, "rule": state["rule_id"],
                 "reason": "subject to rule with no completed work order"}
                for a in asset_nodes
            ]
            subject_rows = await conn.fetch(
                "select source_id from edges where relation_type = $1 and target_id = $2",
                RelationType.SUBJECT_TO.value, rule_node_id,
            )
            rules = [{"asset": r["source_id"], "rule": state["rule_id"]} for r in subject_rows]
            has_evidence = bool(subject_rows)
        elif scope == "asset":
            node = asset_node(state["unified_id"])
            statuses = await _asset_rule_status(conn, node)
            rules = statuses
            at_risk = [
                {"asset": node, "rule": s["rule"],
                 "reason": "no completed work order satisfies this rule"}
                for s in statuses if not s["satisfied"]
            ]
            has_evidence = bool(statuses)
        else:
            has_evidence = False

        emit(
            "gather",
            f"Checked compliance: {len(rules)} rule link(s), {len(at_risk)} at risk",
            at_risk=len(at_risk),
        )
        return {
            "stages": ["gather"],
            "at_risk": at_risk,
            "rules": rules,
            "has_compliance_evidence": has_evidence,
        }

    async def reason(state: ComplianceState) -> dict[str, Any]:
        if not state.get("has_compliance_evidence"):
            emit("reason", "No compliance rules linked to this scope in the index.")
            narrative = {
                "summary": (
                    "No compliance rules are linked to this scope in the current "
                    "index, so compliance cannot be determined from available evidence."
                ),
                "posture": "unknown",
                "contradictions": [],
                "unresolved": ["No SUBJECT_TO (rule) evidence found for this scope."],
            }
            return {"stages": ["reason"], "narrative": narrative}
        emit("reason", "Summarizing compliance posture against evidence")
        evidence = {
            "scope": state.get("scope"),
            "rule_id": state.get("rule_id"),
            "at_risk": state.get("at_risk"),
            "rule_links": state.get("rules"),
        }
        narrative = await reasoner(state.get("question", ""), evidence)
        emit("reason", f"Posture: {narrative.get('posture', 'unknown')}")
        return {"stages": ["reason"], "narrative": narrative}

    async def assemble(state: ComplianceState) -> dict[str, Any]:
        result = {
            "scope": state.get("scope"),
            "rule_id": state.get("rule_id"),
            "asset": state.get("unified_id"),
            "resolution": state.get("resolution"),
            # Deterministic, authoritative — never model-generated.
            "at_risk_assets": state.get("at_risk", []),
            "narrative": state.get("narrative"),
            "evidence_from": "index",
        }
        emit("complete", "Compliance analysis complete", result=result)
        return {"stages": ["assemble"], "result": result}

    g = StateGraph(ComplianceState)
    g.add_node("resolve_scope", resolve_scope)
    g.add_node("gather", gather)
    g.add_node("reason", reason)
    g.add_node("assemble", assemble)

    g.add_edge(START, "resolve_scope")
    g.add_edge("resolve_scope", "gather")
    g.add_edge("gather", "reason")
    g.add_edge("reason", "assemble")
    g.add_edge("assemble", END)
    return g.compile()


_compiled = None


def get_compliance_graph() -> Any:
    global _compiled
    if _compiled is None:
        _compiled = build_compliance_graph()
    return _compiled


async def run_compliance_events(
    question: str,
    rule_hint: str | None = None,
    asset_hint: str | None = None,
) -> AsyncIterator[str]:
    """Run compliance analysis, streaming per-stage status (SSE)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        state: ComplianceState = {
            "conn": conn,
            "question": question,
            "rule_hint": rule_hint,
            "asset_hint": asset_hint,
            "stages": [],
        }
        yield sse({"step": "start", "message": "Starting compliance analysis"})
        final: dict[str, Any] | None = None
        async for chunk in get_compliance_graph().astream(state, stream_mode="custom"):
            if chunk.get("step") == "complete":
                final = chunk.get("result")
            yield sse(chunk)
        await save_workflow_run(
            conn, "compliance", question, final, asset=asset_hint, rule=rule_hint
        )
