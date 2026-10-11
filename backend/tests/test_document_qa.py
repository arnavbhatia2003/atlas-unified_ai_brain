"""Regression tests for document questions and audited failure modes."""
import asyncio
import json
import pytest
from app.services.documents import parse_pdf_bytes
from app.services.embeddings import EmbeddingClient, cosine
from app.engine.resolution import SourceRecord, resolve


def pdf_bytes(pages):
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for text in pages:
        page_id = len(objects) + 1
        kids.append(f"{page_id} 0 R")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode())
        import textwrap
        wrapped = textwrap.wrap(text, width=84) or [""]
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        lines = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in wrapped]
        stream = ("BT /F1 10 Tf 40 740 Td 13 TL " + " T* ".join(f"({line}) Tj" for line in lines) + " ET").encode()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data)); data.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]: data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return bytes(data)


def test_large_pdf_keeps_page_provenance_without_asset_extraction():
    parsed = parse_pdf_bytes(pdf_bytes([f"Page {i}: The inspection interval is {i} days." for i in range(1, 151)]), "teacher.pdf")
    assert parsed.page_count == 150
    assert len(parsed.chunks) == 150
    assert parsed.chunks[-1].page == 150
    assert "150 days" in parsed.chunks[-1].text
    assert parsed.parser == "pypdfium2-text"


def test_blank_pdf_fails_instead_of_claiming_success():
    with pytest.raises(ValueError, match="OCR"):
        parse_pdf_bytes(pdf_bytes([""]), "scan.pdf")


def test_cannot_link_survives_a_transitive_bridge():
    records = [SourceRecord(record_id=x, system="test", identifiers={"serial_number":["SN-123456"]}) for x in "ABC"]
    result = resolve(records, forced_separate={frozenset("AB")})
    assert not any({"A", "B"}.issubset(c.members) for c in result.clusters)


def test_unrelated_record_does_not_renumber_existing_asset():
    a = SourceRecord(record_id="B", system="test")
    before = resolve([a]).cluster_of("B").cluster_id
    after = resolve([SourceRecord(record_id="A", system="test"), a]).cluster_of("B").cluster_id
    assert before == after


def test_invalid_embedding_vectors_cannot_match():
    assert cosine([1], [1, 2]) == 0
    assert cosine([float("nan")], [1]) == 0


def test_embedding_outage_recovers_after_cooldown(monkeypatch):
    from app.services import embeddings
    from app.services.rate_limit import get_rate_limiter
    monkeypatch.setattr(get_rate_limiter(), "acquire", lambda **kw: True)
    client = EmbeddingClient("test")
    class Provider:
        calls = 0
        def embed_query(self, text):
            self.calls += 1
            if self.calls == 1: raise RuntimeError("temporary")
            return [1.0]
    client._embedder = Provider()
    assert client.embed_query("q") is None
    client._retry_at = 0
    assert client.embed_query("q") == [1.0]


def test_negated_faults_are_not_failures_and_mixed_records_keep_real_failures():
    from app.workflows.extract_ops import _anomaly_signal
    assert _anomaly_signal("no failure recorded") is False
    assert _anomaly_signal("fault resolved") is False
    assert _anomaly_signal("no failure on pump A; pump B has a critical leak") is True


def test_rca_short_citation_cannot_match_longer_identifier():
    from app.workflows.rca import _calibrate_confidence
    result = _calibrate_confidence({"hypotheses":[{"cause":"bad", "evidence":["INC-1"], "confidence":1}], "contradictions":[]}, {"observed_failures":[{"citation":"INC-10", "detail":"fault"}]})
    assert result["hypotheses"][0]["confidence"] <= 0.4


def test_connector_rejects_files_and_private_networks():
    from app.services.connectors import _http_get_json
    with pytest.raises(ValueError): _http_get_json("file:///etc/passwd")
    with pytest.raises(ValueError): _http_get_json("http://127.0.0.1/private")


def scanned_pdf_bytes(text="Equipment SCAN-017. Inspection interval is 17 days."):
    import io
    from PIL import Image, ImageDraw, ImageFont
    import pypdfium2 as pdfium
    image = Image.new("RGB", (1400, 1800), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except OSError:
        font = ImageFont.load_default(size=40)
    draw.text((80, 100), text, font=font, fill="black")
    document = pdfium.PdfDocument.new()
    page = document.new_page(700, 900)
    obj = pdfium.PdfImage.new(document)
    bitmap = pdfium.PdfBitmap.from_pil(image)
    obj.set_bitmap(bitmap)
    obj.set_matrix(pdfium.PdfMatrix(700, 0, 0, 900, 0, 0))
    page.insert_obj(obj); page.gen_content()
    stream = io.BytesIO(); document.save(stream)
    page.close(); bitmap.close(); document.close()
    return stream.getvalue()


def test_scanned_pages_are_detected_for_docling():
    from app.services.documents import _parse_pdf_textonly
    parsed = _parse_pdf_textonly(scanned_pdf_bytes(), "scan.pdf", allow_empty=True)
    assert parsed.raw["image_pages"] == [1]
    assert not parsed.chunks


@pytest.mark.parametrize("payload, expected", [
    ({"answerable":True,"answer":"17 days","citations":["PDF:1:p2:c1"]}, "17 days"),
    ({"answerable":True,"answer":"17 days","citations":["[PDF:1:p2:c1]"]}, "17 days"),
    ({"answerable":True,"answer":"17 days","citations":["PDF:999:p2:c1"]}, ""),
    ({"answerable":False,"answer":"I would guess","citations":[]}, "do not establish"),
])
def test_document_answers_validate_source_handles(monkeypatch, payload, expected):
    from types import SimpleNamespace
    from app.workflows import chat
    class Model:
        async def ainvoke(self, messages):
            return SimpleNamespace(content=json.dumps(payload))
    monkeypatch.setattr(chat, "get_fast_chat_model", lambda: Model())
    monkeypatch.setattr(chat, "resilient", lambda model: model)
    monkeypatch.setattr(chat, "emit", lambda *args, **kwargs: None)
    answer = asyncio.run(chat._stream_grounded_answer("Use sources", "question", {"PDF:1:p2:c1"}))
    if expected: assert expected in answer
    else: assert answer == ""


def test_docling_enrichment_keeps_original_page_numbers(monkeypatch, tmp_path):
    from app.services import documents
    import pypdfium2 as pdfium
    import io
    source=pdfium.PdfDocument(pdf_bytes(["Selectable first page with inspection details."]))
    mixed=pdfium.PdfDocument.new();mixed.import_pages(source);source.close()
    source=pdfium.PdfDocument(scanned_pdf_bytes());mixed.import_pages(source);source.close()
    stream=io.BytesIO();mixed.save(stream);mixed.close()
    async def fake_worker(source, output, selected):
        assert selected == [2]
        Path(output).write_text(json.dumps({"chunks":[{"index":0,"text":"Scanned inspection interval is 17 days.","page":2,"section":""}],"tables":[],"sections":[]}),encoding="utf-8")
    from pathlib import Path
    monkeypatch.setattr(documents, "_docling_pages", fake_worker)
    monkeypatch.setattr(documents, "_docling_enabled", lambda: True)
    parsed=asyncio.run(documents.parse_pdf(stream.getvalue(), "mixed.pdf"))
    assert {c.page for c in parsed.chunks} == {1,2}
    assert parsed.raw["ocr_pages"] == [2]
    assert not parsed.raw["unreadable_pages"]
