"""Regressions for real manuals, instance identity, and changed source records."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.engine.resolution import SourceRecord, resolve
from app.services.doc_extract import _ie_chunk, _mention_to_record, _records_from_tables
from app.services.documents import DocChunk, DocTable, ParsedDocument, _extract_chunks_and_outline


def parsed(sha="a" * 64):
    return ParsedDocument("manual.pdf", sha, "test", 2, "Manual", "", [])


def test_identifier_table_extracts_without_crashing_and_versions_keep_distinct_mentions():
    a, b = parsed(), parsed("b" * 64)
    for p in (a, b):
        p.tables = [DocTable(2, "Equipment", ["serial_number"], [{"serial_number": "SN-123456"}])]
    ra, rb = _records_from_tables(a), _records_from_tables(b)
    assert ra[0]["serial_number"] == "SN-123456"
    assert ra[0]["_page"] == 2
    assert ra[0]["record_id"] != rb[0]["record_id"]


def test_repeated_literal_document_serials_keep_identity_evidence():
    from app.services.pipeline import normalize_records
    from app.engine.schema_discovery import discover_schema
    records = [{"record_id":str(i),"_source":"document","serial_number":"SN-123456","description":"Installed pump"} for i in range(4)]
    normalized = normalize_records("document",records,discover_schema(records))
    assert all(r.identifiers.get("serial_number") == ["SN-123456"] for r in normalized)


def test_pinout_and_reference_tables_do_not_create_installed_assets():
    p = parsed()
    p.tables = [DocTable(2, "Pinout", ["Pin", "Function"], [{"Pin": "11", "Function": "MOSI"}])]
    assert _records_from_tables(p) == []
    p.chunks = [DocChunk(0, "Arduino UNO R3 is a product model.", 1, "")]
    assert _mention_to_record(p, p.chunks[0], 0, {"asset": "Arduino UNO R3", "entity_kind": "equipment_model"}) is None
    assert _mention_to_record(p, p.chunks[0], 0, {"asset": "invented pump", "entity_kind": "physical_instance"}) is None


def test_docling_blank_items_and_page_changes_keep_provenance():
    def item(text, page):
        return SimpleNamespace(text=text, label="text", prov=[SimpleNamespace(page_no=page)])
    chunks, _ = _extract_chunks_and_outline(SimpleNamespace(texts=[item("", 1), item("First page fact.", 1), item("Second page fact.", 2)]))
    assert [(c.page,c.text) for c in chunks] == [(1,"First page fact."),(2,"Second page fact.")]


@pytest.mark.parametrize("response", ['not json', '{"mentions": null}', '{"mentions":["bad"]}'])
def test_invalid_extraction_is_an_error_not_a_cached_zero(response):
    class Model:
        async def ainvoke(self, messages, **kwargs): return SimpleNamespace(content=response)
    with pytest.raises(ValueError): asyncio.run(_ie_chunk(Model(), "A passage"))


def test_extraction_provider_outage_propagates():
    class Model:
        async def ainvoke(self, messages, **kwargs): raise ConnectionError("Unavailable")
    with pytest.raises(ConnectionError): asyncio.run(_ie_chunk(Model(), "A passage"))


@pytest.mark.parametrize("field,values", [("entity_kind", ["physical_instance","functional_position"]),("identity_namespace",["plant-a","plant-b"])])
def test_shared_serial_cannot_override_explicit_identity_scope(field, values):
    records = [SourceRecord(record_id=str(i),system="test",identifiers={"serial_number":["SN-123456"]},**{field:v}) for i,v in enumerate(values)]
    result = resolve(records)
    assert len(result.clusters) == 2
    assert result.review_items[0].kind == "identity_scope"


def test_scope_guard_survives_an_unspecified_transitive_bridge():
    records = [SourceRecord(record_id=str(i),system="test",identity_namespace=v,identifiers={"serial_number":["SN-123456"]}) for i,v in enumerate(["plant-a","","plant-b"])]
    result = resolve(records)
    assert not any({"0","2"}.issubset(c.members) for c in result.clusters)


def test_manual_counts_are_not_asset_inventory_questions():
    from app.workflows.chat import _keyword_intent
    assert _keyword_intent("How many digital pins does the UNO R3 have?") is None
    assert _keyword_intent("How many assets do we have?") == "overview"


def test_nemotron_3_disables_reasoning_with_request_parameter(monkeypatch):
    from app.services import llm
    monkeypatch.setattr(llm, "ChatNVIDIA", lambda **kwargs: kwargs)
    monkeypatch.setattr(llm, "get_rate_limiter", lambda: None)
    payload = llm._build("nvidia/nemotron-3-super-120b-a12b", 0)
    assert payload["model_kwargs"]["chat_template_kwargs"]["enable_thinking"] is False


def test_truncated_json_answer_is_not_accepted(monkeypatch):
    from app.workflows import chat
    class Model:
        async def ainvoke(self, messages, **kwargs):
            assert kwargs["response_format"]["type"] == "json_schema"
            return SimpleNamespace(content='{"answerable":true,"answer":"Incomplete","citations":["PDF:1:p1:c0"]}',response_metadata={"finish_reason":"length"})
    monkeypatch.setattr(chat,"get_fast_chat_model",lambda:Model())
    monkeypatch.setattr(chat,"resilient",lambda m:m)
    monkeypatch.setattr(chat,"emit",lambda *args,**kwargs:None)
    assert asyncio.run(chat._stream_grounded_answer("","",{"PDF:1:p1:c0"})) == ""


def test_changed_records_keep_previous_payload_and_clear_stale_enrichment():
    from app.config import get_settings
    from app.services.pipeline import store_source_delta
    import asyncpg
    from pydantic import ValidationError
    async def scenario():
        try:
            conn = await asyncpg.connect(get_settings().database_url, timeout=5)
        except (OSError, ValidationError):
            pytest.skip("Local Postgres unavailable")
        transaction = conn.transaction()
        await transaction.start()
        try:
            system = "__test_revision_contract__"
            old = {"record_id":"r1","description":"Before"}
            new = {**old,"description":"After"}
            assert (await store_source_delta(conn,{system:[old]}))["new"] == 1
            await conn.execute("insert into operational_extractions(record_id,payload) values($1,'{}')",system+":r1")
            assert (await store_source_delta(conn,{system:[new]}))["updated"] == 1
            assert (await store_source_delta(conn,{system:[new]}))["skipped"] == 1
            previous = await conn.fetchval("select r.raw_json from source_record_revisions r join source_records s on s.id=r.source_record_id where s.system=$1",system)
            assert json.loads(previous)["description"] == "Before"
            assert not await conn.fetchval("select exists(select 1 from operational_extractions where record_id=$1)",system+":r1")
        finally:
            await transaction.rollback()
            await conn.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", ['invalid json', [None], {"records":[1]}])
def test_connector_invalid_data_cannot_succeed_as_zero_records(payload):
    from app.services.connectors import _coerce_records
    with pytest.raises(ValueError): _coerce_records(payload)


def test_failed_operational_extraction_remains_retryable(monkeypatch):
    from app.workflows import extract_ops
    class Conn:
        async def fetchval(self, *args): return None
        async def execute(self, *args): raise AssertionError("Failed model output must not be cached")
    async def unavailable(*args): raise ConnectionError("Unavailable")
    monkeypatch.setattr(extract_ops,"_extract_one",unavailable)
    result = asyncio.run(extract_ops._load_or_extract(Conn(),"r1","Pump vibration alarm",{}))
    assert result["extraction_mode"] == "deterministic_fallback"
    assert result["extraction_error"] == "ConnectionError"
    assert result["confidence"] == 0


@pytest.mark.parametrize("workflow", ["rca", "compliance"])
@pytest.mark.parametrize("failure", ["outage", "malformed", "truncated"])
def test_workflow_failure_cannot_masquerade_as_model_analysis(monkeypatch, workflow, failure):
    from importlib import import_module
    module = import_module("app.workflows." + workflow)
    class Model:
        async def ainvoke(self, *args, **kwargs):
            if failure == "outage": raise ConnectionError("Unavailable")
            return SimpleNamespace(content='{}', response_metadata={"finish_reason":"length" if failure=="truncated" else "stop"})
    monkeypatch.setattr(module, "get_fast_chat_model", lambda: Model())
    monkeypatch.setattr(module, "resilient", lambda m: m)
    evidence = {"at_risk":[{"asset":"test", "rule":"R1"}], "observed_failures":[{"citation":"INC-1", "detail":"Alarm"}]}
    result = asyncio.run(module._default_reason("Analyze", evidence))
    assert result["generation_mode"] == "manual_evidence"
    if workflow == "rca": assert result["hypotheses"] == []
    else: assert result["posture"] == "at_risk"


def test_dead_ocr_worker_cannot_report_model_readiness(monkeypatch):
    from app.services import documents
    monkeypatch.setattr(documents, "_ocr_worker", SimpleNamespace(returncode=1, pid=123))
    monkeypatch.setattr(documents, "_ocr_ready", True)
    monkeypatch.setattr(documents, "_ocr_error", "BrokenPipeError")
    assert documents.docling_status()["state"] == "failed"
    assert documents.docling_status()["ok"] is False


def test_rca_cannot_accept_a_fabricated_model_citation(monkeypatch):
    from app.workflows import rca
    class Model:
        async def ainvoke(self, *args, **kwargs):
            allowed = kwargs["response_format"]["json_schema"]["schema"]["$defs"]["Hypothesis"]["properties"]["evidence"]["items"]["enum"]
            assert allowed == ["INC-1"]
            return SimpleNamespace(content=json.dumps({"summary":"Alarm", "hypotheses":[{"cause":"Imbalance", "explanation":"Possible", "evidence":["FAKE-1"], "confidence":0.6}], "contradictions":[], "unresolved":[]}))
    monkeypatch.setattr(rca, "get_fast_chat_model", lambda:Model())
    monkeypatch.setattr(rca, "resilient", lambda m:m)
    result=asyncio.run(rca._default_reason("Analyze", {"observed_failures":[{"citation":"INC-1", "detail":"Alarm"}]}))
    assert result["generation_mode"] == "manual_evidence"
    assert result["hypotheses"] == []


def test_operational_projection_keeps_obligation_period(monkeypatch):
    from app.workflows import extract_ops
    from app.engine.resolution import SourceRecord
    class Conn:
        def __init__(self): self.edges=[]
        async def execute(self, query, *args):
            if query.startswith("insert into edges"):
                self.edges.append({"relation":args[1], "metadata":json.loads(args[3])})
    async def extracted(*args):
        return {"rule_id":"R1", "sensors":[], "extraction_mode":"model"}
    monkeypatch.setattr(extract_ops, "_load_or_extract", extracted)
    conn=Conn()
    rec=SourceRecord(record_id="r1", system="test")
    asyncio.run(extract_ops.rebuild_operational_edges(conn,[rec],{"r1":"ua-test"},{},{"r1":{"_event_kind":"rule", "period":"2026"}}))
    obligation=next(e for e in conn.edges if e["relation"]=="SUBJECT_TO")
    assert obligation["metadata"]["period"] == "2026"


@pytest.mark.parametrize("quote,accepted", [("Inspection interval is 17 days.",True),("Inspection interval is 3 days.",False)])
def test_citation_requires_verbatim_support_in_that_passage(monkeypatch,quote,accepted):
    from app.workflows import chat
    handle="PDF:1:p2:c1"
    class Model:
        async def ainvoke(self,messages,**kwargs):
            assert "support" in kwargs["response_format"]["json_schema"]["schema"]["required"]
            return SimpleNamespace(content=json.dumps({"answerable":True,"answer":"17 days","citations":[handle],"support":[{"citation":handle,"quote":quote}]}))
    monkeypatch.setattr(chat,"get_fast_chat_model",lambda:Model())
    monkeypatch.setattr(chat,"resilient",lambda m:m)
    monkeypatch.setattr(chat,"emit",lambda *args,**kwargs:None)
    result=asyncio.run(chat._stream_grounded_answer("","",{handle:"Inspection interval is 17 days."}))
    assert bool(result) is accepted


def test_rare_query_terms_outweigh_repeated_common_words_in_long_documents():
    import asyncpg
    from uuid import uuid4
    from pydantic import ValidationError
    from app.config import get_settings
    from app.workflows.retrieval import document_search
    async def scenario():
        try: conn=await asyncpg.connect(get_settings().database_url,timeout=5)
        except (OSError,ValidationError): pytest.skip("Local Postgres unavailable")
        tx=conn.transaction();await tx.start()
        try:
            doc=await conn.fetchval("insert into document_parses(system,filename,sha256,parsed) values('test','test.pdf',$1,'{}') returning id",uuid4().hex*2)
            rows=[(doc,i,i+1,"EEPROM write "*220) for i in range(30)]
            rows.append((doc,30,31,"Flash and EEPROM endurance is 10000 and 100000 write/erase cycles."))
            await conn.executemany("insert into document_chunks(document_id,chunk_index,page,text) values($1,$2,$3,$4)",rows)
            results=await document_search(conn,"What write endurance is specified for flash and EEPROM?",doc,k=1)
            assert results[0].raw["page"] == 31
            assert results[0].raw["retrieval_method"] == "indexed_keyword"
        finally: await tx.rollback();await conn.close()
    asyncio.run(scenario())


def test_exact_quote_binds_to_its_actual_page_not_the_models_page(monkeypatch):
    from app.workflows import chat
    wrong,correct="PDF:1:p1:c0","PDF:1:p2:c1"
    class Model:
        async def ainvoke(self,*args,**kwargs):
            return SimpleNamespace(content=json.dumps({"answerable":True,"answer":f"17 days [{wrong}]","citations":[wrong],"support":[{"citation":wrong,"quote":"Inspection interval is 17 days."}]}))
    monkeypatch.setattr(chat,"get_fast_chat_model",lambda:Model())
    monkeypatch.setattr(chat,"resilient",lambda m:m)
    monkeypatch.setattr(chat,"emit",lambda *args,**kwargs:None)
    result=asyncio.run(chat._stream_grounded_answer("","",{wrong:"A different fact.",correct:"Inspection interval is 17 days."}))
    assert f"[{correct}]" in result and f"[{wrong}]" not in result
