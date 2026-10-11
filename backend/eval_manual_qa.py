"""Real public-document acceptance; excerpts never count as successful answers.

Download the two official PDFs linked in SOURCES into the existing demo folder.
Run from backend with the local server running. Reuses identical existing uploads.
"""
import concurrent.futures
import argparse
import json
import re
import statistics
import time
import unicodedata
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "arduino-uno-r3.pdf": "https://docs.arduino.cc/resources/datasheets/A000066-datasheet.pdf",
    "atmega328p-datasheet.pdf": "https://ww1.microchip.com/downloads/en/DeviceDoc/Atmel-7810-Automotive-Microcontrollers-ATmega328P_Datasheet.pdf",
}
# Ground truth inspected in the official PDFs, with physical PDF page numbers.
CASES = [
    ("offline_setup", 0, "What must I install and what cable is needed to program the UNO R3 without an internet connection?", [r"desktop ide", r"usb.b"], [{9,21}]),
    ("voltage_table", 0, "What are the maximum input voltages for VIN and USB?", [r"20\s*v", r"5\.5\s*v"], [{6,18}]),
    ("memory", 0, "What are the flash, SRAM and EEPROM capacities of the ATmega328P?", [r"32\s*k",r"2\s*k",r"1\s*k"], [{2}]),
    ("conflict", 0, "Does the manual give consistent processor clock speeds? Compare Features and Processor sections.", [r"16\s*mhz",r"20\s*mhz"], [{2},{7,19}]),
    ("pinout", 0, "Which analog pins carry the I2C data and clock lines?", [r"a4",r"a5",r"sda",r"scl"], [{11,23}]),
    ("placeholder", 0, "The PMax row contains xx. Does this PDF establish a numeric maximum whole-board power consumption?", [], []),
    ("absent_warranty", 0, "What is the warranty expiry date of my installed UNO R3?", [], []),
    ("component_table", 0, "Which module is listed at board reference U3?", [r"atmega16u2"], [{7,19}]),
    ("summary", 0, "Summarize how to start using this board and its main hardware limitations. Include flash/SRAM/EEPROM capacities and VIN/USB voltage limits.", [r"ide",r"usb",r"32\s*k",r"20\s*v",r"5\.5\s*v"], [{9,21},{2},{6,18}]),
    ("large_endurance", 1, "What write/erase endurance is specified for flash and EEPROM?", [r"10,?000",r"100,?000"], [{1,18}]),
    ("large_voltage", 1, "What operating voltage range is specified for ATmega328P?", [r"2\.7",r"5\.5"], [{2}]),
    ("late_errata", 1, "In the Errata section, what is stated for device Revision D versus Revision A to Revision C?", [r"no known errata",r"not sampled"], [{288}]),
    ("scope_guard", 1, "What is the UNO R3 board's VIN pad maximum input voltage?", [], []),
]


def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).casefold()


def ask(client, doc_id, question, patterns, page_groups, history=None):
    start = time.perf_counter()
    response = client.post("/api/chat",json={"message":question,"document_id":doc_id,"history":history or []})
    response.raise_for_status()
    events = [json.loads(l[5:]) for l in response.text.splitlines() if l.startswith("data:")]
    result = next(e["result"] for e in reversed(events) if e.get("step") == "complete")
    answer = result.get("answer", "")
    used = re.findall(r"\[(PDF:\d+:p\d+:c\d+)\]",answer)
    pages = {int(c.split(":p")[1].split(":")[0]) for c in used if c.startswith(f"PDF:{doc_id}:")}
    text = normalize(answer)
    answer_ok = all(re.search(p,text) for p in patterns) if patterns else "do not establish" in text
    pages_ok = all(g & pages for g in page_groups)
    valid = {c["id"] for c in result.get("citations",[]) if isinstance(c,dict)}
    cites_ok = all(c in valid and c.startswith(f"PDF:{doc_id}:") for c in used)
    passed = result.get("answer_mode") == "llm" and answer_ok and pages_ok and cites_ok
    return {"question":question,"seconds":round(time.perf_counter()-start,3),"mode":result.get("answer_mode"),"retrieval":result.get("retrieval_method"),"answer":answer[:1800],"used_citations":used,"answer_ok":bool(answer_ok),"page_ok":bool(pages_ok),"passed":bool(passed),"validation_messages":[e["message"] for e in events if e.get("step")=="answer_validation"]}


def main(reprocess=False):
    report = {"scope":"Bounded local acceptance, not an industrial benchmark or proof of semantic entailment", "sources":SOURCES,"uploads":[],"cases":[]}
    with httpx.Client(base_url="http://127.0.0.1:8001",timeout=360) as client:
        documents = []
        for filename, source in SOURCES.items():
            path = ROOT / "demo" / filename
            data = path.read_bytes()
            before = next((d for d in client.get('/api/documents').json() if d['filename']==filename),None)
            start = time.perf_counter(); native = None
            if reprocess and before:
                r=client.post(f"/api/documents/{before['id']}/reprocess",json={'extract_assets':False})
            else:
                r=client.post('/api/documents/ingest',files={'files':(filename,data,'application/pdf')})
            r.raise_for_status()
            job=r.json()
            with client.stream('GET',f"/api/documents/jobs/{job['job_id']}/stream") as stream:
                for line in stream.iter_lines():
                    if not line.startswith('data:'): continue
                    ev=json.loads(line[5:])
                    if ev.get('step')=='searchable': native=round(time.perf_counter()-start,3)
                    if ev.get('step')=='_end': assert ev.get('status')=='done',ev
            doc=next(d for d in client.get('/api/documents').json() if d['filename']==filename)
            assert client.get(f"/api/documents/{doc['id']}/file").content==data
            report['uploads'].append({'filename':filename,'pages':doc['page_count'],'passages':doc['passages'],'already_present':bool(before),'reprocessed':bool(reprocess and before),'native_searchable_seconds':native,'finished_seconds':round(time.perf_counter()-start,3),'original_bytes_match':True})
            documents.append(doc)
        for name,index,q,patterns,groups in CASES:
            result=ask(client,documents[index]['id'],q,patterns,groups)
            report['cases'].append({'name':name,**result})
            print(name,result['passed'],result['mode'],result['seconds'],flush=True)
        follow=ask(client,documents[0]['id'],"And for USB?",[r"5\.5\s*v"],[{6,18}],history=[{'role':'user','content':'What is the maximum VIN input voltage?'},{'role':'assistant','content':'The VIN maximum is 20 V.'}])
        report['cases'].append({'name':'follow_up',**follow})
        # Modest four-request concurrency, separately measured from sequential answers.
        start=time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            cases=CASES[:4]
            report['concurrent']=list(executor.map(lambda case: ask(client,documents[case[1]]['id'],case[2],case[3],case[4]),cases))
        report['concurrent_wall_seconds']=round(time.perf_counter()-start,3)
        pdf_connector=next(c for c in client.get('/api/connectors').json() if any(d['id']==documents[0]['id'] for d in c['documents']))
        assert pdf_connector['kind']=='document' and pdf_connector['documents'][0]['passages']>0
        assert client.post(f"/api/connectors/{pdf_connector['id']}/sync",json={'payload':None}).status_code==409
        report['pdf_connector']={'kind':pdf_connector['kind'],'asset_records':pdf_connector['records'],'passages':sum(d['passages'] for d in pdf_connector['documents']),'json_sync_rejected':True}
    checks=report['cases']+report['concurrent']
    report['passed']=all(c['passed'] for c in checks)
    report['sequential_median_seconds']=round(statistics.median(c['seconds'] for c in report['cases']),3)
    output=ROOT/'docs'/'atlas-real-document-acceptance.json'
    output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print('report',output,'passed',report['passed'],flush=True)
    assert report['passed'],'Real document acceptance failures recorded; inspect the report'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reprocess',action='store_true',help='Measure parsing/OCR again for existing originals')
    main(parser.parse_args().reprocess)
