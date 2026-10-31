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

from app.services.documents import docling_threads


def worker_environment(threads):
    threads = str(docling_threads(threads))
    return {**os.environ, "HF_HUB_DISABLE_IMPLICIT_TOKEN":"1", "OMP_NUM_THREADS":threads,
            "DOCLING_NUM_THREADS":threads, "TOKENIZERS_PARALLELISM":"false"}


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--pages", type=pages_arg, required=True)
    parser.add_argument("--threads", type=int, default=2, help="Inference threads; at most the reported process CPU count")
    parser.add_argument("--output", type=Path, required=True, help="New output directory; never overwritten")
    args = parser.parse_args()
    if os.environ.get("CODESPACES") != "true" or sys.platform != "linux":
        parser.error("Run this job in the student Codespace after verifying its free allowance")
    try:
        env = worker_environment(args.threads)
    except ValueError as exc:
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
        with (source.parent/"worker.log").open("wb") as log:
            process = subprocess.Popen([sys.executable, "-m", "app.services.document_worker", str(source), str(output), json.dumps(args.pages)],
                cwd=Path(__file__).resolve().parent, env=env, stdout=log, stderr=subprocess.STDOUT)
            report = supervise(process)
        report.update(source_sha256=original_hash, physical_pages=args.pages, output_present=output.is_file(),
                      inference_threads=args.threads, available_cpus=os.process_cpu_count() or 1)
        if sha256(source) != original_hash:
            report["failure"] = "source_changed"
        if output.is_file():
            try:
                data = json.loads(output.read_text(encoding="utf-8"))
                report["failure"] = report["failure"] or data.get("error_type")
                report["statuses"] = [item["status"] for item in data.get("documents", [])]
                completed = sorted(page for item in data.get("documents", []) if item["status"] == "success" for page in item["physical_pages"])
                if completed != args.pages or not data.get("chunks"):
                    report["failure"] = report["failure"] or "incomplete_pages"
            except (ValueError, KeyError, TypeError):
                report["failure"] = report["failure"] or "invalid_worker_output"
        from importlib.metadata import version
        report["versions"] = {name:version(name) for name in ("docling", "docling-parse", "torch", "rapidocr")}
        (source.parent/"manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return int(bool(report["failure"] or report["exit_code"] or not output.is_file()))


if __name__ == "__main__":
    raise SystemExit(main())
