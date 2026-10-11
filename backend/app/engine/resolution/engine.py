"""The entity-resolution pipeline (doc Section 5).

Candidate generation -> evidence extraction -> conflict-aware clustering ->
decision bands.

Clustering is Kruskal-style: candidate edges are processed strongest-first and
merged with union-find, BUT before each merge we check whether joining the two
clusters would create an identifier contradiction (e.g. two different serials in
one cluster). If it would, the edge is penalized; if the penalized confidence
falls below the merge floor the edge is blocked and the bridging pair is flagged
for human review instead of silently merging (the transitive-conflict guard).
"""

from __future__ import annotations

import hashlib
import json

from app.knowledge.identity import CanonicalConcept, weight_of

from .confidence import ConfidenceEngine, WeightedConfidenceEngine
from .extractors import SimilarityFn, pairwise_evidence
from .models import (
    Cluster,
    Evidence,
    EvidenceKind,
    MergeDecision,
    ResolutionResult,
    ReviewItem,
    SourceRecord,
)
from .thresholds import (
    AUTO_MERGE,
    HIGH_CONFLICT_WEIGHT,
    PENALTY_HIGH,
    PENALTY_MEDIUM,
    PENALTY_NONE,
    REVIEW_FLOOR,
    SUGGEST,
)


class _Component:
    __slots__ = ("members", "values", "confs", "methods", "kinds", "namespaces")

    def __init__(self, record: SourceRecord) -> None:
        self.members: set[str] = {record.record_id}
        self.values: dict[str, set[str]] = {
            c: {v.strip().lower() for v in vals if v.strip()}
            for c, vals in record.identifiers.items()
        }
        self.confs: list[float] = []
        self.methods: set[str] = set()
        self.kinds = {record.entity_kind} if record.entity_kind != "unspecified" else set()
        self.namespaces = {record.identity_namespace} if record.identity_namespace else set()


def _conflict_penalty(
    a: dict[str, set[str]], b: dict[str, set[str]]
) -> tuple[float, str | None]:
    """Penalty for merging two clusters, by the heaviest contradicting concept.

    A contradiction is two non-empty, disjoint value sets for the same identifier
    concept. High-weight (>0.85) contradictions penalize hard; medium ones softly.
    """
    worst = PENALTY_NONE
    worst_concept: str | None = None
    for concept in set(a) | set(b):
        va, vb = a.get(concept, set()), b.get(concept, set())
        if not va or not vb or not va.isdisjoint(vb):
            continue
        try:
            w = weight_of(CanonicalConcept(concept))
        except ValueError:
            continue
        if w <= 0:
            continue
        pen = PENALTY_HIGH if w > HIGH_CONFLICT_WEIGHT else PENALTY_MEDIUM
        if pen < worst:
            worst, worst_concept = pen, concept
    return worst, worst_concept


def _methods(evidence: list[Evidence]) -> set[str]:
    m: set[str] = set()
    for e in evidence:
        m.add("anchor" if e.kind == EvidenceKind.EXACT_ANCHOR else "embedding")
    return m


def resolve(
    records: list[SourceRecord],
    similarity: SimilarityFn | None = None,
    engine: ConfidenceEngine | None = None,
    forced_merge: set[frozenset[str]] | None = None,
    forced_separate: set[frozenset[str]] | None = None,
) -> ResolutionResult:
    engine = engine or WeightedConfidenceEngine()
    forced_merge = forced_merge or set()
    forced_separate = forced_separate or set()

    # --- candidate edges (keep only pairs that could plausibly merge) ---
    edges: list[tuple[float, str, str, list[Evidence]]] = []
    for i, a in enumerate(records):
        for b in records[i + 1:]:
            ev = pairwise_evidence(a, b, similarity)
            if not ev:
                continue
            base = engine.score(ev).score  # unpenalized ceiling
            if base >= REVIEW_FLOOR:
                edges.append((base, a.record_id, b.record_id, ev))
    edges.sort(key=lambda e: e[0], reverse=True)

    # --- conflict-aware union-find ---
    comp: dict[str, _Component] = {r.record_id: _Component(r) for r in records}
    parent: dict[str, str] = {r.record_id: r.record_id for r in records}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    review: list[ReviewItem] = []

    def _merge(root_a: str, root_b: str, conf: float, methods: set[str]) -> None:
        ca, cb = comp[root_a], comp[root_b]
        if any(frozenset((a, b)) in forced_separate for a in ca.members for b in cb.members):
            return
        ca.members |= cb.members
        for concept, vals in cb.values.items():
            ca.values.setdefault(concept, set()).update(vals)
        ca.confs.append(conf)
        ca.confs.extend(cb.confs)
        ca.methods |= cb.methods | methods
        ca.kinds |= cb.kinds
        ca.namespaces |= cb.namespaces
        parent[root_b] = root_a

    # Human overrides first: forced merges join regardless of conflict (the human
    # accepted the merge), so they are never re-flagged.
    for pair in forced_merge:
        a, b = sorted(pair)
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                _merge(ra, rb, 1.0, {"manual"})

    for base, ra, rb, ev in edges:
        if frozenset((ra, rb)) in forced_separate:
            continue  # human said these are different assets — never merge, never flag
        root_a, root_b = find(ra), find(rb)
        if root_a == root_b:
            continue
        ca, cb = comp[root_a], comp[root_b]
        if len(ca.kinds | cb.kinds) > 1 or len(ca.namespaces | cb.namespaces) > 1:
            review.append(ReviewItem(kind="identity_scope", records=sorted([ra, rb]), reason="Shared evidence spans different entity kinds or identity namespaces; automatic identity merge blocked."))
            continue
        penalty, conflict_concept = _conflict_penalty(ca.values, cb.values)
        conf = engine.score(ev, penalty).score

        if conf >= REVIEW_FLOOR:
            _merge(root_a, root_b, conf, _methods(ev))
        else:
            # Blocked by an identifier conflict. Only surface it for human review
            # when the two records ACTUALLY share a strong identifier (a genuine
            # bridge/transitive conflict). Records that merely LOOK similar
            # (embedding-only) but carry different strong IDs are simply different
            # assets — flagging them would flood the queue with noise.
            has_anchor = any(e.kind == EvidenceKind.EXACT_ANCHOR for e in ev)
            if not has_anchor:
                continue
            va = sorted(ca.values.get(conflict_concept or "", set()))
            vb = sorted(cb.values.get(conflict_concept or "", set()))
            review.append(
                ReviewItem(
                    kind="bridge_conflict",
                    records=sorted([ra, rb]),
                    reason=(
                        f"Records share {ev[0].detail} but also carry conflicting "
                        f"{conflict_concept} values {va} vs {vb}. A human must decide "
                        f"if these are the same asset."
                    ),
                    detail={
                        "conflict_concept": conflict_concept,
                        "values": {ra: va or vb, rb: vb or va},
                        "penalized_confidence": round(conf, 4),
                    },
                )
            )

    # --- build clusters + decisions ---
    roots: dict[str, list[str]] = {}
    for rid in parent:
        roots.setdefault(find(rid), []).append(rid)

    clusters: list[Cluster] = []
    for idx, (root, members) in enumerate(
        sorted(roots.items(), key=lambda kv: min(kv[1])), start=1
    ):
        c = comp[root]
        identifiers = {k: sorted(v) for k, v in c.values.items() if v}
        if len(members) == 1:
            decision, confidence = MergeDecision.SINGLETON, 0.0
        else:
            max_conf = min(c.confs) if c.confs else 0.0
            if max_conf >= AUTO_MERGE:
                decision = MergeDecision.AUTO_MERGED
            elif max_conf >= SUGGEST:
                decision = MergeDecision.SUGGESTED
            else:
                decision = MergeDecision.REVIEW
            confidence = round(max_conf, 4)
        cluster = Cluster(
            cluster_id="ua-" + hashlib.sha256(json.dumps(sorted(members)).encode()).hexdigest()[:16],
            members=sorted(members),
            identifiers=identifiers,
            decision=decision,
            confidence=confidence,
            methods=sorted(c.methods),
        )
        clusters.append(cluster)
        if decision == MergeDecision.SUGGESTED:
            review.append(ReviewItem(
                kind="suggest_merge", records=cluster.members,
                reason=(
                    f"These {len(cluster.members)} records likely describe the same "
                    f"asset (confidence {confidence:.2f}). Confirm the merge, or keep "
                    f"them separate."
                ),
                detail={"confidence": confidence, "methods": sorted(c.methods)},
            ))
        elif decision == MergeDecision.REVIEW:
            review.append(ReviewItem(
                kind="review", records=cluster.members,
                reason=(
                    f"Low-confidence match (confidence {confidence:.2f}): review "
                    f"whether these {len(cluster.members)} records are the same asset."
                ),
                detail={"confidence": confidence, "methods": sorted(c.methods)},
            ))

    return ResolutionResult(clusters=clusters, review_items=review)
