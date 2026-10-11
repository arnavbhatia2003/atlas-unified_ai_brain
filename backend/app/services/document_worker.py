"""Isolated Docling worker: a native parser/OCR crash must not kill the API."""
import json
import sys
import tempfile
from pathlib import Path
from dataclasses import asdict
import pypdfium2 as pdfium
from .documents import _get_converter, _extract_tables, _extract_chunks_and_outline, DocChunk


def convert_pages(source, output, selected):
    converter = _get_converter(True)
    chunks, tables, sections = [], [], []
    original = pdfium.PdfDocument(source)
    try:
        # Process bounded batches while reusing the warmed converter/models.
        for start in range(0, len(selected), 10):
            page_numbers = selected[start:start+10]
            subset = pdfium.PdfDocument.new()
            subset.import_pages(original, pages=[p-1 for p in page_numbers])
            with tempfile.NamedTemporaryFile(suffix=".pdf", dir=Path(source).parent, delete=False) as f:
                path = Path(f.name)
            try:
                subset.save(path); subset.close()
                result = converter.convert(str(path))
                cs, outline = _extract_chunks_and_outline(result.document)
                ts = _extract_tables(result.document)
                for item in [*cs, *ts]:
                    item.page = page_numbers[item.page - 1] if item.page and item.page <= len(page_numbers) else None
                for table in ts:
                    for row in table.rows:
                        text = " | ".join(f"{column}: {value}" for column, value in row.items())
                        if text.strip():
                            cs.append(DocChunk(index=len(cs), text=text, page=table.page, section="Table"))
                chunks.extend(asdict(c) for c in cs)
                tables.extend(asdict(t) for t in ts)
                sections.extend(outline)
            finally:
                path.unlink(missing_ok=True)
    finally:
        original.close()
    Path(output).write_text(json.dumps({"chunks":chunks,"tables":tables,"sections":sections}),encoding="utf-8")

def main():
    if sys.argv[1:] != ["--server"]:
        source, output, selected_json = sys.argv[1:]
        convert_pages(source, output, json.loads(selected_json))
        return
    from contextlib import redirect_stdout
    from docling.datamodel.base_models import InputFormat
    with redirect_stdout(sys.stderr):
        _get_converter(True).initialize_pipeline(InputFormat.PDF)
    print("ATLAS_DOCLING_READY", flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        try:
            with redirect_stdout(sys.stderr):
                convert_pages(request["source"], request["output"], request["pages"])
        except Exception as exc:
            Path(request["output"]).write_text(json.dumps({"error_type":type(exc).__name__}), encoding="utf-8")
        print("ATLAS_DOCLING_DONE", flush=True)

if __name__ == "__main__": main()
