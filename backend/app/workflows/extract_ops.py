"""Operational-graph extraction (the OPERATIONAL layer of the knowledge graph).

Records that come from operational sources — work orders, incident reports,
maintenance / inspection logs, compliance obligations (determined from the
connector's description, not hard-coded) — describe *events*, not just identity.
This module extracts those events and projects them as OPERATIONAL-layer edges
linked to the record's resolved canonical asset:

    asset -HAS_FAILURE->    fm:<observed problem>   (failures / incidents)
    asset -HAS_WORK_ORDER-> wo:<record id>          (work orders / inspections)
    asset -SUBJECT_TO->     rule:<rule id>          (compliance obligations)
    asset -HAS_SENSOR->     sensor:<name>           (signals named in the record)

Grounding rules (non-negotiable):
  * The extractor records only what the source OBSERVES — never a diagnosed root
    cause. Root-cause inference is the RCA reasoner's job; handing it the answer
    would defeat the point. The prompt forbids diagnosing.
  * Every edge carries the source record id as its citation.
  * Deterministic facts (dates, statuses) are pulled from fields with no LLM;
    only free-text understanding uses ONE structured LLM call per record, cached
    in `operational_extractions` so re-resolution never re-calls the model.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.config import get_settings
from app.engine.graph.model import (
    GraphLayer,
    RelationType,
    asset_node,
    failure_node,
    rule_node,
    sensor_node,
    work_order_node,
)
from app.services.llm import get_extract_model, json_response_format, resilient

from .resolve_mention import resolve_mention

# The connector's purpose (its description -> relation label) deterministically
# fixes what KIND of operational event its records are — far more reliable than
# asking the LLM to classify. The LLM only extracts the observed details.
RELATION_TO_KIND: dict[str, str] = {
    "Incident report": "failure",
    "Work order": "work_order",
    "Maintenance record": "work_order",
    "Inspection log": "inspection",
    "Compliance / permit": "rule",
}
OPERATIONAL_RELATIONS = set(RELATION_TO_KIND)

# The operational event kinds an edge can be projected as. Event kind is a
# property of the RECORD, not only the source: CSV connectors infer it from the
# connector's description (RELATION_TO_KIND); document records carry it per-record
# as `_event_kind` (set by doc_extract). This decouples extraction from a fixed
# per-connector vocabulary.
_ALLOWED_EVENT_KINDS = {"failure", "work_order", "inspection", "rule"}


def _norm_event_kind(value: Any) -> str | None:
    v = str(value or "").strip().lower()
    return v if v in _ALLOWED_EVENT_KINDS else None


class OpsExtraction(BaseModel):
    """What a single operational record OBSERVES. Never a diagnosis."""

    is_anomaly: bool = Field(
        default=True,
        description=(
            "true if the record describes an actual problem, anomaly, degradation, "
            "alarm, or failure; false if it is routine, expected, successful, "
            "planned, or within tolerance (e.g. a completed backup, a normal "
            "reading, scheduled maintenance, an all-clear)"
        ),
    )
    component: str = Field(default="", description="sub-component/system affected, if named")
    observation: str = Field(
        default="", description="short summary of the OBSERVED problem or activity, 3-8 words"
    )
    symptoms: list[str] = Field(
        default_factory=list,
        description="observed anomalies/measurements only — NEVER a diagnosed cause",
    )
    sensors: list[str] = Field(
        default_factory=list, description="sensor/signal names explicitly mentioned"
    )
    status: str = Field(default="", description="work-order status if present, e.g. completed/open")
    rule_id: str = Field(default="", description="rule/obligation id if this is a compliance record")
    confidence: float = Field(default=0.7, description="0..1 confidence in this extraction")


_SYSTEM = (
    "You extract structured facts from ONE maintenance/operations record. Report "
    "ONLY what the record explicitly states or observes: the component involved, a "
    "short observation summary, observed symptoms/measurements, any sensor names, a "
    "work-order status, and a rule id if present. "
    "Also judge `is_anomaly`: set it false when the record is routine, expected, "
    "successful, planned, or within tolerance (a completed backup, a normal "
    "reading, scheduled maintenance), and true when it reports a problem, "
    "degradation, alarm, or failure. "
    "CRITICAL: never infer, guess, or state a ROOT CAUSE or diagnosis, even if it "
    "seems obvious — that is done downstream. Symptoms are observations of what was "
    "measured or seen (e.g. a reading rose or fell, an alarm triggered), never a "
    "diagnosis of why (do NOT name a failed part or the reason for the event)."
)


def _slug(text: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return (s[:maxlen] or "event")


def _dates_from(raw: dict[str, Any]) -> dict[str, Any]:
    """Pull date/time-ish fields verbatim (deterministic — no LLM)."""
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        kl = k.lower()
        if v and any(t in kl for t in ("date", "time", "start", "end", "reported", "occurred")):
            out[k] = str(v)
    return out


# Lightweight, industry-agnostic anomaly signal (rule-based first, per the
# "rules + LLM judge" pattern). Returns True/False, or None when unclear (→ LLM).
_ANOMALY_RE = re.compile(
    r"\b(fail|failure|fault|faulty|leak|rupture|burst|crack|breach|alarm|error|"
    r"outage|down(time)?|breakdown|anomal|abnormal|spike|surge|timeout|overheat|"
    r"exceed|critical|severe|degrad|deteriorat|malfunction|loss|lost|collaps|drop|"
    r"stall|seiz|jam|oom|swapping|starv|corrupt|unrecover)\w*", re.I,
)
_BENIGN_RE = re.compile(
    r"(completed successfully|no action (required|needed)|within (tolerance|"
    r"normal|spec)|returned to (baseline|normal)|as expected|nominal|routine|"
    r"all[- ]?clear|no (issue|anomal|fault)|resolved|passed|ok\b|healthy)", re.I,
)


def _anomaly_signal(text: str) -> bool | None:
    t = text or ""
    t = re.sub(r"\bno\s+(failure|fault|error|alarm|leak|anomaly)\w*\b", "healthy", t, flags=re.I)
    t = re.sub(r"\b(failure|fault|error|alarm|leak|anomaly)\w*\s+(?:is\s+|was\s+)?resolved\b", "healthy", t, flags=re.I)
    if _ANOMALY_RE.search(t):
        return True
    if _BENIGN_RE.search(t):
        return False
    return None


_JSON_SHAPE = (
    '\n\nReturn ONLY a JSON object (no prose, no markdown fences): '
    '{"is_anomaly": true|false, "component": "sub-component if named", '
    '"observation": "3-8 word summary of what was observed", '
    '"symptoms": ["observed anomalies only"], "sensors": ["signal names"], '
    '"status": "work-order status if any", "rule_id": "rule id if any"}'
)


def _text_of(raw: Any) -> str:
    c = getattr(raw, "content", raw)
    return c if isinstance(c, str) else str(c)


def _reasoning_of(raw: Any) -> str:
    ak = getattr(raw, "additional_kwargs", {}) or {}
    return str(ak.get("reasoning_content") or ak.get("reasoning") or "")


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _sensors_from_raw(raw: dict[str, Any]) -> list[str]:
    v = raw.get("sensors")
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    if isinstance(v, list):
        return [str(s) for s in v]
    return []


async def _extract_one(text: str, raw: dict[str, Any]) -> dict[str, Any]:
    # Constrain output at the endpoint; retain raw metadata for validation.
    model = resilient(get_extract_model())
    human = (
        f"Record fields:\n{json.dumps(raw, indent=2, default=str)}\n\n"
        f"Record text:\n{text}{_JSON_SHAPE}"
    )
    out = await model.ainvoke(
        [SystemMessage(content="detailed thinking off\n" + _SYSTEM),
         HumanMessage(content=human)],
        response_format=json_response_format("OperationalExtraction", OpsExtraction.model_json_schema()),
    )
    if getattr(out, "response_metadata", {}).get("finish_reason") == "length":
        raise ValueError("Operational extraction was truncated")
    data = _extract_json(_text_of(out))
    if not data:
        raise ValueError("Operational extraction returned invalid JSON")
    return OpsExtraction.model_validate(data).model_dump()


async def _load_or_extract(
    conn: Any, record_id: str, text: str, raw: dict[str, Any]
) -> dict[str, Any]:
    """Cached extraction for a record; robust deterministic fallbacks so a flaky
    LLM response never blocks ingest. The anomaly signal is rule-first (see
    ``_anomaly_signal``) with the LLM's judgment only as a tie-breaker."""
    cached = await conn.fetchval(
        "select payload from operational_extractions where record_id = $1", record_id
    )
    if cached is not None:
        return json.loads(cached) if isinstance(cached, str) else cached

    # Document IE pre-supplies the extraction (`_ext`) so we don't spend a second
    # LLM call re-extracting what doc_extract already parsed from the passage.
    pre = raw.get("_ext")
    if isinstance(pre, dict):
        payload = {
            "is_anomaly": bool(pre.get("is_anomaly", True)),
            "component": str(pre.get("component") or raw.get("component") or "").strip(),
            "observation": str(pre.get("observation") or text[:60]).strip(),
            "symptoms": pre.get("symptoms") if isinstance(pre.get("symptoms"), list) else [],
            "sensors": pre.get("sensors") if isinstance(pre.get("sensors"), list) else [],
            "status": str(pre.get("status") or raw.get("status") or "").strip(),
            "rule_id": str(pre.get("rule_id") or raw.get("rule_id") or "").strip(),
            "confidence": pre.get("confidence", 0.6),
        }
        await conn.execute(
            "insert into operational_extractions (record_id, payload) values ($1, $2::jsonb) "
            "on conflict (record_id) do update set payload = excluded.payload",
            record_id, json.dumps(payload),
        )
        return payload

    try:
        data = await _extract_one(text, raw)
        extraction_error = None
    except Exception as exc:
        data = {}
        extraction_error = type(exc).__name__
    sig = _anomaly_signal(f"{data.get('observation', '')} {text}")
    payload = {
        "is_anomaly": sig if sig is not None else data.get("is_anomaly") is True,
        "component": str(data.get("component") or raw.get("component") or "").strip(),
        "observation": str(data.get("observation") or text[:60]).strip(),
        "symptoms": data.get("symptoms") if isinstance(data.get("symptoms"), list) else [],
        "sensors": data.get("sensors") if isinstance(data.get("sensors"), list) and data.get("sensors")
        else _sensors_from_raw(raw),
        "status": str(data.get("status") or raw.get("status") or ""),
        "rule_id": str(data.get("rule_id") or raw.get("rule_id") or ""),
        "confidence": data.get("confidence", 0.0),
        "extraction_mode": "model" if data else "deterministic_fallback",
        "extraction_error": extraction_error,
    }
    if data:
        await conn.execute(
            "insert into operational_extractions (record_id, payload) values ($1, $2::jsonb) "
            "on conflict (record_id) do update set payload = excluded.payload",
            record_id, json.dumps(payload),
        )
    return payload


async def rebuild_operational_edges(
    conn: Any,
    records: list[Any],
    member_to_cluster: dict[str, str],
    relation_by_system: dict[str, str],
    raw_by_record: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Delete + rebuild the operational-layer edges from cached/extracted events.

    Only records from operational-type connectors are considered. LLM extraction
    runs once per record (cached); edges are re-linked to the CURRENT asset id on
    every call so cluster renumbering never leaves dangling edges.
    """
    settings = get_settings()
    counts = {"failures": 0, "work_orders": 0, "rules": 0, "sensors": 0,
              "skipped": 0, "benign": 0, "dependencies": 0}

    # Rebuild is authoritative for the operational layer.
    await conn.execute("delete from edges where layer = 'operational'")


    seen: set[tuple[str, str, str]] = set()

    async def add_edge(src: str, rel: RelationType, tgt: str, meta: dict[str, Any]) -> None:
        key = (src, rel.value, tgt)
        if key in seen:
            return
        seen.add(key)
        await conn.execute(
            "insert into edges (source_id, relation_type, target_id, layer, metadata) "
            "values ($1,$2,$3,'operational',$4::jsonb)",
            src, rel.value, tgt, json.dumps(meta),
        )

    for rec in records:
        rid = rec.record_id
        system = rec.system
        raw = raw_by_record.get(rid, {})
        # Event kind: the record's own `_event_kind` wins (document IE, per-record).
        # For non-document sources, fall back to the connector-description mapping.
        # Document records NEVER use the connector fallback, so an identity-only
        # mention is not mislabeled as an event by the connector's type.
        kind = _norm_event_kind(raw.get("_event_kind"))
        if kind is None and raw.get("_source") != "document":
            kind = RELATION_TO_KIND.get(relation_by_system.get(system, ""))
        if kind is None:
            continue
        uid = member_to_cluster.get(rid)
        if uid is None:
            continue
        ext = await _load_or_extract(conn, rid, rec.semantic_text, raw)
        asset = asset_node(uid)
        dates = _dates_from(raw)
        base_meta = {
            "citation": rid,
            "component": ext.get("component", ""),
            "observation": ext.get("observation", ""),
            "symptoms": ext.get("symptoms", []),
            # The verbatim source text — so RCA reasons over real evidence, not
            # only the (thin) extraction.
            "detail": rec.semantic_text,
            "confidence": ext.get("confidence", 0.7),
            "extraction_mode": ext.get("extraction_mode", "cached"),
            "extraction_error": ext.get("extraction_error"),
            **dates,
        }

        if kind == "failure":
            # Noise gate: a record from an incident source that is actually routine
            # / within-tolerance (a completed backup, a normal reading) is NOT a
            # failure. Filtering noise before diagnosis keeps RCA grounded and
            # avoids false-positive failures — the key cross-industry robustness win.
            # is_anomaly already combines the deterministic signal + LLM tie-break.
            if not ext.get("is_anomaly", True):
                counts["benign"] += 1
            else:
                label = ext.get("observation") or ext.get("component") or "reported failure"
                await add_edge(asset, RelationType.HAS_FAILURE, failure_node(_slug(label)), base_meta)
                counts["failures"] += 1
        elif kind in ("work_order", "inspection"):
            status = (ext.get("status") or raw.get("status") or "").strip().lower()
            wo_meta = {**base_meta, "status": status, "rule_id": ext.get("rule_id") or raw.get("rule_id"), "period": raw.get("period")}
            await add_edge(asset, RelationType.HAS_WORK_ORDER, work_order_node(_slug(rid)), wo_meta)
            counts["work_orders"] += 1
        elif kind == "rule":
            rid_val = (ext.get("rule_id") or raw.get("rule_id") or _slug(ext.get("observation", ""))).strip()
            if rid_val:
                await add_edge(asset, RelationType.SUBJECT_TO, rule_node(rid_val), base_meta)
                counts["rules"] += 1

        for sensor in ext.get("sensors", []) or []:
            if sensor and sensor.strip():
                await add_edge(
                    asset, RelationType.HAS_SENSOR, sensor_node(_slug(sensor)),
                    {"citation": rid, "name": sensor},
                )
                counts["sensors"] += 1

    # Dependency edges (asset -> upstream asset) for multi-hop RCA. Any record —
    # of any connector kind — may declare a dependency; resolve the reference to
    # a canonical asset and link the two assets. This is how a symptom on one
    # asset can be traced to a failure on the upstream asset it relies on.
    for rec in records:
        raw = raw_by_record.get(rec.record_id, {})
        dep = raw.get("depends_on") or raw.get("upstream")
        if not dep:
            continue
        src_uid = member_to_cluster.get(rec.record_id)
        if src_uid is None:
            continue
        try:
            res = await resolve_mention(conn, str(dep))
        except Exception:
            res = None
        tgt_uid = res.asset.unified_id if (res and res.asset) else None
        if tgt_uid and tgt_uid != src_uid:
            await add_edge(
                asset_node(src_uid), RelationType.DEPENDS_ON, asset_node(tgt_uid),
                {"citation": rec.record_id, "via": str(dep)},
            )
            counts["dependencies"] += 1

    return counts
