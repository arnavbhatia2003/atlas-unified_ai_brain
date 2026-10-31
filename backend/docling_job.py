"""One bounded CPU parsing job in an explicitly funded student Codespace.

No database, model API credential, web service or .env loader is used here.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import psutil

from app.services.docling_options import DoclingOptions, docling_threads, load_options
from app.services.document_validation import Expectations, inspect_result, read_json


def worker_environment(threads, options=None):
    options = DoclingOptions.model_validate({**(options or DoclingOptions()).model_dump(), "threads":docling_threads(threads)})
    return {**os.environ, "HF_HUB_DISABLE_IMPLICIT_TOKEN":"1", "OMP_NUM_THREADS":str(options.threads),
            "DOCLING_NUM_THREADS":str(options.threads), "ATLAS_DOCLING_OPTIONS":options.model_dump_json(),
            "TOKENIZERS_PARALLELISM":"false", "LANGSMITH_TRACING":"false", "LANGCHAIN_TRACING_V2":"false"}


def pages_arg(value):
    pages = [int(item) for item in value.split(",")]
    if not pages or len(pages) > 10 or len(set(pages)) != len(pages) or min(pages) < 1:
        raise argparse.ArgumentTypeError("Select 1–10 unique physical page numbers")
    return sorted(pages)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stop(process):
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for item in [*children, parent]:
            try:
                item.kill()
            except psutil.NoSuchProcess:
                pass
    except psutil.NoSuchProcess:
        pass
    process.wait()


def supervise(process, *, seconds=900, memory_mib=4096):
    started, peak = time.monotonic(), 0
    failure = None
    try:
        while process.poll() is None:
            try:
                parent = psutil.Process(process.pid)
                rss = sum(item.memory_info().rss for item in [parent, *parent.children(recursive=True)] if item.is_running())
            except psutil.NoSuchProcess:
                continue
            peak = max(peak, rss)
            if rss > memory_mib*1024**2 or psutil.virtual_memory().available < 512*1024**2:
                failure = "memory_limit"
            elif time.monotonic()-started > seconds:
                failure = "time_limit"
            if failure:
                break
            time.sleep(.25)
    finally:
        if process.poll() is None:
            stop(process)
    return {"exit_code":process.returncode, "failure":failure,
            "seconds":round(time.monotonic()-started, 3), "peak_rss_mib":round(peak/1024**2, 1)}


def main():
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--pages", type=pages_arg, required=True)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1]/"deploy/docling.json")
    parser.add_argument("--threads", type=int, help="Override profile inference threads; at most the reported process CPU count")
    parser.add_argument("--batch-size", type=int, help="Override all three stage batch sizes (1–8)")
    parser.add_argument("--expectations", type=Path, help="Source-hash-bound text and table-cell labels")
    parser.add_argument("--output", type=Path, required=True, help="New output directory; never overwritten")
    args = parser.parse_args()
    if os.environ.get("CODESPACES") != "true" or sys.platform != "linux":
        parser.error("Run this job in the student Codespace after verifying its free allowance")
    try:
        values = load_options(args.config).model_dump()
        if args.threads is not None:
            values["threads"] = args.threads
        if args.batch_size is not None:
            values.update({key:args.batch_size for key in ("ocr_batch_size","layout_batch_size","table_batch_size")})
        options = DoclingOptions.model_validate(values)
        env = worker_environment(options.threads, options)
        expected = Expectations.model_validate(read_json(args.expectations,1024**2)) if args.expectations else None
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if not args.source.is_file() or args.source.stat().st_size > 50*1024**2:
        parser.error("Source must be one PDF of at most 50 MiB")
    if psutil.virtual_memory().available < 2*1024**3:
        parser.error("At least 2 GiB available memory is required before starting")
    import fcntl
    import tempfile
    with (Path(tempfile.gettempdir())/"atlas-docling-job.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another Docling job is running")
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(args.source) as pdf:
            if max(args.pages) > len(pdf):
                parser.error("A selected physical page exceeds the document length")
        args.output.mkdir(parents=True, exist_ok=False)
        source = args.output.resolve()/"source.pdf"
        shutil.copyfile(args.source, source)
        original_hash = sha256(source)
        output = source.parent/"document.json"
        (source.parent/"config.json").write_text(options.model_dump_json(indent=2),encoding="utf-8")
        with (source.parent/"worker.log").open("wb") as log:
            process = subprocess.Popen([sys.executable, "-m", "app.services.document_worker", str(source), str(output), json.dumps(args.pages)],
                cwd=Path(__file__).resolve().parent, env=env, stdout=log, stderr=subprocess.STDOUT)
            report = supervise(process)
        report.update(source_sha256=original_hash, physical_pages=args.pages, output_present=output.is_file(),
                      inference_threads=options.threads, available_cpus=os.process_cpu_count() or 1,
                      pipeline_options=options.model_dump(), config_sha256=sha256(args.config),
                      expectations_sha256=sha256(args.expectations) if args.expectations else None)
        if report["exit_code"] or not output.is_file():
            report["failure"] = report["failure"] or "worker_failed"
        if sha256(source) != original_hash:
            report["failure"] = "source_changed"
        if output.is_file():
            try:
                data = read_json(output)
                report["failure"] = report["failure"] or data.get("error_type")
                if data.get("pipeline_options") != options.model_dump():
                    raise ValueError("Worker configuration does not match the requested profile")
                report["statuses"] = [item["status"] for item in data.get("documents", [])]
                completed = sorted(page for item in data.get("documents", []) if item["status"] == "success" for page in item["physical_pages"])
                if completed != args.pages or not data.get("chunks"):
                    report["failure"] = report["failure"] or "incomplete_pages"
                evidence = read_json(source.parent/"source-evidence.json")
                if evidence.get("source_sha256") != original_hash:
                    raise ValueError("Source checkpoint hash mismatch")
                validation = inspect_result(data,evidence,args.pages,source.parent,expected)
                (source.parent/"validation.json").write_text(json.dumps(validation,indent=2,ensure_ascii=False),encoding="utf-8")
                report["validation"] = {"status":validation["status"],"errors":len(validation["errors"]),
                                        "warnings":len(validation["warnings"]),"human_review_required":True}
                if validation["errors"]:
                    report["failure"] = report["failure"] or "validation_failed"
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                report["failure"] = report["failure"] or "invalid_worker_output"
        from importlib.metadata import version
        report["versions"] = {name:version(name) for name in ("docling", "docling-core", "docling-parse", "pypdfium2", "torch", "rapidocr", "onnxruntime")}
        report["total_seconds"] = round(time.monotonic()-started,3)
        (source.parent/"manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return int(bool(report["failure"] or report["exit_code"] or not output.is_file()))


if __name__ == "__main__":
    raise SystemExit(main())
