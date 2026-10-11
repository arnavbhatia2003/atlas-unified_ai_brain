"""Entity-resolution tests, built from the architecture doc's Examples 1-4.

Assertions focus on clustering outcomes and merge decisions (unambiguous across
the doc), not exact confidence floats. A deterministic similarity function
injects the doc's stated cosine values so no live embedding model is needed.
"""

from __future__ import annotations

import pytest

from app.engine.resolution import MergeDecision, SourceRecord, resolve


def _sim(pairs: dict[frozenset[str], float]):
    def similarity(a: SourceRecord, b: SourceRecord) -> float:
        return pairs.get(frozenset({a.record_id, b.record_id}), 0.0)
    return similarity


# --- Example 1: clean merge via serial anchor, C joins by embedding ---------

def test_example1_clean_merge():
    a = SourceRecord(record_id="A", system="SAP",
                     identifiers={"serial_number": ["SN-4521987"]},
                     semantic_text="CR10-10 GRUNDFOS")
    b = SourceRecord(record_id="B", system="ServiceNow",
                     identifiers={"serial_number": ["SN-4521987"],
                                  "asset_tag": ["AT-7729-A"]},
                     semantic_text="GRUNDFOS CR 10-10")
    c = SourceRecord(record_id="C", system="CMDB",
                     identifiers={"uuid": ["550e8400-e29b-41d4-a716-446655440001"],
                                  "mac_address": ["00:80:F4:12:9A:3B"]},
                     semantic_text="MAIN CENTRIFUGAL PUMP GRUNDFOS CR10-10")
    sim = _sim({frozenset({"A", "C"}): 0.91, frozenset({"B", "C"}): 0.91})

    res = resolve([a, b, c], similarity=sim)

    assert len(res.clusters) == 1
    cluster = res.clusters[0]
    assert set(cluster.members) == {"A", "B", "C"}
    assert cluster.decision != MergeDecision.AUTO_MERGED  # weakest membership controls approval
    # multi-valued identifiers preserved
    assert cluster.identifiers["serial_number"] == ["sn-4521987"]
    assert "00:80:f4:12:9a:3b" in cluster.identifiers["mac_address"]


# --- Example 2: transitive conflict -> split, bridge record flagged ---------

def test_example2_transitive_conflict_splits():
    a = SourceRecord(record_id="A", system="SAP",
                     identifiers={"serial_number": ["SN-123"]})
    b = SourceRecord(record_id="B", system="ServiceNow",
                     identifiers={"serial_number": ["SN-123"], "asset_tag": ["AT-456"]})
    c = SourceRecord(record_id="C", system="CMDB",
                     identifiers={"serial_number": ["SN-999"], "asset_tag": ["AT-456"]})

    res = resolve([a, b, c])

    # A and B merge (same serial); C stays separate (conflicting serial).
    ab = res.cluster_of("A")
    assert set(ab.members) == {"A", "B"}
    assert res.cluster_of("C").members == ["C"]
    assert res.cluster_of("C").decision == MergeDecision.SINGLETON

    # a bridge conflict was surfaced naming the conflicting serials
    bridges = [r for r in res.review_items if r.kind == "bridge_conflict"]
    assert bridges, "expected a bridge_conflict review item"
    reason = bridges[0].reason.lower()
    assert "sn-123" in reason and "sn-999" in reason
    assert bridges[0].detail["conflict_concept"] == "serial_number"


# --- Example 3: semantic-only match -> suggest merge ------------------------

def test_example3_semantic_only_suggests():
    a = SourceRecord(record_id="A", system="SAP",
                     identifiers={},  # serial unreadable
                     semantic_text="CR10-10 GRUNDFOS PLANT_7")
    b = SourceRecord(record_id="B", system="CMDB",
                     identifiers={"uuid": ["abc-123-def-456"],
                                  "mac_address": ["00:80:F4:12:9A:3B"]},
                     semantic_text="MAIN CENTRIFUGAL PUMP GRUNDFOS CR10-10 PLANT 7 LINE 2")
    sim = _sim({frozenset({"A", "B"}): 0.88})

    res = resolve([a, b], similarity=sim)

    cluster = res.cluster_of("A")
    assert set(cluster.members) == {"A", "B"}
    assert cluster.decision == MergeDecision.SUGGESTED  # 0.80 <= 0.88 < 0.95
    assert any(r.kind == "suggest_merge" for r in res.review_items)


# --- Example 4: multi-MAC asset; unrelated device stays separate ------------

def test_example4_multi_mac():
    a = SourceRecord(record_id="A", system="CMDB",
                     identifiers={"mac_address": ["00:80:F4:12:9A:3B"]},
                     semantic_text="pump-p7-l2-001")
    b = SourceRecord(record_id="B", system="CMDB",
                     identifiers={"mac_address": ["00:80:F4:12:9A:3B"]},
                     semantic_text="pump-p7-l2-001-backup")
    c = SourceRecord(record_id="C", system="NetMon",
                     identifiers={"mac_address": ["A4:83:E7:45:2B:11"]},
                     semantic_text="dev_7729")
    sim = _sim({frozenset({"A", "C"}): 0.42, frozenset({"B", "C"}): 0.40})

    res = resolve([a, b, c], similarity=sim)

    ab = res.cluster_of("A")
    assert set(ab.members) == {"A", "B"}          # same MAC -> merged
    assert res.cluster_of("C").members == ["C"]   # different MAC, weak text -> separate


# --- decision bands --------------------------------------------------------

def test_uuid_only_match_is_suggested_not_auto():
    """A UUID match (0.85) is below the 0.95 auto-merge band -> suggested."""
    a = SourceRecord(record_id="A", system="X",
                     identifiers={"uuid": ["550e8400-e29b-41d4-a716-446655440001"]})
    b = SourceRecord(record_id="B", system="Y",
                     identifiers={"uuid": ["550e8400-e29b-41d4-a716-446655440001"]})
    res = resolve([a, b])
    assert res.cluster_of("A").decision == MergeDecision.SUGGESTED


# --- no over-flagging: embedding-only similarity with different strong IDs --

def test_embedding_only_conflict_is_not_flagged():
    """Two records that merely look alike but carry different serials are just
    different assets — not a review item (avoids flooding the queue)."""
    a = SourceRecord(record_id="A", system="SAP",
                     identifiers={"serial_number": ["SN-135"]},
                     semantic_text="Feed compressor, plant 4")
    b = SourceRecord(record_id="B", system="CMDB",
                     identifiers={"serial_number": ["SN-130"]},
                     semantic_text="Feed pump, plant 5, line 1")
    sim = _sim({frozenset({"A", "B"}): 0.63})  # above the review floor

    res = resolve([a, b], similarity=sim)

    assert len(res.clusters) == 2  # different assets
    assert not any(r.kind == "bridge_conflict" for r in res.review_items)


# --- human decisions survive resolution ------------------------------------

def test_forced_separate_blocks_merge():
    """A 'keep separate' decision prevents even a same-serial auto-merge."""
    a = SourceRecord(record_id="A", system="X", identifiers={"serial_number": ["SN-1"]})
    b = SourceRecord(record_id="B", system="Y", identifiers={"serial_number": ["SN-1"]})

    res = resolve([a, b], forced_separate={frozenset({"A", "B"})})

    assert res.cluster_of("A").members == ["A"]
    assert res.cluster_of("B").members == ["B"]


def test_forced_merge_joins_unrelated_records():
    """A 'confirm merge' decision joins records that share no evidence."""
    a = SourceRecord(record_id="A", system="X", identifiers={"serial_number": ["SN-1"]})
    b = SourceRecord(record_id="B", system="Y", identifiers={"serial_number": ["SN-2"]})

    res = resolve([a, b], forced_merge={frozenset({"A", "B"})})

    assert set(res.cluster_of("A").members) == {"A", "B"}
    assert res.cluster_of("A").decision == MergeDecision.AUTO_MERGED
