"""Layer 2 — Schema Discovery.

Given ~100 sample records from a newly connected source system, decide which
fields are usable identity anchors. Three independent signals feed a voting
rule (doc Section 3):

  Signal 1 - Pattern:     do the values match a known identifier structure?
  Signal 2 - Cardinality: are the values unique enough to identify a thing?
  Signal 3 - Semantic:    does the field NAME mean an identity concept?

A field is a proposed anchor only if:
  (pattern_ratio>=0.80 AND pattern_weight>=0.85 AND cardinality>=0.80)          -> anchor
  OR (semantic>=0.80 AND pattern_ratio>=0.80 AND semantic concept is an anchor) -> anchor
  OR (pattern_ratio>=0.80 AND cardinality>=0.80 AND semantic<0.50               -> probable, needs human
      AND detected concept is a plausible identifier)
Otherwise it is descriptive / transactional / internal and never used to match.

High cardinality ALONE never makes an anchor — it must pass pattern matching
too (the equnr trap: sequential row ids are not physical identifiers).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.knowledge.identity import (
    CanonicalConcept,
    is_anchor_eligible,
    weight_of,
)
from app.knowledge.industry import IndustryDetection, detect_industry
from app.knowledge.patterns import looks_sequential, patterns_for_industry
from app.knowledge.semantics import semantic_match

PATTERN_MATCH_THRESHOLD = 0.80
CARDINALITY_THRESHOLD = 0.80
SEMANTIC_ANCHOR_THRESHOLD = 0.80
SEMANTIC_LOW_THRESHOLD = 0.50
PROBABLE_MIN_WEIGHT = 0.40
SAMPLE_SIZE = 100

# Concepts that are explicitly NOT identity anchors (descriptive/transactional).
_NON_ANCHOR_CONCEPTS = {
    CanonicalConcept.WORK_ORDER,
    CanonicalConcept.SYSTEM_INTERNAL_ID,
    CanonicalConcept.TIMESTAMP,
    CanonicalConcept.MODEL,
    CanonicalConcept.LOCATION,
    CanonicalConcept.IP_ADDRESS,
    CanonicalConcept.NUMERIC_ID,
}


class FieldRole(str, Enum):
    ANCHOR = "anchor"
    PROBABLE_ANCHOR = "probable_anchor"  # requires human confirmation
    DESCRIPTIVE = "descriptive"
    TRANSACTIONAL = "transactional"
    INTERNAL_ID = "internal_id"


class FieldClassification(BaseModel):
    field_name: str
    detected_concept: CanonicalConcept
    role: FieldRole
    confidence: float
    # signal detail (auditable)
    pattern_concept: CanonicalConcept
    pattern_match_ratio: float
    pattern_weight: float
    cardinality_ratio: float
    max_frequency: float
    semantic_concept: CanonicalConcept
    semantic_score: float

    @property
    def is_anchor(self) -> bool:
        return self.role == FieldRole.ANCHOR

    @property
    def needs_human(self) -> bool:
        return self.role == FieldRole.PROBABLE_ANCHOR


class SchemaDiscoveryResult(BaseModel):
    industry: str
    industry_confidence: float
    fields: list[FieldClassification] = Field(default_factory=list)

    @property
    def anchors(self) -> list[FieldClassification]:
        return [f for f in self.fields if f.role == FieldRole.ANCHOR]

    @property
    def review_items(self) -> list[FieldClassification]:
        return [f for f in self.fields if f.role == FieldRole.PROBABLE_ANCHOR]


def _sample_values(records: list[dict[str, object]], field: str) -> list[str]:
    out: list[str] = []
    for rec in records:
        v = rec.get(field)
        if v is not None and str(v).strip() != "":
            out.append(str(v).strip())
        if len(out) >= SAMPLE_SIZE:
            break
    return out


def _pattern_analysis(
    values: list[str], industry: str
) -> tuple[CanonicalConcept, float, float]:
    """Return (concept, match_ratio, weight) using first-match precedence.

    Patterns are ordered specific -> generic, so the first pattern that clears
    the match threshold wins (work_order beats generic alphanumeric, etc.).
    Sequential pure-numeric values are reclassified as system_internal_id.
    """
    if not values:
        return CanonicalConcept.UNKNOWN, 0.0, 0.0

    # Sequential pure-numeric values are row ids, not physical identifiers.
    # Check first so a barcode/numeric pattern can't swallow them (equnr trap).
    if looks_sequential(values):
        c = CanonicalConcept.SYSTEM_INTERNAL_ID
        return c, 1.0, weight_of(c)

    best: tuple[CanonicalConcept, float] | None = None
    for pat in patterns_for_industry(industry):
        hits = sum(1 for v in values if pat.matches(v))
        ratio = hits / len(values)
        if ratio >= PATTERN_MATCH_THRESHOLD:
            best = (pat.concept, ratio)
            break
        if best is None or ratio > best[1]:
            # remember best-effort even if below threshold
            best = (pat.concept, ratio)

    if best is None:
        return CanonicalConcept.UNKNOWN, 0.0, 0.0

    concept, ratio = best
    # Sequential numeric -> row id, not a physical identifier.
    if concept == CanonicalConcept.NUMERIC_ID and looks_sequential(values):
        concept = CanonicalConcept.SYSTEM_INTERNAL_ID
    return concept, round(ratio, 3), weight_of(concept)


def _classify_field(
    field_name: str, values: list[str], industry: str
) -> FieldClassification:
    pattern_concept, pattern_ratio, pattern_weight = _pattern_analysis(values, industry)

    unique = len(set(values))
    total = len(values)
    cardinality = round(unique / total, 3) if total else 0.0
    if total:
        counts: dict[str, int] = {}
        for v in values:
            counts[v] = counts.get(v, 0) + 1
        max_frequency = round(max(counts.values()) / total, 3)
    else:
        max_frequency = 0.0

    semantic_concept, semantic_score = semantic_match(field_name)

    pattern_ok = pattern_ratio >= PATTERN_MATCH_THRESHOLD
    cardinality_ok = cardinality >= CARDINALITY_THRESHOLD

    # --- voting ---
    role = FieldRole.DESCRIPTIVE
    detected = pattern_concept
    confidence = 0.0

    # Rule 1: strong pattern + unique + anchor-weight -> confident anchor.
    rule1 = pattern_ok and pattern_weight >= 0.85 and cardinality_ok
    # Rule 2: field NAME clearly means an anchor concept, corroborated by pattern.
    rule2 = (
        semantic_score >= SEMANTIC_ANCHOR_THRESHOLD
        and pattern_ok
        and cardinality_ok
        and pattern_concept == semantic_concept
        and is_anchor_eligible(semantic_concept)
    )
    # Rule 3 (generalized from the doc): a plausible but weak/generic identifier
    # that is unique and NOT clearly a descriptive concept -> propose to a human.
    # (The doc's literal "semantic < 0.50" is one instance of this; generalizing
    # avoids brittleness to exact similarity scores.)
    plausible = (
        pattern_weight >= PROBABLE_MIN_WEIGHT
        and pattern_concept not in _NON_ANCHOR_CONCEPTS
    )
    confidently_descriptive = (
        semantic_score >= SEMANTIC_ANCHOR_THRESHOLD
        and semantic_concept in _NON_ANCHOR_CONCEPTS
    )
    rule3 = pattern_ok and cardinality_ok and plausible and not confidently_descriptive

    if rule1:
        role, detected, confidence = FieldRole.ANCHOR, pattern_concept, pattern_weight
    elif rule2:
        role, detected, confidence = FieldRole.ANCHOR, semantic_concept, semantic_score
    elif rule3:
        # Prefer a plausible weak-identifier semantic label (e.g. asset_tag)
        # over the bare generic pattern concept when the name supports it.
        if (
            weight_of(semantic_concept) >= PROBABLE_MIN_WEIGHT
            and semantic_concept not in _NON_ANCHOR_CONCEPTS
            and semantic_score >= 0.60
        ):
            detected = semantic_concept
        else:
            detected = pattern_concept
        role, confidence = FieldRole.PROBABLE_ANCHOR, pattern_weight
    else:
        # descriptive / transactional / internal — prefer a strong semantic
        # descriptive label when the pattern only found a generic id.
        if (
            pattern_concept in (CanonicalConcept.ALPHANUMERIC_ID, CanonicalConcept.NUMERIC_ID)
            and semantic_score >= 0.60
            and semantic_concept != CanonicalConcept.UNKNOWN
        ):
            detected = semantic_concept
        confidence = max(semantic_score, pattern_weight)
        role = _descriptive_role(detected)

    return FieldClassification(
        field_name=field_name,
        detected_concept=detected,
        role=role,
        confidence=round(confidence, 3),
        pattern_concept=pattern_concept,
        pattern_match_ratio=pattern_ratio,
        pattern_weight=pattern_weight,
        cardinality_ratio=cardinality,
        max_frequency=max_frequency,
        semantic_concept=semantic_concept,
        semantic_score=semantic_score,
    )


def _descriptive_role(concept: CanonicalConcept) -> FieldRole:
    if concept == CanonicalConcept.WORK_ORDER:
        return FieldRole.TRANSACTIONAL
    if concept == CanonicalConcept.SYSTEM_INTERNAL_ID:
        return FieldRole.INTERNAL_ID
    return FieldRole.DESCRIPTIVE


def discover_schema(records: list[dict[str, object]]) -> SchemaDiscoveryResult:
    """Analyze sample records: detect industry, then classify every field."""
    detection: IndustryDetection = detect_industry(records)

    field_names: list[str] = []
    seen: set[str] = set()
    for rec in records:
        for k in rec.keys():
            # Underscore-prefixed keys are ingest metadata (provenance, event
            # kind, pre-computed extraction) — never content/anchors.
            if str(k).startswith("_"):
                continue
            if k not in seen:
                seen.add(k)
                field_names.append(str(k))

    fields = [
        _classify_field(name, _sample_values(records, name), detection.industry)
        for name in field_names
    ]
    return SchemaDiscoveryResult(
        industry=detection.industry,
        industry_confidence=detection.confidence,
        fields=fields,
    )
