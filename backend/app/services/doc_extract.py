"""Generic document information-extraction.

Turns a parsed PDF (tables + narrative chunks) into flat "mention" records that
feed the EXISTING resolution pipeline unchanged. Nothing here is tuned to a
specific document or industry:

  * Tables -> one record per row (no LLM; schema discovery handles them like any
    tabular source).
  * Narrative -> one budget-capped, domain-neutral LLM call per chunk that names
    the ASSET(S) the passage is about, any identifier-looking strings, and any
    OPERATIONAL EVENT (failure / rule / work-order / inspection). Identifier
    strings are validated by patterns.py, so only real structured IDs become
    anchors — never a hallucinated label.

Each emitted record carries provenance (`citation`, `_page`, `_section`) and,
for event records, a per-record `_event_kind` + a pre-computed `_ext` payload so
`extract_ops` links the edge WITHOUT a second LLM call. Meta keys are
underscore-prefixed and ignored by schema discovery.

Results are cached per content hash (`doc_extractions`) so reprocessing a
document never re-calls the model.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.knowledge.identity import CanonicalConcept, weight_of
from app.knowledge.patterns import ALL_PATTERNS
from app.services.llm import get_extract_model, resilient

from .documents import ParsedDocument

# Event kinds we project onto the operational graph (domain-neutral).
_OP_KINDS = {"failure", "rule", "work_order", "inspection"}

# Identifier concepts too generic to anchor on (mirror pipeline._UNTRUSTED_CONCEPTS).
_UNTRUSTED = {
    CanonicalConcept.ALPHANUMERIC_ID.value,
    CanonicalConcept.NUMERIC_ID.value,
    CanonicalConcept.SYSTEM_INTERNAL_ID.value,
    CanonicalConcept.UNKNOWN.value,
}

# Budget guard: cap narrative LLM calls per document (rate budget is 35 rpm
# shared). Chunks beyond this are skipped (tables are always kept).
_MAX_CHUNKS = 24
_MIN_CHUNK_CHARS = 60


def _ident_concept(value: str) -> str | None:
    """Validate an identifier-looking string with the shared pattern library.

    Returns the canonical concept name iff the value matches a *meaningful*
    identifier structure (not a generic alphanumeric/numeric fallback). This is
    what keeps document-extracted anchors grounded.
    """
    v = (value or "").strip()
    if len(v) < 3 or len(v) > 64:
        return None
    for pat in ALL_PATTERNS:
        if pat.matches(v):
            concept = pat.concept.value
            if concept in _UNTRUSTED or weight_of(pat.concept) <= 0:
                return None
            return concept
    return None


def _records_from_tables(parsed: ParsedDocument) -> list[dict[str, Any]]:
    """One record per table row. Columns become fields; identifier-looking cell
    values are surfaced under their validated concept so discovery anchors them."""
    out: list[dict[str, Any]] = []
    for ti, tbl in enumerate(parsed.tables):
        for ri, row in enumerate(tbl.rows):
            rid = f"{parsed.filename}#p{tbl.page}#t{ti}#r{ri}"
            rec: dict[str, Any] = {"record_id": rid}
            # keep original columns as descriptive content
            for k, val in row.items():
                if val is None or str(val).strip() == "":
                    continue
                rec[str(k)] = str(val)
                concept = _ident_concept(str(val))
                if concept and concept not in rec and raw_id.strip().casefold() in chunk.text.casefold():
                    rec[concept] = str(val).strip()
            rec["_citation"] = rid
            rec["_page"] = tbl.page
            rec["_section"] = tbl.section or ""
            rec["_source"] = "document"
            out.append(rec)
    return out


_SYSTEM = (
    "You extract structured facts from ONE passage of an operations, "
    "engineering, incident, or regulatory document. Identify the physical or "
    "logical ASSETS the passage is about — equipment, machines, vehicles, "
    "facilities, plants, units, systems, components, or infrastructure — and any "
    "OPERATIONAL EVENT the passage reports about them.\n"
    "For each asset mention, capture:\n"
    "  - asset: a short natural name for the thing (as written)\n"
    "  - identifiers: any tag/serial/model/id-LIKE strings shown for it (verbatim)\n"
    "  - kind: 'failure' if the passage reports a fault/incident/damage/anomaly; "
    "'rule' if it states a regulation/obligation/requirement/permit; 'work_order' "
    "for maintenance/repair/work performed; 'inspection' for an inspection/audit; "
    "'none' if the passage only identifies or describes the asset.\n"
    "  - detail: ONE sentence copied/condensed from the passage (what happened or "
    "what is required)\n"
    "  - is_anomaly: true only if something is wrong/degraded/failed/out-of-spec\n"
    "  - component, symptoms (observations only), sensors, rule_id, status: if present\n"
    "STRICT: report only what the passage states. NEVER diagnose or infer a ROOT "
    "CAUSE. Ignore people, pure dates, citations, and generic prose that names no "
    "asset. If the passage names no asset, return an empty list."
)

_JSON_SHAPE = (
    '\n\nReturn ONLY JSON (no prose, no fences): '
    '{"mentions":[{"asset":"name","identifiers":["..."],'
    '"kind":"failure|rule|work_order|inspection|none","detail":"one sentence",'
    '"is_anomaly":true,"component":"","symptoms":["..."],"sensors":["..."],'
    '"rule_id":"","status":""}]}'
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


def _as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [s.strip() for s in v.split(",") if s.strip()]
    return []


async def _ie_chunk(model: Any, chunk_text: str) -> list[dict[str, Any]]:
    human = f"Passage:\n{chunk_text}{_JSON_SHAPE}"
    try:
        out = await model.ainvoke(
            [
                SystemMessage(content="detailed thinking off\n" + _SYSTEM),
                HumanMessage(content=human),
            ]
        )
    except Exception:
        return []
    data = _extract_json(_text_of(out)) or _extract_json(_reasoning_of(out))
    if not data:
        return []
    mentions = data.get("mentions")
    return mentions if isinstance(mentions, list) else []


def _mention_to_record(
    parsed: ParsedDocument, chunk, mi: int, m: dict[str, Any]
) -> dict[str, Any] | None:
    asset = str(m.get("asset") or "").strip()
    if not asset:
        return None
    rid = f"{parsed.filename}#p{chunk.page}#c{chunk.index}#{mi}"
    detail = str(m.get("detail") or "").strip()
    rec: dict[str, Any] = {
        "record_id": rid,
        "name": asset,
        "description": detail or asset,
        "_citation": rid,
        "_page": chunk.page,
        "_section": chunk.section or "",
        "_source": "document",
    }
    # validated identifiers -> concept-named fields (discovery will anchor them)
    for raw_id in _as_list(m.get("identifiers")):
        concept = _ident_concept(raw_id)
        if concept and concept not in rec and raw_id.strip().casefold() in chunk.text.casefold():
            rec[concept] = raw_id.strip()

    kind = str(m.get("kind") or "none").strip().lower()
    if kind in _OP_KINDS:
        rec["_event_kind"] = kind
        # Pre-computed operational extraction so extract_ops needs no 2nd LLM call.
        rec["_ext"] = {
            "is_anomaly": m.get("is_anomaly", kind == "failure") is True,
            "component": str(m.get("component") or "").strip(),
            "observation": (detail[:60] or asset),
            "symptoms": _as_list(m.get("symptoms")),
            "sensors": _as_list(m.get("sensors")),
            "status": str(m.get("status") or "").strip(),
            "rule_id": str(m.get("rule_id") or "").strip(),
            "confidence": 0.6,  # narrative extraction is less certain than a form
        }
    return rec


def _prioritize(chunks: list, limit: int) -> list:
    """Sample evenly across the document when narrative exceeds the request budget."""
    usable = [c for c in chunks if len(c.text) >= _MIN_CHUNK_CHARS]
    if len(usable) <= limit:
        return usable
    return [usable[round(i * (len(usable) - 1) / (limit - 1))] for i in range(limit)] if limit > 1 else usable[:1]


async def extract_document_records(
    parsed: ParsedDocument, *, max_chunks: int = _MAX_CHUNKS
) -> list[dict[str, Any]]:
    """Extract asset/event mention records from a parsed document.

    Tables are always included (free); narrative chunks are IE'd up to
    `max_chunks` (rate-budget guard). Returns flat record dicts ready for
    `pipeline.ingest_records`.
    """
    records: list[dict[str, Any]] = _records_from_tables(parsed)

    chunks = _prioritize(parsed.chunks, max_chunks)
    if chunks:
        model = resilient(get_extract_model())
        for chunk in chunks:
            mentions = await _ie_chunk(model, chunk.text)
            for mi, m in enumerate(mentions):
                if not isinstance(m, dict):
                    continue
                rec = _mention_to_record(parsed, chunk, mi, m)
                if rec is not None:
                    records.append(rec)
    return records


_DOCTYPE_SYSTEM = (
    "Classify this document into ONE short lowercase snake_case type based on its "
    "title and opening text. Examples of the KIND of label (not an exhaustive "
    "list): incident_report, regulation, settlement, inspection_report, "
    "scientific_article, asset_registry, maintenance_log, press_release. Answer "
    "with ONLY the label."
)


async def classify_doc_type(parsed: ParsedDocument) -> str:
    """One cheap LLM call to label the document (editable metadata, not logic)."""
    sample = parsed.markdown[:1500] if parsed.markdown else " ".join(
        c.text for c in parsed.chunks[:2]
    )
    prompt = f"Title: {parsed.title}\n\nText:\n{sample}"
    try:
        model = resilient(get_extract_model())
        out = await model.ainvoke(
            [
                SystemMessage(content="detailed thinking off\n" + _DOCTYPE_SYSTEM),
                HumanMessage(content=prompt),
            ]
        )
        label = _text_of(out).strip().splitlines()[0] if _text_of(out).strip() else ""
        label = re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_")
        return label[:40] or "document"
    except Exception:
        return "document"
