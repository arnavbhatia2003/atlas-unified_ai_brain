"""Isolated Docling worker: a native parser/OCR crash must not kill the API."""
import json
import hashlib
import sys
import tempfile
from pathlib import Path
from dataclasses import asdict
from itertools import groupby
from statistics import median
import pypdfium2 as pdfium
from .documents import _get_converter, _extract_tables, _extract_chunks_and_outline, _word_ocr, DocChunk
from .docling_options import configured_options


def source_evidence(original, source, output, selected, options):
    """Checkpoint native text and optional previews before model conversion."""
    pages, images, warnings = [], [], []
    directory = Path(output).parent
    for number in selected:
        page = original[number-1]
        try:
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_range()
            finally:
                textpage.close()
            pages.append({"page":number,"text":text})
            if options.export_page_images:
                width, height = page.get_size()
                if width*height*options.page_image_scale**2 > 20_000_000:
                    warnings.append(f"Page {number}: preview exceeds 20 megapixels; original retained")
                    continue
                path = directory/"pages"/f"p{number:04d}.png"
                path.parent.mkdir(exist_ok=True)
                bitmap = page.render(scale=options.page_image_scale)
                try:
                    image = bitmap.to_pil()
                    try:
                        image.save(path, "PNG")
                        size = image.size
                    finally:
                        image.close()
                finally:
                    bitmap.close()
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                images.append({"page":number,"path":path.relative_to(directory).as_posix(),"sha256":digest,
                               "width":size[0],"height":size[1],"scale":options.page_image_scale})
        except Exception as exc:
            warnings.append(f"Page {number}: source export failed ({type(exc).__name__}); original retained")
        finally:
            page.close()
    with Path(source).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    evidence = {"source_sha256":digest,"native_pages":pages,"page_images":images,"warnings":warnings}
    (directory/"source-evidence.json").write_text(json.dumps(evidence,ensure_ascii=False),encoding="utf-8")
    return evidence


def _page_batches(selected, force_ocr_pages):
    forced = set(force_ocr_pages)
    for force_ocr, group in groupby(selected, key=lambda page: page in forced):
        pages = list(group)
        for start in range(0, len(pages), 10):
            yield pages[start:start+10], force_ocr


def _refine_ocr_table(table, image, reader, scale=3):
    """Re-read a simple OCR grid with separate crops; keep the first extraction in custody."""
    import numpy as np
    cells = table.cells
    if not cells or len(cells) > 256 or any(c.get('row_span', 1) != 1 or c.get('col_span', 1) != 1 or not c.get('bbox') or c['bbox'].get('coord_origin') != 'TOPLEFT' for c in cells):
        return
    rows = max(c['end_row_offset_idx'] for c in cells)
    cols = max(c['end_col_offset_idx'] for c in cells)
    grid = {(c['start_row_offset_idx'], c['start_col_offset_idx']):c for c in cells}
    if cols < 2 or rows < 2 or len(grid) != rows*cols or any((r,c) not in grid for r in range(rows) for c in range(cols)):
        return
    if not all(grid[0,c].get('column_header') for c in range(cols)) or any(c.get('column_header') for (r,_),c in grid.items() if r > 0):
        return
    xs = [min(c['bbox']['l'] for c in cells)-2]
    for col in range(1, cols):
        right = max(grid[row,col-1]['bbox']['r'] for row in range(rows))
        left = median(grid[row,col]['bbox']['l'] for row in range(rows))
        if right >= left:
            return  # shortcut: overlapping columns need source review, not a guessed crop boundary.
        xs.append((right+left)/2)
    xs.append(max(c['bbox']['r'] for c in cells)+2)
    ys = [min(c['bbox']['t'] for c in cells)-2]
    for row in range(1, rows):
        bottom = max(grid[row-1,col]['bbox']['b'] for col in range(cols))
        top = min(grid[row,col]['bbox']['t'] for col in range(cols))
        if bottom >= top:
            return
        ys.append((bottom+top)/2)
    ys.append(max(c['bbox']['b'] for c in cells)+2)
    refined = {}
    for (row,col), cell in grid.items():
        bounds = [max(0,int(xs[col]*scale)), max(0,int(ys[row]*scale)), min(image.width,int(xs[col+1]*scale)), min(image.height,int(ys[row+1]*scale))]
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            return
        crop = image.crop(bounds)
        try:
            result = reader(np.asarray(crop))
        finally:
            crop.close()
        text = ' '.join(result.txts or [])
        scores = [float(s) for s in result.scores or []]
        if (cell.get('text') and not text) or (scores and min(scores) < 0.7):
            text = cell.get('text','')
            table.warnings.append(f'Cell OCR uncertain at row {row+1}, column {col+1}; original cell text retained')
        refined[row,col] = {**cell, 'text':text, 'original_text':cell.get('text',''),
                            'ocr_crop_bbox':dict(zip(('l','t','r','b'), (v/scale for v in bounds))), 'ocr_scores':scores}
    columns = [refined[0,c]['text'] for c in range(cols)]
    keys = [c if c.strip() and columns.count(c) == 1 else f"{c or 'Unlabelled'} [column {i+1}]" for i,c in enumerate(columns)]
    table.columns = columns
    table.rows = [{keys[col]:refined[row,col]['text'] for col in range(cols)} for row in range(1,rows)]
    table.cells = [refined[key] for key in sorted(refined)]
    table.warnings.append('Table re-read from OCR cell crops; verify critical values against the original')


def convert_pages(source, output, selected, force_ocr_pages=()):
    chunks, tables, sections = [], [], []
    documents = []
    original = pdfium.PdfDocument(source)
    try:
        options = configured_options()
        evidence = source_evidence(original, source, output, selected, options)
        # Process bounded batches while reusing the warmed converter/models.
        for page_numbers, force_ocr in _page_batches(selected, force_ocr_pages):
            force_ocr = force_ocr or options.ocr_mode == "full"
            converter = _get_converter(True, force_ocr=force_ocr)
            subset = pdfium.PdfDocument.new()
            subset.import_pages(original, pages=[p-1 for p in page_numbers])
            with tempfile.NamedTemporaryFile(suffix=".pdf", dir=Path(source).parent, delete=False) as f:
                path = Path(f.name)
            try:
                subset.save(path); subset.close()
                result = converter.convert(str(path))
                status = str(result.status.value)
                if status not in ("success", "partial_success"):
                    Path(output).write_text(json.dumps({"error_type":"DoclingConversionIncomplete","conversion_status":status,"physical_pages":page_numbers}),encoding="utf-8")
                    return
                cs, outline = _extract_chunks_and_outline(result.document)
                ts = _extract_tables(result.document)
                if force_ocr:
                    reader = next(iter(converter.initialized_pipelines.values())).ocr_model.reader
                    if getattr(reader, 'func', None) is _word_ocr:
                        reader = reader.args[0]  # A cell crop has one column; retain line recognition and its line scores.
                    for table in ts:
                        if table.page is None:
                            continue
                        page = original[page_numbers[table.page-1]-1]
                        try:
                            bitmap = page.render(scale=3)
                            try:
                                image = bitmap.to_pil().copy()
                            finally:
                                bitmap.close()
                            try:
                                _refine_ocr_table(table, image, reader)
                                if not table.warnings:
                                    table.warnings.append('OCR table grid could not be re-read; verify column and row relationships against the original')
                            finally:
                                image.close()
                        finally:
                            page.close()
                for item in [*cs, *ts]:
                    item.page = page_numbers[item.page - 1] if item.page and item.page <= len(page_numbers) else None
                    for prov in item.provenance:
                        local = prov.get("page_no")
                        prov["page_no"] = page_numbers[local-1] if isinstance(local, int) and 1 <= local <= len(page_numbers) else None
                for table in ts:
                    row_texts = [" | ".join(f"{column}: {value}" for column,value in row.items()) for row in table.rows]
                    complete_table = "\n".join(row_texts)
                    if len(row_texts)>1 and len(complete_table)<=6000:
                        cs.append(DocChunk(index=len(cs),text=complete_table,page=table.page,section=table.section,kind="table",provenance=table.provenance))
                    for text in row_texts:
                        if text.strip():
                            cs.append(DocChunk(index=len(cs), text=text, page=table.page, section=table.section, kind="table", provenance=table.provenance))
                chunks.extend(asdict(c) for c in cs)
                tables.extend(asdict(t) for t in ts)
                sections.extend(outline)
                documents.append({"physical_pages": page_numbers, "status":status, "document": result.document.export_to_dict()})
            finally:
                path.unlink(missing_ok=True)
    finally:
        original.close()
    Path(output).write_text(json.dumps({"chunks":chunks,"tables":tables,"sections":sections,"documents":documents,
                                      "source_evidence":evidence,"pipeline_options":options.model_dump()}),encoding="utf-8")

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
                convert_pages(request["source"], request["output"], request["pages"], request.get("force_ocr_pages", []))
        except Exception as exc:
            import traceback
            frames = [{"file":Path(f.filename).name,"line":f.lineno,"function":f.name} for f in traceback.extract_tb(exc.__traceback__)]
            Path(request["output"]).write_text(json.dumps({"error_type":type(exc).__name__,"error_frames":frames}), encoding="utf-8")
        print("ATLAS_DOCLING_DONE", flush=True)

if __name__ == "__main__": main()
