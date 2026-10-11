"""Offline counterexamples against the audited implementation; no DB or vendor calls.

Run from the repository: backend/.venv/Scripts/python docs/atlas-audit-probes.py
These assertions document current defects, not desired behavior or benchmark quality.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.engine.resolution.engine import resolve
from app.engine.resolution.models import SourceRecord
from app.engine.schema_discovery import _classify_field, _pattern_analysis
from app.knowledge.identity import CanonicalConcept as C
from app.services.embeddings import EmbeddingClient
from app.workflows.extract_ops import _anomaly_signal
from app.workflows.rca import _calibrate_confidence

results = []


def record(rid, serial=None):
    return SourceRecord(record_id=rid, system="synthetic", semantic_text="pump",
                        identifiers={"serial_number": [serial]} if serial else {})


def note(id, observed, expected):
    results.append({"id": id, "status": "reproduced", "observed": observed,
                    "required_behavior": expected})


r = resolve([record("a"), record("b")], similarity=lambda a, b: .7)
assert len(r.clusters) == 1 and r.clusters[0].decision.value == "review"
note("G02", {"members": r.clusters[0].members, "decision": "review"},
     "Keep proposed membership outside the published identity graph until approved.")

r = resolve([record("a", "SN-12345"), record("b", "SN-12345"), record("c")],
            similarity=lambda a, b: .7 if "c" in (a.record_id, b.record_id) else 0)
assert len(r.clusters) == 1 and r.clusters[0].decision.value == "auto_merged"
assert not r.review_items
note("G03", {"members": r.clusters[0].members, "decision": "auto_merged",
             "confidence": r.clusters[0].confidence, "review_items": 0},
     "A strong existing pair must not promote a weak new member to automatic acceptance.")

r = resolve([record(x, "SN-12345") for x in "abc"],
            forced_separate={frozenset(("a", "b"))})
assert r.cluster_of("a") is r.cluster_of("b")
note("G04", {"a_and_b_same_cluster": True},
     "Cannot-link must apply to every member across proposed clusters.")

before = resolve([record("b"), record("c")]).cluster_of("b").cluster_id
after = resolve([record("a"), record("b"), record("c")]).cluster_of("b").cluster_id
assert before != after
note("G05", {"before": before, "after_unrelated_insert": after},
     "Unrelated inserts must not renumber existing entity identities.")

f = _classify_field("serial_number", ["1", "2", "3", "4"], "unknown")
assert f.role.value == "anchor" and f.pattern_concept == C.SYSTEM_INTERNAL_ID
note("G08", {"role": f.role.value, "concept": f.detected_concept.value,
             "pattern_concept": f.pattern_concept.value},
     "Reject conflicting pattern/name semantics or require approved source mapping.")

patterns = [SimpleNamespace(concept=C.UUID, matches=lambda v: v == "0"),
            SimpleNamespace(concept=C.MAC_ADDRESS, matches=lambda v: int(v) < 7)]
with patch("app.engine.schema_discovery.patterns_for_industry", return_value=patterns), \
     patch("app.engine.schema_discovery.looks_sequential", return_value=False):
    concept, ratio, _ = _pattern_analysis([str(i) for i in range(10)], "unknown")
assert ratio == .1 and concept == C.UUID
note("G09", {"returned_ratio": ratio, "available_better_ratio": .7,
             "fixture": "controlled pattern predicates"},
     "Below threshold, retain the actual best-effort pattern.")

signals = {s: _anomaly_signal(s) for s in ["no failure", "fault resolved"]}
assert all(signals.values())
note("G16", signals, "Negation and resolved status must not generate active failures.")

h = {"hypotheses": [{"cause": "test", "evidence": ["INC-1"], "confidence": 1}]}
e = {"observed_failures": [{"citation": "INC-10"}]}
out = _calibrate_confidence(h, e)["hypotheses"][0]["confidence"]
assert out == .7
note("G20", {"invented_citation": "INC-1", "known_citation": "INC-10",
             "confidence_cap": out},
     "Require exact, source-qualified citation identity; reject every unsupported citation.")

class Flaky:
    calls = 0
    def embed_documents(self, texts):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("synthetic transient outage")
        return [[1., 0.] for _ in texts]

client = EmbeddingClient("offline-fixture")
client._embedder = Flaky()
with patch("app.services.rate_limit.get_rate_limiter",
           return_value=SimpleNamespace(acquire=lambda **kw: None)):
    first = client.embed(["pump"])
    second = client.embed(["pump"])
assert first is None and second is None and client._embedder.calls == 1
note("G13", {"second_attempt": None, "provider_calls": 1, "available": client.available},
     "Recover after bounded backoff; a transient exception must not disable the process forever.")

print(json.dumps({"kind": "offline implementation counterexamples",
                  "external_calls": 0, "database_writes": 0,
                  "reproduced": len(results), "results": results}, indent=2))
