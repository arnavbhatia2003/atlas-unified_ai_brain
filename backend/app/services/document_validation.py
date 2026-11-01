"""Mechanical source checks for parser experiments, not semantic certification."""
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field

TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)+|[+\-−–]?\d+(?:\.\d+)?|[A-Za-z]+")


class TextCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page: int = Field(ge=1)
    text: str = Field(min_length=1, pattern=r"\S")


class CellCheck(TextCheck):
    table_index: int = Field(ge=0)
    row: int = Field(ge=0)
    column: int = Field(ge=0)


class Expectations(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_url: str = ""
    label_origin: str = ""
    text_spans: list[TextCheck] = Field(default_factory=list)
    table_cells: list[CellCheck] = Field(default_factory=list)


def read_json(path: Path, limit=100*1024**2):
    if path.stat().st_size > limit:
        raise ValueError("Parser artifact exceeds the inspection size limit; retained for review")
    return json.loads(path.read_text(encoding="utf-8"))


def whitespace(text):
    return " ".join(text.split())


def numeric_sign_equivalent(expected, actual):
    """Diagnose only leading-minus glyphs in complete numeric literals; never repair evidence."""
    left, right = whitespace(expected), whitespace(actual)
    number = r"[+\-−–]?[0-9]+(?:\.[0-9]+)?"
    if not re.fullmatch(number,left) or not re.fullmatch(number,right):
        return False
    minus = str.maketrans({"−":"-","–":"-"})
    return left.translate(minus)==right.translate(minus)


def page_text(result):
    text = defaultdict(list)
    for chunk in result.get("chunks", []):
        text[chunk.get("page")].append(chunk.get("text", ""))
    for table in result.get("tables", []):
        text[table.get("page")].extend(cell.get("text", "") for cell in table.get("cells", []))
    return {page:"\n".join(parts) for page, parts in text.items()}


def table_cells(result):
    """Retain row/column relationships, spans, headers and literal cell values."""
    counts, cells = Counter(), []
    for table in result.get("tables", []):
        page = table.get("page")
        index = counts[page]
        counts[page] += 1
        for cell in table.get("cells", []):
            cells.append((page,index,cell.get("start_row_offset_idx"),cell.get("end_row_offset_idx"),
                          cell.get("start_col_offset_idx"),cell.get("end_col_offset_idx"),cell.get("text", ""),
                          cell.get("column_header",False),cell.get("row_header",False)))
    return cells


def page_provenance(result):
    values = []
    for kind in ("chunks","tables"):
        for item in result.get(kind,[]):
            values.extend((kind,item.get("page"),json.dumps(prov,sort_keys=True)) for prov in item.get("provenance",[]))
    for doc in result.get("documents",[]):
        mapping = doc.get("physical_pages",[])
        for picture in doc.get("document",{}).get("pictures",[]):
            for prov in picture.get("prov",[]) or [{}]:
                local = prov.get("page_no")
                page = mapping[local-1] if type(local) is int and 1 <= local <= len(mapping) else tuple(mapping)
                values.append(("picture",page,json.dumps(prov,sort_keys=True)))
    return values


def inspect_result(result, evidence, pages, directory: Path, expected: Expectations | None = None):
    errors, warnings = [], list(evidence.get("warnings", []))
    completed = [page for doc in result.get("documents", []) if doc.get("status") == "success" for page in doc.get("physical_pages", [])]
    if any(type(page) is not int for page in completed) or sorted(completed) != pages:
        errors.append("Conversion did not complete each selected physical page exactly once")
    native = {page["page"]:page["text"] for page in evidence.get("native_pages", [])}
    if any(type(page) is not int for page in native) or sorted(native) != pages or len(evidence.get("native_pages",[])) != len(pages):
        errors.append("Native source checkpoint is missing selected pages")
    text = page_text(result)
    missing = {}
    for page in pages:
        if not text.get(page, "").strip():
            errors.append(f"Page {page}: no extracted text; original requires review")
        source_tokens = set(TOKEN.findall(native.get(page, "")))
        missing[str(page)] = sorted(source_tokens-set(TOKEN.findall(text.get(page, ""))))
        if missing[str(page)]:
            warnings.append(f"Page {page}: extracted text omitted or changed native tokens")
        if not native.get(page, "").strip():
            warnings.append(f"Page {page}: no native text reference; OCR fidelity needs visual review")
    for item in [*result.get("chunks", []), *result.get("tables", [])]:
        if type(item.get("page")) is not int or item.get("page") not in pages:
            errors.append("Extracted item has absent or out-of-scope physical-page evidence")
        for prov in item.get("provenance", []):
            if type(prov.get("page_no")) is not int or prov.get("page_no") not in pages:
                errors.append("Provenance points outside selected physical pages")
        if not item.get("provenance"):
            warnings.append("An extracted item has no coordinate/span provenance; inspect its original page")
    for table in result.get("tables", []):
        warnings.extend(f"Page {table.get('page')}: {warning}" for warning in table.get("warnings", []))
        if not table.get("cells"):
            warnings.append(f"Page {table.get('page')}: table has no source-cell structure")
        for cell in table.get("cells", []):
            values = [cell.get(k) for k in ("start_row_offset_idx","end_row_offset_idx","start_col_offset_idx","end_col_offset_idx")]
            if any(type(v) is not int for v in values) or not (0 <= values[0] < values[1] and 0 <= values[2] < values[3]):
                errors.append("Table cell has invalid row/column bounds")
            bbox = cell.get("bbox")
            if bbox and any(type(bbox.get(k)) not in (int,float) or not math.isfinite(bbox[k]) for k in ("l","t","r","b")):
                errors.append("Table cell has invalid physical coordinates")
    figures = []
    for doc in result.get("documents", []):
        mapping = doc.get("physical_pages", [])
        for picture in doc.get("document", {}).get("pictures", []):
            for prov in picture.get("prov", []):
                local = prov.get("page_no")
                if type(local) is int and 1 <= local <= len(mapping):
                    figures.append(mapping[local-1])
    if figures:
        warnings.append("Detected pictures require visual/semantic review; diagram interpretation is not implemented")
    root = directory.resolve()
    if result.get("pipeline_options",{}).get("export_page_images") and sorted(image["page"] for image in evidence.get("page_images",[])) != pages:
        errors.append("Requested page previews are missing or duplicated")
    for image in evidence.get("page_images", []):
        path = (root/image["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            errors.append("Page preview is missing or has an unsafe artifact path")
            continue
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream,"sha256").hexdigest()
        if digest != image.get("sha256"):
            errors.append("Page preview hash mismatch")
    checks = []
    if expected:
        if expected.source_sha256 != evidence.get("source_sha256"):
            errors.append("Expected facts belong to a different original source hash")
        for check in expected.text_spans:
            checks.append({"kind":"text",**check.model_dump(),"passed":whitespace(check.text) in whitespace(text.get(check.page,""))})
        tables = defaultdict(list)
        for table in result.get("tables", []):
            tables[table.get("page")].append(table)
        for check in expected.table_cells:
            found = []
            if check.table_index < len(tables[check.page]):
                for cell in tables[check.page][check.table_index].get("cells", []):
                    if (cell.get("start_row_offset_idx",-1) <= check.row < cell.get("end_row_offset_idx",-1)
                            and cell.get("start_col_offset_idx",-1) <= check.column < cell.get("end_col_offset_idx",-1)):
                        found.append(cell.get("text", ""))
            checks.append({"kind":"cell",**check.model_dump(),"observed":found,
                           "passed":len(found)==1 and whitespace(found[0])==whitespace(check.text),
                           "numeric_sign_equivalent":len(found)==1 and numeric_sign_equivalent(check.text,found[0])})
        if not checks:
            errors.append("Expected-facts file contains no checks")
        if any(not item["passed"] for item in checks):
            errors.append("One or more source-labelled checks failed")
    sign_only = sum(not c["passed"] and c.get("numeric_sign_equivalent",False) for c in checks)
    return {"status":"failed" if errors else "needs_review" if warnings or not checks else "labelled_checks_passed",
            "check_summary":{"total":len(checks),"strict_passed":sum(c["passed"] for c in checks),
                             "numeric_sign_only_differences":sign_only,
                             "other_failures":sum(not c["passed"] for c in checks)-sign_only},
            "errors":list(dict.fromkeys(errors)),"warnings":list(dict.fromkeys(warnings)),"missing_native_tokens":missing,
            "labelled_checks":checks,"figure_review_pages":sorted(set(figures)),"human_review_required":True,
            "comparison_policy":"Whitespace-only acceptance; numeric sign equivalence is diagnostic and never changes passed/status",
            "scope":"Mechanical custody, coverage and labelled-value checks; not lossless or semantic certification"}


def compare_results(baseline, candidate, baseline_validation, candidate_validation):
    left, right = page_text(baseline), page_text(candidate)
    missing = {str(page):sorted(set(TOKEN.findall(text))-set(TOKEN.findall(right.get(page,"")))) for page,text in left.items()}
    lost_cells = Counter(table_cells(baseline))-Counter(table_cells(candidate))
    lost_provenance = Counter(page_provenance(baseline))-Counter(page_provenance(candidate))
    removed_warnings = sorted(set(baseline_validation["warnings"])-set(candidate_validation["warnings"]))
    label_pass = bool(candidate_validation["labelled_checks"]) and all(c["passed"] for c in candidate_validation["labelled_checks"])
    source_hash = baseline.get("source_evidence",{}).get("source_sha256", "")
    same_source = (bool(re.fullmatch(r"[a-f0-9]{64}",source_hash)) and bool(baseline.get("documents"))
                   and source_hash == candidate.get("source_evidence",{}).get("source_sha256")
                   and [d.get("physical_pages") for d in baseline.get("documents",[])] == [d.get("physical_pages") for d in candidate.get("documents",[])])
    return {"missing_baseline_tokens":{k:v for k,v in missing.items() if v},
            "lost_or_changed_cells":[{"cell":list(cell),"count":count} for cell,count in lost_cells.items()],
            "lost_page_or_picture_provenance":[{"evidence":list(item),"count":count} for item,count in lost_provenance.items()],
            "removed_warnings":removed_warnings,
            "same_source_pages":same_source,
            "mechanical_retention_pass":same_source and not any(missing.values()) and not lost_cells and not lost_provenance and not removed_warnings and not candidate_validation["errors"],
            "labelled_checks_pass":label_pass,"human_review_required":True,"automatic_promotion":False}


def main():
    """Reinspect a retained job without loading models or overwriting old reports."""
    import argparse
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("directory",type=Path)
    parser.add_argument("--expectations",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args()
    try:
        data = read_json(args.directory/"document.json")
        evidence = read_json(args.directory/"source-evidence.json")
        manifest = read_json(args.directory/"manifest.json")
        expected = Expectations.model_validate(read_json(args.expectations,1024**2))
        with (args.directory/"source.pdf").open("rb") as stream:
            digest = hashlib.file_digest(stream,"sha256").hexdigest()
        if any(value!=digest for value in (evidence.get("source_sha256"),manifest.get("source_sha256"),
                                          data.get("source_evidence",{}).get("source_sha256"))):
            raise ValueError("Retained source hashes disagree; inspect custody before rescoring")
        pages = manifest["physical_pages"]
        if not pages or any(type(p) is not int or p<1 for p in pages) or sorted(set(pages))!=pages:
            raise ValueError("Invalid retained physical-page selection")
        result = inspect_result(data,evidence,pages,args.directory,expected)
        result.update(source_sha256=digest,expectations_sha256=hashlib.sha256(args.expectations.read_bytes()).hexdigest(),
                      parser_rerun=False)
        with args.output.open("x",encoding="utf-8") as stream:
            json.dump(result,stream,indent=2,ensure_ascii=False)
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status":result["status"],"check_summary":result["check_summary"],
                      "errors":result["errors"],"warnings":result["warnings"],"output":str(args.output),"parser_rerun":False}))
    return int(bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
