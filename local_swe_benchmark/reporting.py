from __future__ import annotations

import csv
import html
import json
import math
import re
import statistics
import hashlib
from pathlib import Path
from typing import Any


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * fraction
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] if lo == hi else ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    success = [bool(x.get("success")) for x in records if x.get("status") not in {"infrastructure_error", "evaluation_error"}]
    valid = len(success)
    numeric = {}
    for name, getter in {
        "duration_seconds": lambda x: x.get("duration_seconds"),
        "prompt_tokens": lambda x: (x.get("llm") or {}).get("prompt_tokens"),
        "completion_tokens": lambda x: (x.get("llm") or {}).get("completion_tokens"),
        "iterations": lambda x: (x.get("agent") or {}).get("iterations"),
        "tool_calls": lambda x: (x.get("agent") or {}).get("tool_calls"),
        "tokens_per_second": lambda x: (x.get("llm") or {}).get("tokens_per_second"),
        "peak_context": lambda x: (x.get("llm") or {}).get("peak_context"),
    }.items():
        vals = [float(v) for row in records if (v := getter(row)) is not None]
        numeric[name] = {"n": len(vals), "mean": statistics.mean(vals) if vals else None,
                         "median": statistics.median(vals) if vals else None,
                         "stdev": statistics.stdev(vals) if len(vals) > 1 else None,
                         "min": min(vals) if vals else None, "max": max(vals) if vals else None,
                         "p25": _percentile(vals, .25), "p75": _percentile(vals, .75),
                         "p95": _percentile(vals, .95)}
    self_corrected = 0
    correction_eligible = 0
    for row in records:
        agent = row.get("agent") or {}
        if agent.get("public_test_failures", 0) > 0:
            correction_eligible += 1
            self_corrected += bool(agent.get("self_corrected"))
    successful = [r for r in records if r.get("success")]
    success_count = len(successful)
    efficiency = {}
    for key, getter in {
        "tokens_per_successful_task": lambda r: (r.get("llm") or {}).get("total_tokens"),
        "time_per_successful_task_seconds": lambda r: r.get("duration_seconds"),
        "tool_calls_per_successful_task": lambda r: (r.get("agent") or {}).get("tool_calls"),
        "iterations_per_successful_task": lambda r: (r.get("agent") or {}).get("iterations"),
    }.items():
        observed = sum(getter(row) is not None for row in successful)
        efficiency[key] = {"mean": _sum_metric(successful, getter, observed),
                           "observed_successful_runs": observed, "all_successful_runs": success_count}
    public_run = _test_run_rate(records, "public_tests")
    hidden_run = _test_run_rate(records, "hidden_tests")
    ci = _wilson(sum(success), valid)
    return {"total_runs": len(records), "valid_runs": valid,
            "success_count": sum(success), "success_rate": sum(success) / valid if valid else None,
            "success_rate_wilson_95": ci,
            "invalid_runs": len(records) - valid, "metrics": numeric,
            "self_correction": {"corrected_runs": self_corrected,
                                "eligible_runs_with_test_failure": correction_eligible,
                                "rate": self_corrected / correction_eligible if correction_eligible else None},
            "efficiency": efficiency,
            "functional_correctness": {"public_test_run_pass_rate": public_run,
                                        "hidden_test_run_pass_rate": hidden_run,
                                        "public_test_case_pass_rate": _test_case_rate(records, "public_tests"),
                                        "hidden_test_case_pass_rate": _test_case_rate(records, "hidden_tests"),
                                        "regression_rate": (1 - public_run) if public_run is not None else None},
            "by_category": _group(records, lambda r: (r.get("task") or {}).get("category")),
            "by_model": _group(records, lambda r: (r.get("model") or {}).get("profile")),
            "by_task": _group(records, lambda r: (r.get("task") or {}).get("id")),
            "failure_classes": _counts(x.get("failure_class") for x in records)}


def _sum_metric(rows: list[dict[str, Any]], getter: Any, denominator: int) -> float | None:
    vals = [float(value) for row in rows if (value := getter(row)) is not None]
    return sum(vals) / denominator if vals and denominator else None


def _test_run_rate(records: list[dict[str, Any]], key: str) -> float | None:
    outcomes = [(r.get("evaluation") or {}).get(key) for r in records]
    outcomes = [x for x in outcomes if x is not None]
    if not outcomes:
        return None
    return sum(bool(x.get("passed")) for x in outcomes) / len(outcomes)


def _test_case_rate(records: list[dict[str, Any]], key: str) -> float | None:
    total = passed = 0
    for row in records:
        result = (row.get("evaluation") or {}).get(key) or {}
        if result.get("tests_total") is not None:
            run_passed = result.get("tests_passed") or 0
            run_failed = result.get("tests_failed") or 0
            total += run_passed + run_failed
            passed += run_passed
    return passed / total if total else None


def _group(records: list[dict[str, Any]], key_fn: Any) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        key = key_fn(record)
        if key is not None:
            groups.setdefault(str(key), []).append(record)
    out = {}
    for key, rows in groups.items():
        valid_rows = [r for r in rows if r.get("status") not in {"infrastructure_error", "evaluation_error"}]
        out[key] = {"total_runs": len(rows), "valid_runs": len(valid_rows),
                    "success_count": sum(bool(r.get("success")) for r in valid_rows),
                    "success_rate": (sum(bool(r.get("success")) for r in valid_rows) / len(valid_rows)) if valid_rows else None,
                    "success_rate_wilson_95": _wilson(sum(bool(r.get("success")) for r in valid_rows), len(valid_rows)),
                    "median_duration_seconds": statistics.median([r["duration_seconds"] for r in rows if r.get("duration_seconds") is not None]) if any(r.get("duration_seconds") is not None for r in rows) else None,
                    "median_prompt_tokens": statistics.median([r["llm"]["prompt_tokens"] for r in rows if (r.get("llm") or {}).get("prompt_tokens") is not None]) if any((r.get("llm") or {}).get("prompt_tokens") is not None for r in rows) else None,
                    "median_iterations": statistics.median([r["agent"]["iterations"] for r in rows if (r.get("agent") or {}).get("iterations") is not None]) if any((r.get("agent") or {}).get("iterations") is not None for r in rows) else None}
    return out


def _wilson(successes: int, total: int) -> dict[str, float] | None:
    if not total:
        return None
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return {"lower": max(0.0, center - margin), "upper": min(1.0, center + margin), "method": "Wilson 95%"}


def _counts(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        if value:
            out[value] = out.get(value, 0) + 1
    return out


def load_records(input_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(input_dir.glob("**/run.json"))]


def write_report(records: list[dict[str, Any]], destination: Path, format_name: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if format_name == "json":
        destination.write_text(json.dumps({"summary": summarize(records), "runs": records}, indent=2,
                                          ensure_ascii=False) + "\n", encoding="utf-8")
    elif format_name == "csv":
        fields = ["run_id", "experiment_id", "task_id", "category", "model", "configured_context",
                  "loaded_context", "model_load_seconds", "status", "success",
                  "duration_seconds", "prompt_tokens", "completion_tokens", "total_tokens", "peak_context",
                  "average_context", "iterations", "tool_calls", "successful_tool_calls", "failed_tool_calls",
                  "files_changed", "lines_added", "lines_removed", "public_tests_passed", "public_tests_failed",
                  "public_tests_skipped", "hidden_tests_passed", "hidden_tests_failed", "hidden_tests_skipped",
                  "self_corrected", "tokens_per_second", "failure_class"]
        with destination.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for r in records:
                writer.writerow({"run_id": r.get("run_id"), "experiment_id": r.get("experiment_id"),
                                 "task_id": (r.get("task") or {}).get("id"), "category": (r.get("task") or {}).get("category"),
                                 "model": (r.get("model") or {}).get("profile"),
                                 "configured_context": (r.get("controls") or {}).get("configured_context"),
                                 "loaded_context": (r.get("model") or {}).get("loaded_context"),
                                 "model_load_seconds": (r.get("model") or {}).get("model_load_seconds"),
                                 "status": r.get("status"),
                                 "success": r.get("success"), "duration_seconds": r.get("duration_seconds"),
                                 "prompt_tokens": (r.get("llm") or {}).get("prompt_tokens"),
                                 "completion_tokens": (r.get("llm") or {}).get("completion_tokens"),
                                 "total_tokens": (r.get("llm") or {}).get("total_tokens"),
                                 "peak_context": (r.get("llm") or {}).get("peak_context"),
                                 "average_context": (r.get("llm") or {}).get("average_context"),
                                 "iterations": (r.get("agent") or {}).get("iterations"),
                                 "tool_calls": (r.get("agent") or {}).get("tool_calls"),
                                 "successful_tool_calls": (r.get("agent") or {}).get("successful_tool_calls"),
                                 "failed_tool_calls": (r.get("agent") or {}).get("failed_tool_calls"),
                                 "files_changed": len((r.get("repository") or {}).get("files_changed", [])),
                                 "lines_added": (r.get("agent") or {}).get("lines_added"),
                                 "lines_removed": (r.get("agent") or {}).get("lines_removed"),
                                 "public_tests_passed": ((r.get("evaluation") or {}).get("public_tests") or {}).get("tests_passed"),
                                 "public_tests_failed": ((r.get("evaluation") or {}).get("public_tests") or {}).get("tests_failed"),
                                 "public_tests_skipped": ((r.get("evaluation") or {}).get("public_tests") or {}).get("tests_skipped"),
                                 "hidden_tests_passed": ((r.get("evaluation") or {}).get("hidden_tests") or {}).get("tests_passed"),
                                 "hidden_tests_failed": ((r.get("evaluation") or {}).get("hidden_tests") or {}).get("tests_failed"),
                                 "hidden_tests_skipped": ((r.get("evaluation") or {}).get("hidden_tests") or {}).get("tests_skipped"),
                                 "self_corrected": (r.get("agent") or {}).get("self_corrected"),
                                 "tokens_per_second": (r.get("llm") or {}).get("tokens_per_second"),
                                 "failure_class": r.get("failure_class")})
    elif format_name == "html":
        rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(v if v is not None else ''))}</td>" for v in (
            r.get("run_id"), (r.get("task") or {}).get("id"), (r.get("model") or {}).get("profile"),
            r.get("status"), r.get("success"), r.get("duration_seconds"),
            (r.get("controls") or {}).get("configured_context"),
            (r.get("model") or {}).get("loaded_context"),
            (r.get("model") or {}).get("model_load_seconds"),
            (r.get("llm") or {}).get("peak_context"))) + "</tr>" for r in records)
        s = summarize(records)
        category_rows = _html_summary_rows(s["by_category"])
        model_rows = _html_summary_rows(s["by_model"])
        task_rows = _html_summary_rows(s["by_task"])
        failure_rows = _counts_rows(s["failure_classes"])
        context_chart = _context_chart(records)
        duration = s["metrics"]["duration_seconds"]["mean"]
        tps = [((r.get("llm") or {}).get("tokens_per_second")) for r in records]
        tps = [v for v in tps if v is not None]
        tps_mean = sum(tps) / len(tps) if tps else None
        evaluation_rows = _evaluation_rows(records)
        document = f"""<!doctype html><html><head><meta charset='utf-8'><title>Local SWE Benchmark</title>
<style>body{{font:15px system-ui;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#222}}table{{border-collapse:collapse;width:100%;margin:1rem 0 2rem}}th,td{{border:1px solid #ccc;padding:.45rem;text-align:left;vertical-align:top}}th{{background:#f2f4f7}}.cards{{display:flex;gap:2rem;flex-wrap:wrap}}.card{{padding:1rem;background:#f2f4f7}}pre{{white-space:pre-wrap;max-height:24rem;overflow:auto}}</style></head><body>
<h1>Local SWE Benchmark</h1><div class='cards'><div class='card'>Total runs: {s['total_runs']}</div><div class='card'>Tasks: {len(s['by_task'])}</div><div class='card'>Success rate: {s['success_rate']}</div><div class='card'>Mean duration: {duration}</div><div class='card'>Mean tokens/sec: {tps_mean}</div></div>
<h2>By category</h2><table><tr><th>Category</th><th>Runs</th><th>Successes</th><th>Success rate</th><th>Wilson 95% interval</th><th>Median time</th><th>Median tokens</th><th>Median iterations</th></tr>{category_rows}</table>
<h2>By model</h2><table><tr><th>Model</th><th>Runs</th><th>Successes</th><th>Success rate</th><th>Wilson 95% interval</th><th>Median time</th><th>Median tokens</th><th>Median iterations</th></tr>{model_rows}</table>
<h2>Per task</h2><table><tr><th>Task</th><th>Runs</th><th>Successes</th><th>Success rate</th><th>Wilson 95% interval</th><th>Median time</th><th>Median tokens</th><th>Median iterations</th></tr>{task_rows}</table>
<h2>Failure analysis</h2><table><tr><th>Failure class</th><th>Count</th></tr>{failure_rows}</table>
<h2>Context tokens by agent iteration</h2>{context_chart}
<h2>Individual runs</h2><table><tr><th>Run</th><th>Task</th><th>Model</th><th>Status</th><th>Success</th><th>Seconds</th><th>Configured context</th><th>Loaded context</th><th>Load seconds</th><th>Peak observed context</th></tr>{rows}</table>
<h2>Build and test evaluation</h2><table><tr><th>Run</th><th>Stage</th><th>Result</th><th>Cases</th><th>Exit code</th><th>Command and output</th></tr>{evaluation_rows}</table></body></html>"""
        destination.write_text(document, encoding="utf-8")
    else:
        raise ValueError(f"Unsupported report format: {format_name}")
    return destination


def write_run_reports(record: dict[str, Any], related_records: list[dict[str, Any]],
                      reports_root: Path) -> dict[str, Path]:
    """Write a permanent per-run report and refreshed per-model/task summaries."""
    model_name = str((record.get("model") or {}).get("profile") or "unknown-model")
    task_id = str((record.get("task") or {}).get("id") or "unknown-task")
    run_id = str(record.get("run_id") or "unknown-run")
    directory = reports_root / _safe_component(model_name) / _safe_component(task_id)
    per_run_html = write_report([record], directory / f"{_safe_component(run_id)}.html", "html")
    per_run_json = write_report([record], directory / f"{_safe_component(run_id)}.json", "json")
    summary_html = write_report(related_records or [record], directory / "summary.html", "html")
    summary_json = write_report(related_records or [record], directory / "summary.json", "json")
    return {"html": per_run_html, "json": per_run_json,
            "summary_html": summary_html, "summary_json": summary_json}


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "item"
    # Keep sanitized collisions distinct while preserving readable normal IDs.
    if cleaned != value or cleaned.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        cleaned = f"{cleaned[:72]}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"
    return cleaned


def _evaluation_rows(records: list[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for record in records:
        run_id = html.escape(str(record.get("run_id", "")))
        evaluation = record.get("evaluation") or {}
        for label in ("build", "public_tests", "hidden_tests"):
            result = evaluation.get(label)
            if result is None:
                continue
            passed = "passed" if result.get("passed") else "failed"
            total = result.get("tests_total")
            counts = (f"{result.get('tests_passed', 0)} passed / "
                      f"{result.get('tests_failed', 0)} failed / {total} total") if total is not None else "—"
            command = html.escape(json.dumps(result.get("command", []), ensure_ascii=False))
            output = ""
            if result.get("stdout") or result.get("stderr"):
                output = ("<details><summary>stdout/stderr</summary><pre>"
                          + html.escape((result.get("stdout") or "") + (result.get("stderr") or ""))
                          + "</pre></details>")
            command_output = f"<code>{command}</code>{output}"
            rendered.append("<tr>" + "".join(f"<td>{value}</td>" for value in (
                run_id, html.escape(label), html.escape(passed), html.escape(counts),
                html.escape(str(result.get("exit_code", ""))), command_output)) + "</tr>")
        analysis = evaluation.get("analysis")
        if analysis is not None:
            result_text = "passed" if analysis.get("passed") else "failed"
            detail = {"required_terms": analysis.get("required_terms"),
                      "required_paths": analysis.get("required_paths"),
                      "missing_terms": analysis.get("missing_terms"),
                      "missing_paths": analysis.get("missing_paths")}
            rendered.append("<tr>" + "".join(f"<td>{value}</td>" for value in (
                run_id, "analysis rubric", html.escape(result_text), "—", "—",
                "<pre>" + html.escape(json.dumps(detail, ensure_ascii=False, indent=2)) + "</pre>")) + "</tr>")
        failure = record.get("failure_details")
        if failure:
            trace = html.escape(str(failure.get("traceback", "")))
            message = html.escape(str(failure.get("message", record.get("error", ""))))
            detail = (f"<strong>{html.escape(str(failure.get('stage', '')))} / "
                      f"{html.escape(str(failure.get('exception_type', '')))}</strong>: {message}")
            if trace:
                detail += f"<details><summary>Traceback</summary><pre>{trace}</pre></details>"
            rendered.append(f"<tr><td>{run_id}</td><td colspan='5'>{detail}</td></tr>")
    return "".join(rendered) or "<tr><td colspan='6'>No evaluator results recorded.</td></tr>"


def _html_summary_rows(groups: dict[str, Any]) -> str:
    rendered = []
    for key, row in groups.items():
        interval = row.get("success_rate_wilson_95")
        interval_text = f"{interval['lower']:.1%}–{interval['upper']:.1%}" if interval else ""
        values = (key, row["total_runs"], row["success_count"], row["success_rate"], interval_text,
                  row["median_duration_seconds"], row["median_prompt_tokens"], row["median_iterations"])
        rendered.append("<tr>" + "".join(f"<td>{html.escape(str(v if v is not None else ''))}</td>" for v in values) + "</tr>")
    return "".join(rendered)


def _counts_rows(counts: dict[str, int]) -> str:
    return "".join(f"<tr><td>{html.escape(key)}</td><td>{value}</td></tr>" for key, value in counts.items())


def _context_chart(records: list[dict[str, Any]]) -> str:
    histories = [(r, (r.get("llm") or {}).get("context_by_iteration") or []) for r in records]
    histories = [(r, xs) for r, xs in histories if xs]
    if not histories:
        return "<p>Context usage telemetry was not returned by the provider.</p>"
    width, height, left, top = 820, 320, 70, 20
    max_x = max(len(xs) for _, xs in histories)
    max_y = max((v for _, xs in histories for v in xs if v is not None), default=1) or 1
    colors = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2"]
    elements = [f"<svg role='img' aria-label='Observed prompt context by iteration' viewBox='0 0 {width} {height}' style='max-width:100%;border:1px solid #ddd'>"]
    elements.append(f"<line x1='{left}' y1='{height-35}' x2='{width-20}' y2='{height-35}' stroke='#666'/><line x1='{left}' y1='{top}' x2='{left}' y2='{height-35}' stroke='#666'/>")
    elements.append(f"<text x='8' y='18' font-size='12'>tokens (max {max_y})</text><text x='{width//2}' y='{height-5}' font-size='12'>agent iteration</text>")
    labels = []
    for index, (record, values) in enumerate(histories):
        color = colors[index % len(colors)]
        points = []
        for i, value in enumerate(values):
            if value is None:
                continue
            x = left + (i / max(1, max_x - 1)) * (width - left - 25)
            y = height - 35 - (value / max_y) * (height - top - 45)
            points.append(f"{x:.1f},{y:.1f}")
        elements.append(f"<polyline fill='none' stroke='{color}' stroke-width='2' points='{' '.join(points)}'/>")
        label = html.escape(str((record.get("model") or {}).get("profile", record.get("run_id", "run"))))
        labels.append(f"<span style='color:{color}'>■</span> {label}")
    elements.append("</svg><p>" + " · ".join(labels) + "</p>")
    return "".join(elements)
