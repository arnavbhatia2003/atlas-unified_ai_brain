"""Root-Cause Analysis workflow (LangGraph).

A controlled 4-stage graph over the operational knowledge graph:

    resolve_asset -> gather_evidence -> reason -> assemble

- resolve_asset:  canonical mention resolution (no LLM). Unknown -> stop, flagged.
- gather_evidence: recursive-CTE walk (Asset->Failure->Cause->Rule) + facts +
                   neighborhood. Pure SQL, no LLM.
- reason:         exactly ONE structured LLM call producing ranked hypotheses with
                  cited evidence + confidence + contradictions. Skipped entirely
                  when the graph holds no failure evidence (grounding: say so
                  plainly instead of inventing a cause).
- assemble:       finalize the grounded result (+ evidence provenance).

Cost per run: ~1 LLM call (0 if no failure evidence). Per-stage SSE status via
``emit``. The LLM reasoner is injectable so tests can assert structure without
depending on model wording.
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
from app.engine.graph.model import RelationType, asset_node
from app.engine.graph.store import GraphStore
from app.services.llm import get_fast_chat_model, resilient

from .base import emit, sse
from .resolve_mention import resolve_mention
from .retrieval import asset_facts, graph_neighborhood, save_workflow_run

_graph_store = GraphStore()

# Relations that constitute actual failure evidence (vs. mere structure).
_FAILURE_RELATIONS = {
    RelationType.HAS_FAILURE.value,
    RelationType.MAY_BE_CAUSED_BY.value,
    RelationType.VIOLATES.value,
}

Reasoner = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def _upstream_failures(conn: Any, uid: str) -> list[dict[str, Any]]:
    """Failures on the assets this asset DEPENDS_ON (one hop) — multi-hop RCA:
    a symptom here may be caused by a fault on an upstream asset it relies on."""
    rows = await conn.fetch(
        """
        select e.target_id as dep, f.metadata as meta
        from edges e
        join edges f on f.source_id = e.target_id and f.relation_type = 'HAS_FAILURE'
        where e.source_id = $1 and e.relation_type = 'DEPENDS_ON'
        """,
        asset_node(uid),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        meta = r["meta"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        out.append(
            {
                "depends_on": str(r["dep"]).replace("asset:", ""),
                "observation": (meta or {}).get("observation", ""),
                "detail": (meta or {}).get("detail", ""),
                "component": (meta or {}).get("component", ""),
                "citation": (meta or {}).get("citation", ""),
            }
        )
    return out


# --- structured LLM output -------------------------------------------------
class Hypothesis(BaseModel):
    cause: str = Field(description="the candidate root cause")
    explanation: str = Field(description="why the evidence supports this cause")
    evidence: list[str] = Field(
        default_factory=list, description="citation ids (e.g. node/record ids) used"
    )
    confidence: float = Field(description="0..1 confidence given ONLY the evidence")


class RCAReport(BaseModel):
    summary: str
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    contradictions: list[str] = Field(
        default_factory=list, description="conflicting/unresolved evidence, stated explicitly"
    )
    unresolved: list[str] = Field(default_factory=list)


_SYSTEM = (
    "You are a reliability engineer. You are given the OBSERVED failures for ONE "
    "asset — symptoms only; the diagnosed cause was deliberately withheld. Your job "
    "is to infer the most likely ROOT CAUSE of each observed failure from the "
    "symptoms alone.\n"
    "STRICT RULES:\n"
    "- Use ONLY the observations listed below. Do NOT invent equipment makes, "
    "models, part numbers, dates, locations, or measurements that are not present "
    "in the evidence. If you state a fact not in the evidence, that is an error.\n"
    "- Give one or more ranked hypotheses (most likely first). Each hypothesis's "
    "`evidence` field must list citation id(s) — and you may cite ONLY the exact "
    "citation ids shown in square brackets in the evidence below. NEVER invent, "
    "guess, or reformat an id; if no listed id supports a claim, cite none.\n"
    "- Reason from first principles: for each failure, consider what physical "
    "failure mode of the named component would produce exactly those observed "
    "symptoms, and explain the link. Do not rely on outside assumptions about the "
    "specific asset.\n"
    "- Multi-hop: if an 'Upstream dependency failure' plausibly explains one of "
    "this asset's symptoms (e.g. an upstream network/power/feed fault causing a "
    "downstream symptom), name that upstream fault as the likely root cause and "
    "cite its id — the true cause can sit one hop away on a depended-on asset.\n"
    "- If evidence is insufficient for a confident cause, say so and lower "
    "confidence. Surface contradicting/unresolved evidence explicitly."
)


def _format_evidence(evidence: dict[str, Any]) -> str:
    lines = [f"Asset: {evidence.get('asset')}"]
    ident = evidence.get("identifiers")
    if ident:
        lines.append(f"Identifiers: {ident}")
    failures = evidence.get("observed_failures") or []
    if failures:
        lines.append("\nObserved failures (symptoms only — cause withheld):")
        for i, f in enumerate(failures, 1):
            cite = f.get("citation") or "?"
            comp = f.get("component") or "unspecified component"
            lines.append(f"  {i}. [{cite}] component: {comp}")
            lines.append(f"     observed: {f.get('detail') or f.get('observation')}")
    upstream = evidence.get("upstream_failures") or []
    if upstream:
        lines.append(
            "\nUpstream dependency failures (this asset DEPENDS_ON these — a "
            "symptom here may originate there):"
        )
        for u in upstream:
            cite = u.get("citation") or "?"
            lines.append(
                f"  - depends on {u.get('depends_on')} [{cite}] "
                f"({u.get('component') or 'component n/a'}): "
                f"{u.get('detail') or u.get('observation')}"
            )
    # Any already-known causal links (failure -> cause -> rule) from the graph.
    causal = [
        r
        for r in (evidence.get("causal_chain") or [])
        if r.get("relation_type")
        in (RelationType.MAY_BE_CAUSED_BY.value, RelationType.VIOLATES.value)
    ]
    if causal:
        lines.append("\nKnown causal links in the graph:")
        for r in causal:
            rel = str(r.get("relation_type", "")).lower().replace("_", " ")
            lines.append(f"  - {r.get('source_id')} {rel} {r.get('target_id')}")
    maint = evidence.get("maintenance_history") or []
    if maint:
        lines.append("\nMaintenance history:")
        for m in maint:
            lines.append(
                f"  - [{m.get('citation')}] ({m.get('status') or 'n/a'}) {m.get('detail')}"
            )
    if not failures:
        lines.append("\n(No observed failures are linked to this asset.)")
    return "\n".join(lines)


_JSON_SHAPE = (
    '\n\nRespond with ONLY a JSON object (no prose, no markdown fences) of exactly '
    'this shape:\n'
    '{"summary": "one-paragraph synthesis", '
    '"hypotheses": [{"cause": "the root cause", "explanation": "why the observed '
    'symptoms point to it", "evidence": ["citation id(s)"], "confidence": 0.0-1.0}], '
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


def _calibrate_confidence(
    report: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, Any]:
    """Cap each hypothesis's confidence by how much the EVIDENCE actually supports
    it — so confidence tracks grounding, not the model's eagerness.

    Domain-neutral factors (no dataset-specific cues):
      * grounded    — the hypothesis cites an evidence id that really exists for
                       this asset. Ungrounded claims are capped low.
      * resolving   — a resolving upstream-dependency cause exists and is used.
      * corroborated— multiple observed failures / multiple distinct sources.
      * contradicted— open contradictions lower the ceiling.
    A hypothesis can only approach high confidence when it is grounded AND a
    resolving cause is present; thin evidence stays low (calibrated abstention).
    """
    # Build the set of citation ids that genuinely exist in the evidence.
    known: set[str] = set()
    for f in evidence.get("observed_failures") or []:
        if f.get("citation"):
            known.add(str(f["citation"]).lower())
    for m in evidence.get("maintenance_history") or []:
        if m.get("citation"):
            known.add(str(m["citation"]).lower())
    upstream_ids: set[str] = set()
    for u in evidence.get("upstream_failures") or []:
        for key in ("citation", "depends_on"):
            if u.get(key):
                known.add(str(u[key]).lower())
                upstream_ids.add(str(u[key]).lower())
    for row in evidence.get("causal_chain") or []:
        for key in ("source_id", "target_id"):
            if row.get(key):
                known.add(str(row[key]).lower())

    n_failures = len(evidence.get("observed_failures") or [])
    has_upstream = bool(evidence.get("upstream_failures"))
    n_contra = len(report.get("contradictions") or [])
    # distinct evidence sources cited across all hypotheses (corroboration)
    all_cited: set[str] = set()

    def _matches(ev_id: str, pool: set[str]) -> bool:
        e = ev_id.strip().lower()
        return bool(e) and e in pool

    for h in report.get("hypotheses") or []:
        ev_ids = [str(e) for e in (h.get("evidence") or [])]
        grounded = bool(ev_ids) and all(_matches(e, known) for e in ev_ids)
        for e in ev_ids:
            if _matches(e, known):
                all_cited.add(e.strip().lower())
        text = f"{h.get('cause','')} {h.get('explanation','')}".lower()
        uses_upstream = has_upstream and (
            any(_matches(e, upstream_ids) for e in ev_ids)
        )

        cap = 0.45
        if grounded:
            cap += 0.25
        if uses_upstream:
            cap += 0.20  # a resolving cause was found one hop away
        if n_failures >= 2:
            cap += 0.05  # corroborated by multiple observations
        cap -= 0.08 * min(n_contra, 3)
        if not grounded:
            cap = min(cap, 0.40)  # ungrounded claim can't be confident
        cap = max(0.10, min(0.98, cap))  # never assert absolute certainty

        try:
            model_conf = float(h.get("confidence", 0.5))
        except (TypeError, ValueError):
            model_conf = 0.5
        h["confidence"] = round(min(model_conf, cap), 2)

    return report


def _coerce_report(data: dict[str, Any]) -> dict[str, Any]:
    hyps: list[dict[str, Any]] = []
    for h in data.get("hypotheses") or []:
        if not isinstance(h, dict):
            continue
        try:
            conf = float(h.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        hyps.append(
            {
                "cause": str(h.get("cause", "")).strip() or "Unspecified cause",
                "explanation": str(h.get("explanation", "")),
                "evidence": [str(e) for e in (h.get("evidence") or [])],
                "confidence": max(0.0, min(1.0, conf)),
            }
        )
    return {
        "summary": str(data.get("summary", "")),
        "hypotheses": hyps,
        "contradictions": [str(c) for c in (data.get("contradictions") or [])],
        "unresolved": [str(u) for u in (data.get("unresolved") or [])],
    }


async def _default_reason(question: str, evidence: dict[str, Any]) -> dict[str, Any]:
    # ChatNVIDIA's structured-output path is unreliable for nested schemas (often
    # returns None), so we prompt for plain JSON and parse it tolerantly. The fast
    # model runs at temperature 0 → cleaner, better-grounded JSON than the MoE
    # model, which tended to fabricate ungrounded RCA narratives here.
    model = resilient(get_fast_chat_model())
    # An empty question is NOT an assertion that the asset failed; it simply asks
    # to diagnose whatever failures are linked (this node only runs when failure
    # evidence exists — otherwise RCA says so and skips the LLM).
    q = question.strip() or "Infer the most likely root cause of each observed failure."
    human = f"{_format_evidence(evidence)}\n\nTask: {q}{_JSON_SHAPE}"
    data: dict[str, Any] | None = None
    for _ in range(2):
        raw = await model.ainvoke(
            [SystemMessage(content=_SYSTEM), HumanMessage(content=human)]
        )
        data = _extract_json(_text_of(raw)) or _extract_json(_reasoning_of(raw))
        if data:
            break
    if not data:
        failures = evidence.get("observed_failures") or []
        return {
            "summary": (
                "A structured analysis could not be produced this run; the observed "
                "failures are listed below for manual review."
            ),
            "hypotheses": [],
            "contradictions": [],
            "unresolved": [
                f"[{f.get('citation')}] {f.get('detail') or f.get('observation')}"
                for f in failures
            ],
        }
    return _coerce_report(data)


# --- graph state -----------------------------------------------------------
class RCAState(TypedDict, total=False):
    conn: Any
    question: str
    asset_hint: str | None
    unified_id: str | None
    resolution: dict[str, Any]
    facts: dict[str, Any] | None
    chain: list[dict[str, Any]]
    neighborhood: list[dict[str, Any]]
    upstream: list[dict[str, Any]]
    has_failure_evidence: bool
    report: dict[str, Any] | None
    stages: Annotated[list[str], operator.add]
    result: dict[str, Any] | None


def build_rca_graph(reasoner: Reasoner = _default_reason) -> Any:
    async def resolve_asset(state: RCAState) -> dict[str, Any]:
        conn = state["conn"]
        mention = (state.get("asset_hint") or state.get("question") or "").strip()
        emit("resolve", f"Resolving asset '{mention}'")
        res = await resolve_mention(conn, mention)
        uid = res.asset.unified_id if res.asset else None
        if uid:
            emit("resolve", f"Resolved to canonical asset {uid}")
        else:
            emit("resolve", res.reason or "Asset not resolved")
        return {
            "stages": ["resolve_asset"],
            "resolution": res.model_dump(),
            "unified_id": uid,
        }

    async def gather_evidence(state: RCAState) -> dict[str, Any]:
        conn = state["conn"]
        uid = state["unified_id"]
        assert uid is not None
        chain = await _graph_store.rca(conn, asset_node(uid))
        facts = await asset_facts(conn, uid)
        neighborhood = await graph_neighborhood(conn, asset_node(uid))
        upstream = await _upstream_failures(conn, uid)
        has_failure = any(
            row["relation_type"] in _FAILURE_RELATIONS for row in chain
        ) or bool(upstream)
        emit(
            "gather",
            f"Gathering evidence: {len(chain)} causal-chain hop(s), "
            f"{len(neighborhood)} neighbor edge(s), "
            f"{len(upstream)} upstream-dependency failure(s)",
            failure_evidence=has_failure,
        )
        return {
            "stages": ["gather_evidence"],
            "chain": chain,
            "facts": facts.model_dump() if facts else None,
            "neighborhood": neighborhood,
            "upstream": upstream,
            "has_failure_evidence": has_failure,
        }

    async def reason(state: RCAState) -> dict[str, Any]:
        if not state.get("has_failure_evidence"):
            emit(
                "reason",
                "No failure or incident evidence linked to this asset in the index.",
            )
            report = {
                "summary": (
                    "No failure, incident, or violation evidence is linked to this "
                    "asset in the current index, so a root cause cannot be "
                    "determined from available evidence."
                ),
                "hypotheses": [],
                "contradictions": [],
                "unresolved": [
                    "No operational (failure/work-order) evidence found for this asset."
                ],
            }
            return {"stages": ["reason"], "report": report}
        emit("reason", "Scoring root-cause hypotheses against evidence")
        facts = state.get("facts") or {}
        failures: list[dict[str, Any]] = []
        maintenance: list[dict[str, Any]] = []
        for row in state.get("chain") or []:
            rel = row.get("relation_type")
            meta = row.get("metadata") or {}
            if rel == RelationType.HAS_FAILURE.value:
                failures.append(
                    {
                        "observation": meta.get("observation", ""),
                        "component": meta.get("component", ""),
                        "detail": meta.get("detail") or meta.get("observation", ""),
                        "citation": meta.get("citation", ""),
                    }
                )
            elif rel == RelationType.HAS_WORK_ORDER.value:
                maintenance.append(
                    {
                        "detail": meta.get("detail", ""),
                        "status": meta.get("status", ""),
                        "citation": meta.get("citation", ""),
                    }
                )
        evidence = {
            "asset": facts.get("asset_name") or state.get("unified_id"),
            "identifiers": facts.get("identifiers"),
            "observed_failures": failures,
            "maintenance_history": maintenance,
            # Failures on upstream assets this asset depends on (multi-hop cause).
            "upstream_failures": state.get("upstream") or [],
            # Full walk kept for any seeded/extracted failure->cause->rule links
            # (rendered when present) and for downstream/structural consumers.
            "causal_chain": state.get("chain") or [],
        }
        report = await reasoner(state.get("question", ""), evidence)
        # Calibrate confidence to the evidence actually gathered (grounding +
        # resolving-cause), so scores reflect certainty, not model eagerness.
        report = _calibrate_confidence(report, evidence)
        emit(
            "reason",
            f"Produced {len(report.get('hypotheses', []))} hypothesis(es)",
        )
        return {"stages": ["reason"], "report": report}

    async def assemble(state: RCAState) -> dict[str, Any]:
        uid = state.get("unified_id")
        if uid is None:
            result = {
                "resolved": False,
                "asset": None,
                "resolution": state.get("resolution"),
                "report": None,
                "evidence_from": "index",
                "message": (
                    state.get("resolution", {}).get("reason")
                    or "Could not resolve the asset from available evidence."
                ),
            }
        else:
            result = {
                "resolved": True,
                "asset": {"unified_id": uid, "facts": state.get("facts")},
                "report": state.get("report"),
                "causal_chain": state.get("chain"),
                "evidence_from": "index",
            }
        emit("complete", "Root-cause analysis complete", result=result)
        return {"stages": ["assemble"], "result": result}

    def route_after_resolve(state: RCAState) -> str:
        return "gather_evidence" if state.get("unified_id") else "assemble"

    g = StateGraph(RCAState)
    g.add_node("resolve_asset", resolve_asset)
    g.add_node("gather_evidence", gather_evidence)
    g.add_node("reason", reason)
    g.add_node("assemble", assemble)

    g.add_edge(START, "resolve_asset")
    g.add_conditional_edges(
        "resolve_asset", route_after_resolve, ["gather_evidence", "assemble"]
    )
    g.add_edge("gather_evidence", "reason")
    g.add_edge("reason", "assemble")
    g.add_edge("assemble", END)
    return g.compile()


_compiled = None


def get_rca_graph() -> Any:
    global _compiled
    if _compiled is None:
        _compiled = build_rca_graph()
    return _compiled


async def run_rca_events(
    question: str, asset_hint: str | None = None
) -> AsyncIterator[str]:
    """Run RCA, streaming per-stage status (SSE)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        state: RCAState = {
            "conn": conn,
            "question": question,
            "asset_hint": asset_hint,
            "stages": [],
        }
        yield sse({"step": "start", "message": "Starting root-cause analysis"})
        final: dict[str, Any] | None = None
        async for chunk in get_rca_graph().astream(state, stream_mode="custom"):
            if chunk.get("step") == "complete":
                final = chunk.get("result")
            yield sse(chunk)
        await save_workflow_run(conn, "rca", question, final, asset=asset_hint)
