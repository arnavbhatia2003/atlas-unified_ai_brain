"""Data structures for entity resolution."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.knowledge.identity import CanonicalConcept


class SourceRecord(BaseModel):
    """A raw record from a source system, normalized into typed identifiers.

    `identifiers` maps a canonical concept value (e.g. "serial_number") to the
    list of values that record carries for it. Multi-valued IDs are supported
    (one serial, N MACs, N tags). `semantic_text` is the text used for embedding
    similarity when no anchors are shared.
    """

    record_id: str
    system: str
    identifiers: dict[str, list[str]] = Field(default_factory=dict)
    semantic_text: str = ""
    entity_kind: str = "unspecified"
    identity_namespace: str = ""


class EvidenceKind(str, Enum):
    EXACT_ANCHOR = "exact_anchor"   # shared identifier value
    EMBEDDING = "embedding"         # semantic similarity


class Evidence(BaseModel):
    """One piece of match evidence between two records."""

    kind: EvidenceKind
    concept: CanonicalConcept | None = None
    weight: float                    # identity weight (exact) or similarity (embedding)
    detail: str = ""
    polarity: str = "supports"       # supports | contradicts (contradiction handled at cluster level)


class ExplainableScore(BaseModel):
    """A confidence score plus the breakdown that produced it (never a bare number)."""

    score: float
    base: float
    redundancy_bonus: float
    conflict_penalty: float
    evidence: list[Evidence] = Field(default_factory=list)
    rationale: str = ""


class MergeDecision(str, Enum):
    AUTO_MERGED = "auto_merged"
    SUGGESTED = "suggested"      # needs admin confirmation
    REVIEW = "review"            # needs investigation
    SINGLETON = "singleton"


class Cluster(BaseModel):
    """A unified asset: the set of source records resolved to one physical thing."""

    cluster_id: str
    members: list[str] = Field(default_factory=list)
    identifiers: dict[str, list[str]] = Field(default_factory=dict)  # merged, multi-valued
    decision: MergeDecision
    confidence: float
    methods: list[str] = Field(default_factory=list)  # anchor / embedding


class ReviewItem(BaseModel):
    """Something a human must confirm or resolve."""

    kind: str  # bridge_conflict | suggest_merge | review
    records: list[str] = Field(default_factory=list)
    reason: str = ""
    detail: dict[str, object] = Field(default_factory=dict)


class ResolutionResult(BaseModel):
    clusters: list[Cluster] = Field(default_factory=list)
    review_items: list[ReviewItem] = Field(default_factory=list)

    def cluster_of(self, record_id: str) -> Cluster | None:
        for c in self.clusters:
            if record_id in c.members:
                return c
        return None
