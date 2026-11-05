"""PDF parsing with original-byte custody and page provenance.

Selectable text is saved with PDFium before every page receives isolated
Docling layout/table processing and required OCR. Call parse_pdf asynchronously. The legacy synchronous
parse_pdf_bytes entry point remains available for native-text/demo callers.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .docling_options import DoclingOptions, configured_options, docling_threads

# Docling fetches its layout/table models from the HuggingFace Hub, which tries
# to symlink into the cache. On Windows without Developer Mode/admin that raises
# WinError 1314; disabling symlinks makes HF copy files instead. Must be set
# before huggingface_hub is imported (i.e. before Docling loads its models).
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# Target size for a narrative chunk (characters). Chunks respect section
# boundaries; a long section is split into several ~this-size chunks.
_CHUNK_CHARS = 1400
_CHUNK_OVERLAP = 150

# Above this page count, skip Docling's memory-heavy image/layout pipeline and
# use the low-memory text extractor (avoids std::bad_alloc on constrained
# machines). Tables/layout are lost for these docs; narrative text is retained.
_MAX_DOCLING_PAGES = 40


def _docling_enabled() -> bool:
    """Master switch for Docling. Set DOCLING_ENABLED=false on small/constrained
    hosts (e.g. a 512 MB free tier) where torch/Docling would OOM — every PDF
    then uses the low-memory text-only extractor. Tables/layout are lost; the
    narrative text (what IE reads) is retained, so PDF ingestion still works."""
    return os.getenv("DOCLING_ENABLED", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

_converters: dict[tuple[bool, DoclingOptions], Any] = {}
PARSER_VERSION = "atlas-evidence-6"
_BAD_GLYPHS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ue000-\uf8ff\ufffd\ufffe]")


@dataclass
class DocTable:
    page: int | None
    section: str
    columns: list[str]
    rows: list[dict[str, Any]]
    provenance: list[dict[str, Any]] = field(default_factory=list)
    cells: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class DocChunk:
    index: int
    text: str
    page: int | None
    section: str  # heading path, e.g. "Findings > Root cause"
    kind: str = "text"
    provenance: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ParsedDocument:
    filename: str
    sha256: str
    parser: str  # "docling" | "docling-no-tables" | ...
    page_count: int
    title: str
    markdown: str
    sections: list[str]  # heading outline
    tables: list[DocTable] = field(default_factory=list)
    chunks: list[DocChunk] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)  # durable Docling export

    def stats(self) -> dict[str, int]:
        return {
            "pages": self.page_count,
            "sections": len(self.sections),
            "tables": len(self.tables),
            "chunks": len(self.chunks),
            "markdown_chars": len(self.markdown),
            "ocr_pages": len(self.raw.get("ocr_pages", [])),
            "unreadable_pages": len(self.raw.get("unreadable_pages", [])),
        }


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _get_converter(ocr: bool, *, force_ocr: bool = False) -> Any:
    """Cache by the full configuration; retain the measured default pipeline."""
    settings = configured_options()
    force_ocr = force_ocr or settings.ocr_mode == "full"
    key = (ocr, settings)
    if key in _converters:
        converter = _converters[key]
        _set_ocr_mode(converter, force_ocr)
        return converter
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        TableFormerMode,
        RapidOcrOptions,
    )
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    # Change inference threads independently of the retained page batches and queues.
    opts.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=settings.threads)
    opts.ocr_batch_size = settings.ocr_batch_size
    opts.layout_batch_size = settings.layout_batch_size
    opts.table_batch_size = settings.table_batch_size
    opts.queue_max_size = settings.queue_max_size
    opts.do_ocr = ocr
    opts.ocr_options = RapidOcrOptions(lang=["english"], backend="onnxruntime")
    opts.document_timeout = settings.document_timeout_seconds
    opts.do_table_structure = True
    # Page previews are exported separately; do not embed images in custody JSON.
    for attr in ("generate_page_images", "generate_picture_images"):
        try:
            setattr(opts, attr, False)
        except Exception:
            pass
    try:
        opts.images_scale = 1.0
    except Exception:
        pass
    opts.table_structure_options.mode = TableFormerMode.ACCURATE if settings.table_mode == "accurate" else TableFormerMode.FAST
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )
    converter.initialize_pipeline(InputFormat.PDF)
    _set_ocr_mode(converter, force_ocr)
    _converters[key] = converter
    return converter


def _set_ocr_mode(converter: Any, force_ocr: bool) -> None:
    # Worker requests are serial. Copy runtime options so the converter's cache hash stays stable.
    from functools import partial
    for pipeline in converter.initialized_pipelines.values():
        pipeline.ocr_model.options = pipeline.ocr_model.options.model_copy(update={"force_full_page_ocr": force_ocr})
        if getattr(pipeline.ocr_model, "enabled", False) and not getattr(pipeline, "_atlas_word_ocr", False):
            pipeline.ocr_model.reader = partial(_word_ocr, pipeline.ocr_model.reader)
            pipeline._atlas_word_ocr = True


def _word_ocr(reader, image, **kwargs):
    """Give TableFormer word boxes so one detected line cannot merge adjacent columns."""
    import numpy as np
    result = reader(image, return_word_box=True, **kwargs)
    if result.boxes is None or len(result.word_results or ()) != len(result.boxes):
        return result
    words = []
    for i, line in enumerate(result.word_results):
        valid = [w for w in line if isinstance(w, tuple) and len(w) == 3 and w[2] is not None]
        words.extend(valid or [(result.txts[i], result.scores[i], result.boxes[i])])
    if words:
        result.boxes = np.asarray([w[2] for w in words])
        result.txts = tuple(w[0] for w in words)
        result.scores = tuple(w[1] for w in words)
    return result


def _page_of(item: Any) -> int | None:
    prov = getattr(item, "prov", None) or []
    if prov:
        p = getattr(prov[0], "page_no", None)
        if isinstance(p, int):
            return p
    return None


def _provenance(item: Any) -> list[dict[str, Any]]:
    out = []
    for p in getattr(item, "prov", []) or []:
        value = p.model_dump(mode="json") if hasattr(p, "model_dump") else {"page_no": p.page_no}
        value["item"] = str(getattr(item, "self_ref", ""))
        out.append(value)
    return out


def _extract_tables(doc: Any) -> list[DocTable]:
    tables: list[DocTable] = []
    for t in getattr(doc, "tables", []) or []:
        cols: list[str] = []
        rows: list[dict[str, Any]] = []
        try:
            df = t.export_to_dataframe(doc=doc)
            cols = [str(c) for c in df.columns]
            # A dict keyed by repeated/blank headers silently loses cells. Keep their ordinal identity.
            keys = [c if c.strip() and cols.count(c) == 1 else f"{c or 'Unlabelled'} [column {i+1}]" for i, c in enumerate(cols)]
            for values in df.itertuples(index=False, name=None):
                rows.append({k: ("" if v is None else str(v)) for k, v in zip(keys, values)})
        except Exception:
            continue
        if rows:
            cells = [c.model_dump(mode="json") for c in t.data.table_cells] if getattr(t, "data", None) else []
            tables.append(DocTable(page=_page_of(t), section="", columns=cols, rows=rows, provenance=_provenance(t), cells=cells))
    return tables


def _extract_chunks_and_outline(doc: Any) -> tuple[list[DocChunk], list[str]]:
    """Group body text by heading; retain visible source furniture separately."""
    outline: list[str] = []
    chunks: list[DocChunk] = []
    heading_stack: list[str] = []
    buf: list[str] = []
    buf_page: int | None = None
    buf_prov: list[dict[str, Any]] = []
    idx = 0

    def section_path() -> str:
        return " > ".join(heading_stack[-3:])

    def flush(kind="text") -> None:
        nonlocal buf, buf_page, buf_prov, idx
        text = "\n".join(s.strip() for s in buf if s.strip()).strip()
        buf = []
        if not text:
            buf_page = None
            return
        # split oversized sections into overlapping ~_CHUNK_CHARS windows
        start = 0
        while start < len(text):
            piece = text[start : start + _CHUNK_CHARS]
            chunks.append(
                DocChunk(index=idx, text=piece.strip(), page=buf_page,
                         section="" if kind=="document_metadata" else section_path(), kind=kind, provenance=list(buf_prov))
            )
            idx += 1
            if start + _CHUNK_CHARS >= len(text):
                break
            start += _CHUNK_CHARS - _CHUNK_OVERLAP
        buf_page = None
        buf_prov = []

    if hasattr(doc,"iterate_items"):
        from docling_core.types.doc import ContentLayer
        # Defaults omit furniture and OCR text nested inside pictures.
        texts = [item for item,_ in doc.iterate_items(traverse_pictures=True, included_content_layers={ContentLayer.BODY,ContentLayer.FURNITURE})]
    else:
        texts = getattr(doc,"texts",None)
    if texts:
        for item in texts:
            label = str(getattr(item, "label", "") or "").lower()
            text = str(getattr(item, "text", "") or "").strip()
            if not text:
                continue
            if getattr(item,"content_layer",None)=="furniture":
                flush()
                buf,buf_page,buf_prov = [text],_page_of(item),_provenance(item)
                flush("document_metadata")
                continue
            is_heading = "header" in label or label in ("title", "section_header")
            if is_heading:
                flush()
                # maintain a shallow heading stack (title resets)
                if label == "title":
                    heading_stack[:] = [text]
                else:
                    if heading_stack:
                        heading_stack[-1] = text
                    else:
                        heading_stack.append(text)
                if text not in outline:
                    outline.append(text)
                # Keep headings with their following text, without crossing a physical page.
                buf = [text]
                buf_page = _page_of(item)
                buf_prov = _provenance(item)
            else:
                if buf and _page_of(item) != buf_page:
                    flush()
                if buf_page is None:
                    buf_page = _page_of(item)
                buf.append(text)
                buf_prov.extend(_provenance(item))
        flush()

    return chunks, outline


def _chunks_from_markdown(md: str) -> tuple[list[DocChunk], list[str]]:
    """Fallback chunker: split markdown on headings, then size-window."""
    outline: list[str] = []
    chunks: list[DocChunk] = []
    idx = 0
    current = ""
    section_buf: list[str] = []

    def flush() -> None:
        nonlocal section_buf, idx
        text = "\n".join(section_buf).strip()
        section_buf = []
        if not text:
            return
        start = 0
        while start < len(text):
            piece = text[start : start + _CHUNK_CHARS]
            chunks.append(DocChunk(index=idx, text=piece.strip(), page=None, section=current))
            idx += 1
            if start + _CHUNK_CHARS >= len(text):
                break
            start += _CHUNK_CHARS - _CHUNK_OVERLAP

    for line in md.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            flush()
            current = m.group(1).strip()
            if current and current not in outline:
                outline.append(current)
        else:
            section_buf.append(line)
    flush()
    return chunks, outline


def _parse_pdf_textonly(data: bytes, filename: str, *, allow_empty: bool = False) -> ParsedDocument:
    """Low-memory fallback: extract text per page with pypdfium2 (NO rasterization,
    NO layout model), so large PDFs that OOM Docling's image pipeline still yield
    usable narrative text. Loses tables/section structure — labeled accordingly."""
    import pypdfium2 as pdfium

    digest = sha256_of(data)
    chunks: list[DocChunk] = []
    md_parts: list[str] = []
    page_count = 0
    image_pages = []
    empty_pages = []
    text_quality_pages = []
    idx = 0
    pdf = pdfium.PdfDocument(data)
    try:
        page_count = len(pdf)
        if page_count > 1000:
            raise ValueError("PDF exceeds the 1,000-page limit")
        for pno in range(page_count):
            page = pdf[pno]
            try:
                if any(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE])):
                    image_pages.append(pno + 1)
                tp = page.get_textpage()
                text = (tp.get_text_range() or "").strip()
                tp.close()
            except Exception:
                text = ""
            finally:
                page.close()
            if not text:
                empty_pages.append(pno + 1)
                continue
            if _BAD_GLYPHS.search(text):
                text_quality_pages.append(pno + 1)
            md_parts.append(text)
            start = 0
            while start < len(text):
                piece = text[start : start + _CHUNK_CHARS]
                chunks.append(
                    DocChunk(index=idx, text=piece.strip(), page=pno + 1, section="", provenance=[{"page_no": pno+1, "charspan": [start, start+len(piece)], "origin": "native_text"}])
                )
                idx += 1
                if start + _CHUNK_CHARS >= len(text):
                    break
                start += _CHUNK_CHARS - _CHUNK_OVERLAP
    finally:
        pdf.close()

    if not chunks and not allow_empty:
        raise ValueError("No readable text found. This PDF needs OCR; use a searchable PDF for document questions.")

    return ParsedDocument(
        filename=filename,
        sha256=digest,
        parser="pypdfium2-text",
        page_count=page_count,
        title=filename or "document",
        markdown="\n\n".join(md_parts),
        sections=[],
        tables=[],
        chunks=chunks,
        # shortcut: bounded PDFs live in JSON for byte recovery; move bytes to object storage for larger corpora.
        raw={"parser_version": PARSER_VERSION, "parser": "pypdfium2-text", "pages": page_count, "image_pages": image_pages, "empty_pages": empty_pages, "text_quality_pages": text_quality_pages, "unreadable_pages": empty_pages, "original_pdf_b64": base64.b64encode(data).decode("ascii")},
    )


def parse_pdf_bytes(data: bytes, filename: str, *, ocr: bool = False) -> ParsedDocument:
    """Parse a PDF (bytes) into a structured, provenance-tagged ParsedDocument.

    Blocking + heavy (loads models). Call via `parse_pdf` to run off-thread. If
    Docling raises or returns almost no content (e.g. rasterization OOM on a huge
    report), fall back to a low-memory text-only parse so the document is never
    lost or ingested empty.
    """
    import tempfile
    from pathlib import Path

    digest = sha256_of(data)

    # Master switch: on constrained hosts Docling/torch is disabled entirely and
    # every PDF goes through the low-memory text-only extractor.
    if not _docling_enabled() or not ocr:
        return _parse_pdf_textonly(data, filename)

    # Large PDFs OOM Docling's page-rasterization pipeline — and a native
    # std::bad_alloc can hard-crash the process (it's not a catchable Python
    # exception). So for big documents we skip the image pipeline entirely and
    # use the low-memory text extractor, which still yields narrative text for IE
    # (losing only table structure). Page count is read cheaply (no rendering).
    try:
        import pypdfium2 as pdfium

        _pdf = pdfium.PdfDocument(data)
        _pages = len(_pdf)
        _pdf.close()
        if _pages > _MAX_DOCLING_PAGES:
            return _parse_pdf_textonly(data, filename)
    except Exception:
        pass

    try:
        converter = _get_converter(ocr)
        # Docling wants a path/stream; a temp file is the most version-robust input.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / (filename or "document.pdf")
            p.write_bytes(data)
            result = converter.convert(str(p))
        doc = result.document
        try:
            markdown = doc.export_to_markdown()
        except Exception:
            markdown = ""
        try:
            raw = doc.export_to_dict()
        except Exception:
            raw = {}
        page_count = 0
        try:
            page_count = len(getattr(doc, "pages", {}) or {})
        except Exception:
            pass
        title = str(getattr(doc, "name", "") or filename or "document")
        tables = _extract_tables(doc)
        chunks, outline = _extract_chunks_and_outline(doc)
        parser = "docling"
        if not chunks:  # text-item walk yielded nothing → try markdown split
            chunks, outline = _chunks_from_markdown(markdown)
            parser = "docling-md"

        # If Docling produced almost nothing on a multi-page doc (rasterization
        # failures / std::bad_alloc leave empty pages), recover the text cheaply.
        degraded = (len(chunks) == 0 or len(markdown.strip()) < 400) and page_count > 2
        if degraded:
            fb = _parse_pdf_textonly(data, filename)
            if fb.chunks:
                return fb

        raw["original_pdf_b64"] = base64.b64encode(data).decode("ascii")
        return ParsedDocument(
            filename=filename,
            sha256=digest,
            parser=parser,
            page_count=page_count,
            title=title,
            markdown=markdown,
            sections=outline,
            tables=tables,
            chunks=chunks,
            raw=raw,
        )
    except Exception:
        # Docling failed outright (OOM, bad model, corrupt render) — never lose
        # the document; extract what text we can with near-zero memory.
        return _parse_pdf_textonly(data, filename)


_ocr_worker = None
_ocr_lock = asyncio.Lock()
_ocr_ready = False
_ocr_error = None


def docling_status() -> dict[str, Any]:
    alive = _ocr_worker is not None and _ocr_worker.returncode is None
    enabled = _docling_enabled()
    state = "disabled" if not enabled else (
        "busy" if alive and _ocr_ready and _ocr_lock.locked() else
        "ready" if alive and _ocr_ready else
        "warming" if alive else "failed" if _ocr_error else "stopped"
    )
    settings = configured_options()
    return {"enabled": enabled, "ok": alive and _ocr_ready, "state": state,
            "pid": _ocr_worker.pid if alive else None, "device": "cpu",
            "threads": settings.threads, "stage_batch_pages": max(settings.ocr_batch_size, settings.layout_batch_size, settings.table_batch_size), "queue_max_size": settings.queue_max_size,
            "pipeline_options": settings.model_dump(),
            "ocr": "RapidOCR / ONNX Runtime", "error_type": _ocr_error}


async def close_docling_worker() -> None:
    global _ocr_worker, _ocr_ready
    if _ocr_worker and _ocr_worker.returncode is None:
        _ocr_worker.kill()
        await _ocr_worker.wait()
    _ocr_worker = None
    _ocr_ready = False


async def _start_docling_worker() -> None:
    global _ocr_worker, _ocr_ready, _ocr_error
    import sys
    from pathlib import Path
    if _ocr_worker and _ocr_worker.returncode is None:
        return
    _ocr_ready = False
    _ocr_error = None
    _ocr_worker = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.services.document_worker", "--server",
        cwd=Path(__file__).resolve().parents[2],
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        ready = await asyncio.wait_for(_ocr_worker.stdout.readline(), timeout=180)
        if ready.strip() != b"ATLAS_DOCLING_READY":
            raise ValueError("Docling OCR worker could not initialize; verify full dependencies and model downloads")
        _ocr_ready = True
    except BaseException as exc:
        _ocr_error = type(exc).__name__
        await close_docling_worker()
        raise


async def warm_docling_worker() -> None:
    if _docling_enabled():
        async with _ocr_lock:
            await _start_docling_worker()


async def _docling_pages(source: str, output: str, pages: list[int], *, force_ocr_pages: list[int] | None = None) -> None:
    global _ocr_error
    import json
    async with _ocr_lock:
        try:
            await _start_docling_worker()
            _ocr_worker.stdin.write((json.dumps({"source":source,"output":output,"pages":pages,"force_ocr_pages":force_ocr_pages or []}) + "\n").encode())
            await _ocr_worker.stdin.drain()
            done = await asyncio.wait_for(_ocr_worker.stdout.readline(), timeout=300)
            if done.strip() != b"ATLAS_DOCLING_DONE":
                raise ValueError("Docling OCR worker stopped; the durable PDF remains available for retry")
        except BaseException as exc:
            _ocr_error = type(exc).__name__
            await close_docling_worker()
            raise


def _source_pdf(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """Keep source image bytes; convert a validated PNG/JPEG into one OCR page."""
    if b'%PDF-' in data[:1024]:
        return data, {}
    import io
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in {'PNG','JPEG'} or image.width*image.height > 20_000_000:
            raise ValueError('Only PNG/JPEG images up to 20 megapixels are supported')
        mime = 'image/png' if image.format == 'PNG' else 'image/jpeg'
        output = io.BytesIO()
        upright = ImageOps.exif_transpose(image).convert('RGBA')
        paper = Image.new('RGBA', upright.size, 'white')
        Image.alpha_composite(paper,upright).convert('RGB').save(output,format='PDF',resolution=150)
    return output.getvalue(), {'original_image_b64':base64.b64encode(data).decode('ascii'), 'source_mime':mime,'source_sha256':sha256_of(data)}


async def parse_pdf_fast(data: bytes, filename: str) -> ParsedDocument:
    """Native text plus original bytes, suitable for durable save before OCR."""
    pdf, source = await asyncio.to_thread(_source_pdf,data)
    parsed = await asyncio.to_thread(_parse_pdf_textonly, pdf, filename, allow_empty=True)
    if source:
        parsed.sha256 = source['source_sha256']
        parsed.raw.update(source)
    return parsed


async def parse_pdf(data: bytes, filename: str, *, ocr: bool = False) -> ParsedDocument:
    """Parse layout and tables for every page, independently of asset extraction."""
    import json
    import tempfile
    from pathlib import Path

    original = data
    data, source = await asyncio.to_thread(_source_pdf,data)
    parsed = await asyncio.to_thread(_parse_pdf_textonly, data, filename, allow_empty=True)
    if source:
        parsed.sha256 = sha256_of(original)
        parsed.raw.update(source)
    if not _docling_enabled():
        parsed.raw["parse_warnings"] = ["Layout, table structure and OCR unavailable; native text only"]
        if parsed.raw.get('text_quality_pages'):
            parsed.raw['parse_warnings'].append('Some extracted text has undecodable or control glyphs; inspect the original pages')
        if not parsed.chunks:
            raise ValueError("No native text; layout/OCR is disabled. Use original-page input with a supported visual model or remote OCR.")
        return parsed
    if parsed.page_count:
        selected = list(range(1, parsed.page_count + 1))
        # Named files stay in this backend folder; only the isolated worker loads native ML models.
        input_path = output_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", dir=Path(__file__).parents[2], delete=False) as f:
                f.write(data); input_path = Path(f.name)
            with tempfile.NamedTemporaryFile(suffix=".json", dir=Path(__file__).parents[2], delete=False) as f:
                output_path = Path(f.name)
            enriched = {"chunks": [], "tables": [], "sections": [], "documents": []}
            for start in range(len(selected)):
                pages = selected[start:start+1]
                scans = set(parsed.raw.get("image_pages", [])).intersection(parsed.raw.get("empty_pages", []))
                damaged = sorted(set(pages).intersection(set(parsed.raw.get("text_quality_pages", [])) | scans))
                await _docling_pages(str(input_path), str(output_path), pages, force_ocr_pages=damaged)
                batch = json.loads(output_path.read_text(encoding="utf-8"))
                if "error_type" in batch:
                    import logging
                    logging.getLogger("uvicorn.error").warning("Docling worker failed (%s), status=%s, pages=%s; source-free frames: %s", batch['error_type'], batch.get('conversion_status'), batch.get('physical_pages'), batch.get('error_frames', []))
                    raise ValueError(f"Docling processing failed ({batch['error_type']}); original and native text retained for retry")
                for key in enriched:
                    enriched[key].extend(batch.get(key, []))
            replacement = [DocChunk(**c) for c in enriched["chunks"]]
            native_numbers = {}
            for chunk in parsed.chunks:
                native_numbers.setdefault(chunk.page, set()).update(re.findall(r"\d+(?:\.\d+)?", chunk.text))
            replaced_pages = {c.page for c in replacement}
            parsed.chunks = [c for c in parsed.chunks if c.page not in replaced_pages] + replacement
            parsed.chunks.sort(key=lambda c: (c.page or 0, c.index))
            for i, chunk in enumerate(parsed.chunks): chunk.index = i
            parsed.tables = [DocTable(**t) for t in enriched["tables"]]
            parsed.sections = enriched["sections"]
            parsed.markdown = "\n\n".join(c.text for c in parsed.chunks)
            parsed.parser = "pypdfium2+docling"
            parsed.raw["docling_documents"] = enriched["documents"]
            parsed.raw["layout_pages"] = selected
            parsed.raw["ocr_pages"] = sorted(set(parsed.raw.get("image_pages", []) + parsed.raw.get("text_quality_pages", [])))
            parsed.raw["unreadable_pages"] = sorted(set(selected) - replaced_pages)
            parsed.raw["text_quality_pages"] = sorted({c.page for c in parsed.chunks if _BAD_GLYPHS.search(c.text) and c.page})
            parsed.raw["parse_warnings"] = []
            parsed.raw["parse_warnings"].extend("Docling layout/OCR partially completed on pages " + ", ".join(map(str,d['physical_pages']))
                                              for d in enriched['documents'] if d.get('status', 'success') != 'success')
            if parsed.raw["unreadable_pages"]:
                parsed.raw["parse_warnings"].append("Some pages yielded no layout text; inspect the originals")
            if parsed.raw["text_quality_pages"]:
                parsed.raw["parse_warnings"].append("Some extracted text has undecodable glyphs; inspect the original pages")
            if any(not str(column).strip() or len(table.columns) != len(set(table.columns)) for table in parsed.tables for column in table.columns):
                parsed.raw["parse_warnings"].append("Some table headers are missing or repeated; verify column relationships on the original pages")
            parsed.raw["parse_warnings"].extend(f"Page {table.page}: {warning}" for table in parsed.tables for warning in table.warnings)
            extracted_numbers = {}
            for chunk in parsed.chunks:
                extracted_numbers.setdefault(chunk.page, set()).update(re.findall(r"\d+(?:\.\d+)?", chunk.text))
            parsed.raw["numeric_discrepancy_pages"] = sorted(page for page, numbers in native_numbers.items() if page and not numbers.issubset(extracted_numbers.get(page, set())))
            if parsed.raw["numeric_discrepancy_pages"]:
                parsed.raw["parse_warnings"].append("Layout/OCR omitted or changed native numeric tokens on pages " + ", ".join(map(str, parsed.raw["numeric_discrepancy_pages"])))
        except asyncio.TimeoutError:
            raise ValueError("Docling processing exceeded five minutes; split this scan into smaller PDFs.") from None
        finally:
            for path in (input_path, output_path):
                if path: path.unlink(missing_ok=True)
    if not parsed.chunks:
        raise ValueError("No readable text found after parsing/OCR. Blank pages, handwriting or diagrams may need manual transcription.")
    return parsed


# --- (de)serialization for durable storage ---------------------------------
def parsed_to_dict(doc: ParsedDocument) -> dict[str, Any]:
    """Serialize a ParsedDocument to a JSON-safe dict (durable custody record)."""
    return {
        "filename": doc.filename,
        "sha256": doc.sha256,
        "parser": doc.parser,
        "page_count": doc.page_count,
        "title": doc.title,
        "markdown": doc.markdown,
        "sections": list(doc.sections),
        "tables": [asdict(t) for t in doc.tables],
        "chunks": [asdict(c) for c in doc.chunks],
        "raw": doc.raw,
    }


def parsed_from_dict(d: dict[str, Any]) -> ParsedDocument:
    """Reconstruct a ParsedDocument from its stored dict (recovery / reprocess)."""
    return ParsedDocument(
        filename=d.get("filename", ""),
        sha256=d.get("sha256", ""),
        parser=d.get("parser", ""),
        page_count=int(d.get("page_count", 0) or 0),
        title=d.get("title", ""),
        markdown=d.get("markdown", ""),
        sections=list(d.get("sections", []) or []),
        tables=[
            DocTable(
                page=t.get("page"),
                section=t.get("section", ""),
                columns=list(t.get("columns", []) or []),
                rows=list(t.get("rows", []) or []),
                provenance=list(t.get("provenance", []) or []),
                cells=list(t.get("cells", []) or []),
                warnings=list(t.get("warnings", []) or []),
            )
            for t in (d.get("tables") or [])
        ],
        chunks=[
            DocChunk(
                index=int(c.get("index", i)),
                text=c.get("text", ""),
                page=c.get("page"),
                section=c.get("section", ""),
                kind=c.get("kind", "text"),
                provenance=list(c.get("provenance", []) or []),
            )
            for i, c in enumerate(d.get("chunks") or [])
        ],
        raw=d.get("raw") or {},
    )
