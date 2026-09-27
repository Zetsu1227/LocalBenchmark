from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import canonical_hash, load_yaml
from .reporting import load_records, summarize, write_report, write_run_reports
from .runner import run_trial
from .tasks import discover_tasks, load_task


def _root() -> Path:
    return Path.cwd()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmark", description="Local SWE agent benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    tasks = sub.add_parser("tasks", help="List task catalog")
    tasks_sub = tasks.add_subparsers(dest="tasks_command", required=True)
    tasks_sub.add_parser("list", help="List available task IDs")
    run = sub.add_parser("run", help="Run repeated experimental trials")
    run.add_argument("--model", required=True, help="Model profile key in configs/models.yaml")
    run.add_argument("--task", help="Task ID")
    run.add_argument("--category", choices=["creation", "refactoring", "analysis"])
    run.add_argument("--runs", type=int, default=1)
    run.add_argument("--tasks-dir", type=Path, default=Path("tasks"))
    run.add_argument("--models-config", type=Path, default=Path("configs/models.yaml"))
    run.add_argument("--results", type=Path, default=Path("results"))
    run.add_argument("--reports", type=Path, default=Path("reports"),
                     help="Directory for per-model, per-task run reports")
    report = sub.add_parser("report", help="Export run-level and aggregate results")
    report.add_argument("--input", type=Path, default=Path("results"))
    report.add_argument("--format", choices=["json", "csv", "html"], default="json")
    report.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = _root()
    if args.command == "tasks":
        for path in discover_tasks((root / "tasks").resolve()):
            task = load_task(path)
            print(f"{task.task_id}\t{task.category}\t{path}")
        return 0
    if args.command == "run":
        if args.runs < 1:
            print("--runs must be at least 1", file=sys.stderr)
            return 2
        models = load_yaml((root / args.models_config).resolve()).get("models", {})
        if args.model not in models:
            print(f"Unknown model profile: {args.model}", file=sys.stderr)
            return 2
        selected = []
        for path in discover_tasks((root / args.tasks_dir).resolve()):
            task = load_task(path)
            if args.task and args.task != task.task_id:
                continue
            if args.category and args.category != task.category:
                continue
            selected.append(task)
        if args.task and not selected:
            print(f"Task not found: {args.task}", file=sys.stderr)
            return 2
        if not selected:
            print("No tasks selected", file=sys.stderr)
            return 2
        profile = models[args.model]
        results_root = (root / args.results).resolve()
        reports_root = (root / args.reports).resolve()
        for task in selected:
            exp_id = canonical_hash({"task": task.raw, "model_profile": profile,
                                     "model_profile_name": args.model,
                                     "benchmark_version": __version__})
            for index in range(args.runs):
                trial_profile = dict(profile)
                if profile.get("seed") is not None:
                    # Pair replicate n across model profiles while varying n within a profile.
                    trial_profile["seed"] = (int(profile["seed"]) + index) & 0x7FFFFFFF
                record = run_trial(task, args.model, trial_profile, results_root, exp_id)
                stored_records = load_records(results_root)
                related = [row for row in stored_records
                           if (row.get("model") or {}).get("profile") == args.model
                           and (row.get("task") or {}).get("id") == task.task_id
                           and row.get("experiment_id") == exp_id]
                report_paths = write_run_reports(record, related, reports_root)
                print(json.dumps({"run_id": record["run_id"], "task": task.task_id,
                                  "trial": index + 1, "status": record["status"],
                                  "success": record["success"], "experiment_id": exp_id,
                                  "report_html": str(report_paths["html"]),
                                  "report_json": str(report_paths["json"])}))
        records = load_records(results_root)
        out = results_root / "summary.json"
        out.write_text(json.dumps({"summary": summarize(records), "runs": records}, indent=2,
                                  ensure_ascii=False) + "\n", encoding="utf-8")
        return 0
    if args.command == "report":
        records = load_records((root / args.input).resolve())
        output = args.output or Path("reports") / f"report.{args.format}"
        print(write_report(records, root / output, args.format))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
