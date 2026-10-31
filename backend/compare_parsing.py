"""Opt-in Codespaces comparison on identical pages; never promotes a configuration."""
import argparse
import json
import os
from pathlib import Path
from statistics import median
import subprocess
import sys

from app.services.docling_options import DoclingOptions, docling_threads, load_options
from app.services.document_validation import Expectations, compare_results, read_json
from docling_job import pages_arg, sha256, supervise


def profiles(base):
    """Change threads first, then the stage-batch setting; keep extraction modes."""
    result = {}
    for name, threads, batch in (("baseline",2,1),("threads4",4,1),("batch2",4,2),("batch4",4,4)):
        result[name] = DoclingOptions.model_validate({**base.model_dump(),"threads":threads,
            **{key:batch for key in ("ocr_batch_size","layout_batch_size","table_batch_size")}})
    return result


def compare_run(baseline, candidate):
    return compare_results(read_json(baseline/"document.json"),read_json(candidate/"document.json"),
                           read_json(baseline/"validation.json"),read_json(candidate/"validation.json"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source",type=Path)
    parser.add_argument("--pages",type=pages_arg,required=True)
    parser.add_argument("--config",type=Path,default=Path(__file__).resolve().parents[1]/"deploy/docling.json")
    parser.add_argument("--expectations",type=Path)
    parser.add_argument("--repeats",type=int,choices=(1,2,3),default=2)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--plan-only",action="store_true")
    args = parser.parse_args()
    candidates = profiles(load_options(args.config))
    if not args.source.is_file() or args.source.stat().st_size > 50*1024**2:
        parser.error("Select one original PDF of at most 50 MiB")
    code = [Path(__file__),Path(__file__).with_name("docling_job.py"),
            *(Path(__file__).parent/"app/services"/name for name in ("documents.py","document_worker.py","docling_options.py","document_validation.py"))]
    code_hashes = lambda: {path.name:sha256(path) for path in code}
    if args.expectations:
        Expectations.model_validate(read_json(args.expectations,1024**2))
    plan = {"source_sha256":sha256(args.source),"physical_pages":args.pages,
            "expectations_sha256":sha256(args.expectations) if args.expectations else None,"code_sha256":code_hashes(),
            "configurations":{name:options.model_dump() for name,options in candidates.items()},
            "repeats":args.repeats,"warmup_runs":1,"total_jobs":1+len(candidates)*args.repeats,
            "per_job_deadline_seconds":960,"automatic_promotion":False,
            "scope":"Source-matched parser diagnostics; no inference API, retrieval or chatbot evaluation"}
    if args.plan_only:
        print(json.dumps(plan,indent=2))
        return 0
    if os.environ.get("CODESPACES") != "true" or sys.platform != "linux":
        parser.error("Run the comparison in the student Codespace, not the laptop")
    try:
        docling_threads(4)
    except ValueError as exc:
        parser.error(str(exc))
    args.output.mkdir(parents=True,exist_ok=False)
    directory = args.output.resolve()
    report = {**plan,"runs":[],"summary":{},"human_review_required":True}
    def save():
        (directory/"comparison.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    save()
    baseline = None
    schedule = [("warmup",candidates["baseline"])]
    for repeat in range(args.repeats):
        names = list(candidates) if repeat%2==0 else list(reversed(candidates))
        schedule.extend((f"{name}-{repeat+1}",candidates[name]) for name in names)
    for name,options in schedule:
        if sha256(args.source) != plan["source_sha256"]:
            report["stopped"] = "original_source_changed"
            save()
            return 1
        if code_hashes() != plan["code_sha256"] or (args.expectations and sha256(args.expectations) != plan["expectations_sha256"]):
            report["stopped"] = "parser_code_or_labels_changed"
            save()
            return 1
        profile = directory/f"{name}.json"
        profile.write_text(options.model_dump_json(indent=2),encoding="utf-8")
        output = directory/name
        command = [sys.executable,str(Path(__file__).with_name("docling_job.py")),str(args.source.resolve()),
                   "--pages",",".join(map(str,args.pages)),"--config",str(profile),"--output",str(output)]
        if args.expectations:
            command.extend(["--expectations",str(args.expectations.resolve())])
        with (directory/f"{name}.log").open("wb") as log:
            env = {**os.environ,"LANGSMITH_TRACING":"false","LANGCHAIN_TRACING_V2":"false"}
            if name != "warmup":
                env.update(HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1")
            process = subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,env=env)
            control = supervise(process,seconds=960)
        run = {"name":name,"control":control,"configuration":options.model_dump()}
        report["runs"].append(run)
        if (output/"manifest.json").is_file():
            run["manifest"] = read_json(output/"manifest.json")
        if control["exit_code"] or control["failure"] or "manifest" not in run or run["manifest"].get("failure"):
            report["stopped"] = f"{name}: execution or validation failed; retained artifacts; no retry"
            save()
            return 1
        manifest = run["manifest"]
        if (manifest["source_sha256"] != plan["source_sha256"] or manifest["physical_pages"] != args.pages
                or manifest.get("pipeline_options") != options.model_dump()):
            report["stopped"] = "source_pages_or_configuration_mismatch"
            save()
            return 1
        if code_hashes() != plan["code_sha256"] or (args.expectations and sha256(args.expectations) != plan["expectations_sha256"]):
            report["stopped"] = "parser_code_or_labels_changed"
            save()
            return 1
        if report["runs"][0]["manifest"]["versions"] != manifest["versions"]:
            report["stopped"] = "parser_versions_changed"
            save()
            return 1
        if name != "warmup":
            if baseline is None:
                baseline = output
            run["retention"] = compare_run(baseline,output)
        save()
    for name in candidates:
        runs = [run for run in report["runs"] if run["name"].startswith(name+"-")]
        report["summary"][name] = {"median_total_seconds":median(r["manifest"]["total_seconds"] for r in runs),
            "max_peak_rss_mib":max(r["control"]["peak_rss_mib"] for r in runs),
            "retention_checks_pass":all(r["retention"]["mechanical_retention_pass"] for r in runs),
            "labelled_checks_pass":all(r["retention"]["labelled_checks_pass"] for r in runs)}
    duration = report["summary"]["baseline"]["median_total_seconds"]
    for name,item in report["summary"].items():
        item["speedup_vs_baseline"] = duration/item["median_total_seconds"]
        item["pages_per_minute"] = 60*len(args.pages)/item["median_total_seconds"]
        item["candidate_for_human_review"] = name != "baseline" and item["speedup_vs_baseline"]>1 and item["retention_checks_pass"] and item["labelled_checks_pass"]
    report["completed"] = True
    save()
    print(json.dumps({"completed":True,"jobs":len(report["runs"]),"summary":report["summary"],"automatic_promotion":False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
