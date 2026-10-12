"""Live NIM + PostgreSQL acceptance; all fixture data rolls back, even on failure."""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

from app.config import get_settings
from app.services import pipeline
from app.workflows.compliance import build_compliance_graph
from app.workflows.rca import build_rca_graph
from app.workflows.resolve_mention import resolve_mention
from app.workflows.retrieval import asset_okf, hybrid_search

ROOT = Path(__file__).resolve().parents[1]
TABLES = ("source_records", "unified_assets", "edges", "okf_documents", "document_parses")


async def main():
    conn = await asyncpg.connect(get_settings().database_url)
    baseline = {t: await conn.fetchval(f"select count(*) from {t}") for t in TABLES}
    transaction = conn.transaction()
    await transaction.start()
    original_pool = pipeline.get_pool
    report = {"scope": "Small labeled synthetic asset scenario with live hosted models and real database; not an industrial accuracy benchmark", "checks": []}

    class TransactionPool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    pipeline.get_pool = lambda: TransactionPool()

    def check(name, passed, **evidence):
        report["checks"].append({"name": name, "passed": bool(passed), **evidence})
        print(name, "PASS" if passed else "FAIL", flush=True)
        assert passed, name

    def record(rid, serial, name, description, kind=None, **fields):
        return {"record_id": rid, "serial_number": serial, "name": name,
                "description": description, "_source": "document",
                "_entity_kind": "physical_instance", "_identity_namespace": "evaluation-plant",
                **({"_event_kind": kind} if kind else {}), **fields}

    async def ingest(records):
        started = time.perf_counter()
        events = [e async for e in pipeline.ingest_records(records)]
        final = events[-1]
        check("ingestion enrichment complete", final.get("step") == "complete" and not final.get("warnings"), seconds=round(time.perf_counter()-started, 3), events=events)
        return final

    try:
        registry = [record("pump1", "SN-900001", "Pump P-101", "Installed pump P-101, serial SN-900001"),
                    record("pump2", "SN-900002", "Pump P-102", "Installed pump P-102, serial SN-900002")]
        operations = [
            record("incident", "SN-900001", "Pump P-101", "Pump P-101 serial SN-900001 has a vibration alarm at 7 mm/s while running. No cause has been diagnosed.", "failure"),
            record("normal", "SN-900002", "Pump P-102", "Pump P-102 serial SN-900002 is operating normally with no fault and vibration within tolerance.", "failure"),
            record("rule", "SN-900001", "Pump P-101", "Annual vibration inspection required for Pump P-101 serial SN-900001 under rule R-EVAL, period 2026.", "rule", rule_id="R-EVAL", period="2026"),
            record("old_completion", "SN-900001", "Pump P-101", "Vibration inspection completed for Pump P-101 serial SN-900001 under rule R-EVAL for period 2025.", "work_order", rule_id="R-EVAL", status="completed", period="2025"),
            record("unrelated_completion", "SN-900001", "Pump P-101", "Paint inspection completed for Pump P-101 serial SN-900001 under different rule R-OTHER for period 2026.", "work_order", rule_id="R-OTHER", status="completed", period="2026"),
        ]
        await ingest({"evaluation registry": registry, "evaluation operations": operations})
        resolved = await resolve_mention(conn, "SN-900001")
        healthy = await resolve_mention(conn, "SN-900002")
        uid = resolved.asset.unified_id if resolved.asset else None
        other = healthy.asset.unified_id if healthy.asset else None
        check("exact serial aliases resolve; different units stay separate", uid and other and uid != other, asset=uid, other_asset=other)
        members = await conn.fetchval("select count(*) from asset_source_records where unified_id=$1", uid)
        check("five records for the installed pump share one canonical asset", members == 5, members=members)
        edges = await conn.fetch("select relation_type, metadata from edges where source_id=$1", "asset:"+uid)
        operational = [dict(e) for e in edges if e['relation_type'] in ('HAS_FAILURE','HAS_WORK_ORDER','SUBJECT_TO')]
        check("graph links observations, obligations and work orders with provenance", len(operational)==4 and all('citation' in json.loads(e['metadata']) for e in operational), edges=operational)
        check("benign record creates no failure edge", not await conn.fetchval("select exists(select 1 from edges where source_id=$1 and relation_type='HAS_FAILURE')", 'asset:'+other))
        doc = await asset_okf(conn, uid)
        embedded = await conn.fetchval("select embedding is not null from okf_documents where unified_id=$1", uid)
        check("OKF dossier is built, cited, stored and embedded", bool(doc and doc['markdown'] and doc['citations'] and embedded), markdown=doc['markdown'][:10000], embedded=embedded)
        rca = (await build_rca_graph().ainvoke({"conn":conn,"question":"Which possible causes explain the vibration alarm, and what is uncertain?","asset_hint":"SN-900001","stages":[]}))['result']
        hypotheses = rca['report']['hypotheses']
        check("RCA uses a real model with cited uncertain hypotheses", rca['report'].get('generation_mode')=='llm' and hypotheses and all(h['evidence'] and h['confidence']<1 for h in hypotheses), result=rca)
        benign = (await build_rca_graph().ainvoke({"conn":conn,"question":"Why did it fail?","asset_hint":"SN-900002","stages":[]}))['result']
        check("RCA does not invent failures on healthy equipment", benign['report'].get('generation_mode')=='no_evidence' and not benign['report']['hypotheses'])
        compliance = build_compliance_graph()
        before = (await compliance.ainvoke({"conn":conn,"question":"Does the old inspection satisfy the 2026 obligation?","asset_hint":"SN-900001","stages":[]}))['result']
        check("old-period and unrelated completions cannot satisfy the obligation", len(before['at_risk_assets'])==1 and before['narrative']['posture']=='at_risk' and before['narrative'].get('generation_mode')=='llm', result=before)
        empty = (await compliance.ainvoke({"conn":conn,"asset_hint":"SN-900002","stages":[]}))['result']
        check("no rules means unknown, not compliant", empty['assessed_rule_links']==0 and empty['narrative']['posture']=='unknown' and empty['narrative'].get('generation_mode')=='no_evidence')
        found = await hybrid_search(conn, "What vibration alarm and inspection obligation are recorded for pump P-101?", k=6)
        sources = [r for r in found if r.system!='OKF']
        check("live dense retrieval and reranking retrieve the incident plus OKF", any(r.citation=='evaluation operations:incident' for r in sources) and any(r.system=='OKF' for r in found) and all(r.raw.get('vector_search_used') and r.raw.get('reranker_used') and not r.raw.get('retrieval_warnings') for r in sources), citations=[r.model_dump() for r in found])
        update = {**operations[3], "period":"2026", "description":"Vibration inspection completed for Pump P-101 serial SN-900001 under rule R-EVAL for period 2026."}
        final = await ingest({"evaluation operations":[update]})
        after = (await compliance.ainvoke({"conn":conn,"question":"Is the linked 2026 inspection obligation now satisfied?","asset_hint":"SN-900001","stages":[]}))['result']
        check("current rule/period completion satisfies only the linked obligation", not after['at_risk_assets'] and after['assessed_rule_links']==1 and after['narrative'].get('generation_mode')=='llm', result=after)
        new_uid = (await resolve_mention(conn,"SN-900001")).asset.unified_id
        check("record revision preserves canonical ID and archives old evidence", new_uid==uid and final['summary']['updated']==1 and await conn.fetchval("select count(*) from source_record_revisions r join source_records s on s.id=r.source_record_id where s.record_id='evaluation operations:old_completion'")==1)
    except Exception as exc:
        report['error'] = type(exc).__name__+': '+str(exc)
        raise
    finally:
        pipeline.get_pool = original_pool
        await transaction.rollback()
        remaining = {t: await conn.fetchval(f"select count(*) from {t}") for t in TABLES}
        report['database_preserved'] = baseline==remaining
        report['baseline_counts'] = baseline
        report['after_counts'] = remaining
        report['passed'] = not report.get('error') and report['database_preserved'] and all(c['passed'] for c in report['checks'])
        (ROOT/'docs/atlas-asset-workflow-acceptance.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
        await conn.close()
        assert report['database_preserved'], 'Evaluation altered persistent user data'


if __name__ == '__main__':
    asyncio.run(main())
