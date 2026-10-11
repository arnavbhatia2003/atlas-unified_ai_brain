"""Measured classroom PDF upload and grounded-answer acceptance check (not an industry benchmark)."""
import json
import time
import runpy
import unicodedata
import re
from pathlib import Path
import httpx


def main():
    make_pdf = runpy.run_path(str(Path(__file__).parent / "tests/test_document_qa.py"))["pdf_bytes"]
    filler = " Maintenance records describe periodic checks, operating conditions, inspection activities and evidence review. Follow the approved procedure and preserve observations for audit." * 16
    pages = [f"Equipment ZEPHYR-{i:03d}. Inspection interval: {i} days. Pressure limit: {i + 20} bar." + filler for i in range(1, 151)]
    pages[11] = "Equipment CONFLICT-900. Recorded pressure limit is 80 bar. Manufacturer is unknown."
    pages[12] = "Equipment CONFLICT-900. Recorded pressure limit is 95 bar. This record conflicts with the earlier pressure limit."
    nonce = str(time.time_ns()).encode()
    data = make_pdf(pages) + b"\n% Acceptance run " + nonce
    t = time.perf_counter()
    with httpx.Client(base_url="http://127.0.0.1:8001", timeout=90) as client:
        upload = client.post("/api/documents/ingest", files={"files":("classroom-acceptance-150-pages.pdf", data, "application/pdf")})
        upload.raise_for_status(); jid = upload.json()["job_id"]
        while True:
            jobs = client.get("/api/documents/jobs").json()["jobs"]
            job = next(j for j in jobs if j["id"] == jid)
            if job["status"] != "running": break
            if time.perf_counter() - t > 90: raise AssertionError("Upload did not finish within 90 seconds")
            time.sleep(0.2)
        ready = round(time.perf_counter()-t, 3)
        assert job["status"] == "done", job
        doc = next(d for d in client.get("/api/documents").json() if d["filename"] == "classroom-acceptance-150-pages.pdf")
        assert doc["page_count"] == 150
        original = client.get(f"/api/documents/{doc['id']}/file")
        assert original.content == data, "Original PDF did not survive"
        cases = [
            ("What is the inspection interval for ZEPHYR-003?", ["3 days"], [3]),
            ("What is the pressure limit of ZEPHYR-149?", ["169", "bar"], [149]),
            ("What are the conflicting pressure limits for CONFLICT-900?", ["80", "95"], [12,13]),
            ("Who manufactured CONFLICT-900?", ["unknown"], [12]),
            ("What is the warranty expiry date of ZEPHYR-149?", [], [149]),
        ]
        results=[]
        for question, words, expected_pages in cases:
            started=time.perf_counter()
            response=client.post("/api/chat", json={"message":question,"document_id":doc["id"]})
            response.raise_for_status()
            events=[json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]
            complete=next(e for e in reversed(events) if e.get("step")=="complete")
            result=complete["result"]; answer=result.get("answer", "")
            retrieved={c.get("page") for c in result.get("citations", []) if isinstance(c,dict)}
            retrieval_ok=set(expected_pages).issubset(retrieved)
            used_pages={int(p) for p in re.findall(rf"\[PDF:{doc['id']}:p(\d+):c\d+\]",answer)}
            normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", answer)).lower()
            answer_ok=all(re.search(r"\b" + re.escape(word.lower()) + r"\b", normalized) for word in words)
            if words == ["unknown"]:
                answer_ok = answer_ok or "do not establish" in normalized
            if not words:
                answer_ok=any(word in answer.lower() for word in ["not contain", "not provided", "not specified", "no information", "not available", "doesn't", "could not", "do not", "cannot", "not mentioned"])
            answer_ok = answer_ok and result.get("answer_mode") == "llm"
            if words and words != ["unknown"]:
                answer_ok = answer_ok and set(expected_pages).issubset(used_pages)
            results.append({"question":question,"seconds":round(time.perf_counter()-started,3),"retrieval_ok":retrieval_ok,"answer_ok":answer_ok,"answer":answer,"citations":result.get("citations",[])})
        invalid=client.post("/api/documents/ingest", files={"files":("fake.pdf",b"not a pdf","application/pdf")})
        assert invalid.status_code==415
        out={"scope":"Synthetic 150-page digital PDF with repeated paragraphs and unique facts; single local user, not representative industrial corpus", "upload_to_ready_seconds":ready,"pdf_bytes":len(data),"document_id":doc["id"],"cases":results,"passed":all(c["retrieval_ok"] and c["answer_ok"] for c in results)}
        # Real mixed-document path: selectable first/last pages and an image-only middle page.
        import io
        import pypdfium2 as pdfium
        make_scan = runpy.run_path(str(Path(__file__).parent / "tests/test_document_qa.py"))["scanned_pdf_bytes"]
        mixed=pdfium.PdfDocument.new()
        for content in [make_pdf(["Digital record FIRST-001. Pressure limit is 41 bar."]), make_scan(), make_pdf(["Digital record LAST-003. Pressure limit is 43 bar."])]:
            source=pdfium.PdfDocument(content); mixed.import_pages(source); source.close()
        stream=io.BytesIO();mixed.save(stream);mixed.close();mixed_data=stream.getvalue() + b"\n% Acceptance run " + nonce
        t=time.perf_counter()
        upload=client.post("/api/documents/ingest", files={"files":("classroom-mixed-scan.pdf",mixed_data,"application/pdf")})
        upload.raise_for_status(); jid=upload.json()["job_id"]
        while True:
            job=next(j for j in client.get("/api/documents/jobs").json()["jobs"] if j["id"]==jid)
            if job["status"]!="running": break
            if time.perf_counter()-t>90: raise AssertionError("Mixed OCR upload exceeded 90 seconds")
            time.sleep(0.2)
        assert job["status"]=="done",job
        mixed_ready=round(time.perf_counter()-t,3)
        doc=next(d for d in client.get("/api/documents").json() if d["filename"]=="classroom-mixed-scan.pdf")
        response=client.post("/api/chat",json={"message":"What is the inspection interval for SCAN-017?", "document_id":doc["id"]})
        events=[json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]
        result=next(e for e in reversed(events) if e.get("step")=="complete")["result"]
        mixed_ok=result.get("answer_mode")=="llm" and bool(re.search(r"\b17\s+days\b", result["answer"])) and bool(re.search(rf"\[PDF:{doc['id']}:p2:c\d+\]",result["answer"]))
        out["mixed_scan"]={"upload_to_ready_seconds":mixed_ready,"parser":doc["parser"],"answer":result["answer"],"citations":result.get("citations",[]),"passed":mixed_ok}
        out["passed"]=out["passed"] and mixed_ok
        path=Path(__file__).parent.parent / "docs/atlas-pdf-acceptance.json"
        path.write_text(json.dumps(out,indent=2),encoding="utf-8")
        print(json.dumps({"upload_to_ready_seconds":ready,"passed":out["passed"],"cases":[{"question":c["question"],"seconds":c["seconds"],"retrieval_ok":c["retrieval_ok"],"answer_ok":c["answer_ok"]} for c in results]}))
        if not out["passed"]: raise SystemExit(1)

if __name__ == "__main__": main()
