from __future__ import annotations

import datetime as dt
import hashlib
import json
import platform
import re
import shutil
import sys
import subprocess
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from .agent import Agent, ChatProvider, SYSTEM_PROMPT
from .config import canonical_hash
from . import __version__
from .providers.lmstudio import LMStudioProvider, ProviderError
from .tasks import Task
from .tools import TOOL_SCHEMAS, ToolBox
from .vcs import git_argv, resolve_git_executable


def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(git_argv(repo, *args), cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _exception_details(stage: str, exc: BaseException) -> dict[str, str]:
    return {"stage": stage, "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))}


def run_trial(task: Task, profile_name: str, profile: dict[str, Any], results_root: Path,
              experiment_id: str, provider_override: ChatProvider | None = None) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    run_dir = results_root / run_id
    run_dir.mkdir(parents=True)
    events_path = run_dir / "events.jsonl"
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    started = time.monotonic()

    def event(item: dict[str, Any]) -> None:
        item.setdefault("timestamp", dt.datetime.now(dt.timezone.utc).isoformat())
        with events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")

    worktrees = results_root / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    worktree = worktrees / run_id
    record: dict[str, Any] = {
        "schema_version": 1, "benchmark_version": __version__,
        "run_id": run_id, "experiment_id": experiment_id,
        "status": "infrastructure_error", "success": False, "failure_class": "FAIL_INFRASTRUCTURE",
        "failure_source": "infrastructure", "started_at": started_at,
        "failure_details": None,
        "task": {"id": task.task_id, "version": task.version, "category": task.category,
                 "language": task.language, "base_commit": task.base_commit,
                 "prompt_sha256": canonical_hash(task.description)},
        "model": {"profile": profile_name, "provider": profile.get("provider", "lmstudio"),
                  "reported_name": profile.get("model"),
                  "revision": profile.get("revision"), "quantization": profile.get("quantization"),
                  "backend": profile.get("backend"), "lmstudio_version": profile.get("lmstudio_version"),
                  "lmstudio_instance_id": None, "loaded_context": None,
                  "model_load_seconds": None},
        "controls": {"temperature": float(profile.get("temperature", 0.2)),
                     "top_p": float(profile.get("top_p", 1.0)), "seed": profile.get("seed"),
                     "configured_context": profile.get("context_length"),
                     "max_tokens": task.max_tokens,
                     "max_iterations": task.max_iterations, "max_tool_calls": task.max_tool_calls,
                     "timeout_seconds": task.timeout_seconds,
                     "system_prompt_sha256": canonical_hash(SYSTEM_PROMPT),
                     "tool_schema_sha256": canonical_hash(TOOL_SCHEMAS)},
        "llm": {"requests": 0, "prompt_tokens": None, "completion_tokens": None,
                "total_tokens": None, "peak_context": None, "average_context": None,
                "final_context": None, "ttft_seconds": None, "generation_seconds": None,
                "tokens_per_second": None, "telemetry_source": "provider_usage_and_client_timing"},
        "agent": {},
        # Keep the result schema stable even when setup or an evaluator raises.
        # The previous implementation populated this group only after every
        # evaluation command completed, so one exception erased all progress.
        "evaluation": {"build": None, "public_tests": None,
                       "hidden_tests": None, "analysis": None,
                       "exit_codes": {}},
        "repository": {"path": str(task.repository), "bundle": str(task.repository_bundle) if task.repository_bundle else None,
                       "bundle_sha256": hashlib.sha256(task.repository_bundle.read_bytes()).hexdigest()
                       if task.repository_bundle and task.repository_bundle.is_file() else None,
                       "base_commit": task.base_commit, "patch_sha256": None,
                       "files_changed": [], "worktree": str(worktree), "worktree_removed": None},
        "host": {"os": platform.platform(), "cpu": platform.processor(),
                 "python_version": platform.python_version(), "python_executable": sys.executable,
                 "gpu": None,
                 "vram_bytes": None, "ram_bytes": None},
    }
    agent = None
    toolbox = None
    created = False
    phase = "setup"
    try:
        phase = "repository_clone"
        if not task.repository.is_dir() and task.repository_bundle:
            if not task.repository_bundle.is_file():
                raise RuntimeError(f"Repository bundle does not exist: {task.repository_bundle}")
            task.repository.parent.mkdir(parents=True, exist_ok=True)
            clone = subprocess.run([resolve_git_executable(), "clone", str(task.repository_bundle), str(task.repository)],
                                   cwd=task.repository.parent, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=120)
            if clone.returncode:
                raise RuntimeError(
                    f"Command failed (exit {clone.returncode}): "
                    f"git clone {task.repository_bundle} {task.repository}; "
                    f"stdout={clone.stdout[-4000:]!r}; stderr={clone.stderr[-4000:]!r}"
                )
        if not task.repository.is_dir():
            raise RuntimeError(f"Repository does not exist: {task.repository}")
        phase = "commit_validation"
        check = _git(task.repository, "cat-file", "-e", f"{task.base_commit}^{{commit}}")
        if check.returncode:
            raise RuntimeError(
                f"Command failed (exit {check.returncode}): git cat-file -e "
                f"{task.base_commit}^{{commit}} in {task.repository}; "
                f"stderr={check.stderr[-4000:]!r}"
            )
        phase = "worktree_create"
        add = _git(task.repository, "worktree", "add", "--detach", str(worktree), task.base_commit)
        if add.returncode:
            raise RuntimeError(
                f"Command failed (exit {add.returncode}): git worktree add --detach "
                f"{worktree} {task.base_commit}; stdout={add.stdout[-4000:]!r}; "
                f"stderr={add.stderr[-4000:]!r}"
            )
        created = True
        phase = "worktree_verify"
        actual = _git(worktree, "rev-parse", "HEAD").stdout.strip()
        if actual != task.base_commit and not actual.startswith(task.base_commit):
            raise RuntimeError(f"Worktree commit mismatch: {actual}")

        phase = "agent_setup"
        p = profile
        provider = provider_override or LMStudioProvider(p.get("base_url", "http://localhost:1234/v1"),
                                                        str(p["model"]), p.get("api_key"))
        if provider_override is None and p.get("context_length") is not None:
            phase = "model_load"
            loaded_model = provider.ensure_model_loaded(p["context_length"], task.timeout_seconds)
            record["model"].update({
                "lmstudio_instance_id": loaded_model["instance_id"],
                "loaded_context": loaded_model["context_length"],
                "model_load_seconds": loaded_model["load_time_seconds"],
            })
            event({"type": "model_ready", **loaded_model,
                   "configured_context": p["context_length"]})
        toolbox = ToolBox(worktree, task.allowed_commands, task.public_command,
                          task.timeout_seconds, event)
        agent = Agent(provider, toolbox, event, float(p.get("temperature", 0.2)),
                      float(p.get("top_p", 1.0)), p.get("seed"), task.max_iterations,
                      task.max_tool_calls, task.max_tokens, task.timeout_seconds)
        phase = "agent"
        final = agent.run(task.description)
        (run_dir / "final_response.txt").write_text(final, encoding="utf-8")
        record["status"] = agent.stop_reason
        record["failure_class"] = None if agent.stop_reason == "completed" else "FAIL_" + agent.stop_reason.upper()
        record["failure_source"] = None if agent.stop_reason == "completed" else "agent"
        test_failures, self_corrected = _self_correction_stats(events_path)
        record["agent"] = {
            "iterations": agent.iterations, "tool_calls": agent.tool_calls,
            "successful_tool_calls": agent.successful_tool_calls,
            "failed_tool_calls": agent.failed_tool_calls,
            "commands_executed": toolbox.commands_executed,
            "files_read": len(toolbox.files_read), "files_modified": 0,
            "lines_added": 0, "lines_removed": 0, "public_test_failures": test_failures,
            "self_corrected": self_corrected,
        }
        record["model"]["server_reported_name"] = agent.reported_model
        contexts = agent.contexts
        observed_contexts = [value for value in contexts if value is not None]
        llm = record["llm"]
        llm.update({"requests": agent.requests,
                    "prompt_tokens": agent.prompt_tokens if observed_contexts else None,
                    "completion_tokens": agent.completion_tokens if agent.completion_token_observations else None,
                    "total_tokens": agent.prompt_tokens + agent.completion_tokens if observed_contexts or agent.completion_token_observations else None,
                    "peak_context": max(observed_contexts) if observed_contexts else None,
                    "average_context": sum(observed_contexts) / len(observed_contexts) if observed_contexts else None,
                    "final_context": contexts[-1] if contexts else None,
                    "context_by_iteration": contexts.copy(),
                    "ttft_seconds": sum(agent.ttft) / len(agent.ttft) if agent.ttft else None,
                    "generation_seconds": agent.generation_seconds if agent.requests else None,
                    "tokens_per_second": agent.completion_tokens / agent.generation_seconds
                    if agent.completion_tokens and agent.generation_seconds else None})

        phase = "patch_capture"
        # Include untracked files in the preserved patch, then snapshot before hidden tests are staged.
        _git(worktree, "add", "-N", ".")
        diff = _git(worktree, "diff", "--binary", task.base_commit)
        patch_data = diff.stdout.encode("utf-8")
        (run_dir / "patch.diff").write_bytes(patch_data)
        record["repository"]["patch_sha256"] = hashlib.sha256(patch_data).hexdigest()
        committed = _git(worktree, "rev-list", "--count", f"{task.base_commit}..HEAD")
        record["repository"]["commits_added"] = int(committed.stdout.strip() or "0") if committed.returncode == 0 else None
        changed = _git(worktree, "diff", "--name-only", task.base_commit).stdout.splitlines()
        record["repository"]["files_changed"] = changed
        record["agent"]["files_modified"] = len(set(changed) | toolbox.files_modified)
        numstat = _git(worktree, "diff", "--numstat", task.base_commit).stdout.splitlines()
        plus = minus = 0
        for line in numstat:
            fields = line.split("\t", 2)
            if len(fields) == 3:
                try:
                    plus += int(fields[0]); minus += int(fields[1])
                except ValueError:
                    pass
        record["agent"]["lines_added"] = plus
        record["agent"]["lines_removed"] = minus

        phase = "evaluation_copy"
        eval_root = run_dir / "evaluation_workspace"
        # Evaluation operates on a separate copy so hidden artifacts never enter the agent worktree.
        shutil.copytree(worktree, eval_root, ignore=shutil.ignore_patterns(".git"))
        hidden_root = eval_root / ".benchmark_hidden"
        hidden_root.mkdir(parents=True, exist_ok=True)
        phase = "hidden_test_staging"
        for mapping in task.hidden_files:
            if isinstance(mapping, str):
                source_rel, destination_rel = mapping, Path(".benchmark_hidden") / Path(mapping).name
            elif isinstance(mapping, dict):
                source_rel, destination_rel = mapping["source"], mapping["destination"]
            else:
                raise ValueError("hidden_files entries must be strings or source/destination mappings")
            source = (task.directory / source_rel).resolve()
            if task.directory not in source.parents:
                raise ValueError("Hidden test source escapes task directory")
            destination = (eval_root / destination_rel).resolve()
            if eval_root not in destination.parents:
                raise ValueError("Hidden test destination escapes evaluation workspace")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        # Update the persisted result as each stage completes. Hidden sources
        # are still copied only into eval_root, never into the agent worktree.
        phase = "evaluation_commands"
        evaluation = record["evaluation"]
        for label, command in (("build", task.build_command), ("public_tests", task.public_command),
                               ("hidden_tests", task.hidden_command)):
            if command is None:
                continue
            command = _resolve_command(command)
            outcome = subprocess.run(command, cwd=eval_root, shell=False, capture_output=True,
                                     text=True, encoding="utf-8", errors="replace",
                                     timeout=task.timeout_seconds)
            evaluation[label] = {"command": command, "exit_code": outcome.returncode,
                                 "stdout": outcome.stdout[-50_000:], "stderr": outcome.stderr[-50_000:],
                                 "passed": outcome.returncode == 0}
            evaluation["exit_codes"][label] = outcome.returncode
            if label.endswith("tests"):
                parsed = _parse_test_counts(outcome.stdout + "\n" + outcome.stderr)
                evaluation[label].update(parsed)
        if task.category == "analysis":
            phase = "analysis_rubric"
            normalized = (final or "").casefold()
            missing_terms = [term for term in task.analysis_required_terms if term.casefold() not in normalized]
            missing_paths = [path for path in task.analysis_required_paths if path.casefold() not in normalized]
            evaluation["analysis"] = {"required_terms": task.analysis_required_terms,
                                      "required_paths": task.analysis_required_paths,
                                      "missing_terms": missing_terms, "missing_paths": missing_paths,
                                      "passed": not missing_terms and not missing_paths}
        analysis_pass = (evaluation.get("analysis") or {}).get("passed", True)
        accepted = agent.stop_reason == "completed" and analysis_pass and all(
            evaluation[k] is None or evaluation[k]["passed"] for k in ("build", "public_tests", "hidden_tests"))
        record["success"] = accepted
        record["status"] = "success" if accepted else ("failure" if agent.stop_reason == "completed" else agent.stop_reason)
        record["failure_class"] = None if accepted else _failure_class(agent, evaluation)
        eval_failed = any(evaluation.get(key) is not None and not evaluation[key]["passed"]
                          for key in ("build", "public_tests", "hidden_tests", "analysis"))
        record["failure_source"] = None if accepted else (
            "agent" if agent.stop_reason != "completed" else
            "model" if eval_failed else
            "tool" if record["failure_class"] == "FAIL_TOOL_USAGE" else "agent")
    except subprocess.TimeoutExpired as exc:
        record["status"] = "timeout"
        record["failure_class"] = "FAIL_TIMEOUT"
        record["failure_source"] = "evaluation" if (
            phase.startswith("evaluation") or phase in {"hidden_test_staging", "analysis_rubric"}
        ) else "infrastructure"
        record["error"] = str(exc)
        record["failure_details"] = _exception_details(phase, exc)
        if record["failure_source"] == "evaluation":
            record["evaluation"]["error"] = record["error"]
        event({"type": "run_error", **record["failure_details"]})
    except ProviderError as exc:
        record["status"] = "timeout" if exc.kind == "timeout" else (
            "failure" if exc.kind in {"model_error", "context_error"} else "infrastructure_error")
        record["failure_class"] = "FAIL_TIMEOUT" if exc.kind == "timeout" else (
            "FAIL_CONTEXT" if exc.kind == "context_error" else
            "FAIL_MODEL_ERROR" if exc.kind == "model_error" else "FAIL_INFRASTRUCTURE")
        record["failure_source"] = "model" if exc.kind in {"model_error", "context_error"} else "infrastructure"
        record["error"] = str(exc)
        record["failure_details"] = _exception_details(phase, exc)
        event({"type": "run_error", **record["failure_details"]})
    except Exception as exc:
        if phase.startswith("evaluation") or phase == "hidden_test_staging" or phase == "analysis_rubric":
            record["status"] = "evaluation_error"
            record["failure_class"] = "FAIL_EVALUATION"
            record["failure_source"] = "evaluation"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["failure_details"] = _exception_details(phase, exc)
        if record["failure_source"] == "evaluation":
            record["evaluation"]["error"] = record["error"]
        event({"type": "run_error", **record["failure_details"]})
    finally:
        record["duration_seconds"] = time.monotonic() - started
        if created:
            try:
                phase = "worktree_cleanup"
                removed = _git(task.repository, "worktree", "remove", "--force", str(worktree))
                prune = _git(task.repository, "worktree", "prune")
                record["repository"]["worktree_removed"] = removed.returncode == 0
                if removed.returncode or prune.returncode:
                    record["cleanup_error"] = (
                        f"git worktree remove exit={removed.returncode}: {removed.stderr[-4000:]}; "
                        f"git worktree prune exit={prune.returncode}: {prune.stderr[-4000:]}"
                    )
            except Exception as exc:
                record["repository"]["worktree_removed"] = False
                record["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                record["failure_details"] = _exception_details("worktree_cleanup", exc)
                event({"type": "run_error", **record["failure_details"]})
            if not record["repository"]["worktree_removed"] or record.get("cleanup_error"):
                record["success"] = False
                record["status"] = "infrastructure_error"
                record["failure_class"] = "FAIL_INFRASTRUCTURE"
                record["failure_source"] = "infrastructure"
        if "evaluation_workspace" in locals():
            try:
                shutil.rmtree(eval_root)
            except OSError as exc:
                record["cleanup_error"] = f"Evaluation workspace cleanup failed: {exc}"
                record["failure_details"] = _exception_details("evaluation_cleanup", exc)
                event({"type": "run_error", **record["failure_details"]})
                record["success"] = False
                record["status"] = "infrastructure_error"
                record["failure_class"] = "FAIL_INFRASTRUCTURE"
                record["failure_source"] = "infrastructure"
        (run_dir / "run.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


def _self_correction_stats(events_path: Path) -> tuple[int, bool]:
    failures = 0
    corrected = False
    try:
        for line in events_path.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if e.get("type") == "tool" and e.get("name") == "run_tests":
                result = e.get("result", {})
                if isinstance(result.get("exit_code"), int) and result["exit_code"] != 0:
                    failures += 1
                elif failures and result.get("exit_code") == 0:
                    corrected = True
    except (OSError, json.JSONDecodeError):
        return failures, False
    return failures, corrected


def _parse_test_counts(output: str) -> dict[str, int | None]:
    """Best-effort parser; null counts mean the test runner format was unknown."""
    unittest_total = re.search(r"Ran\s+(\d+)\s+tests?", output, re.IGNORECASE)
    if unittest_total:
        total = int(unittest_total.group(1))
        summary = re.search(r"FAILED\s*\(([^)]*)\)", output, re.IGNORECASE)
        failures = 0
        skipped = 0
        if summary:
            failures = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", summary.group(1)))
            skipped_match = re.search(r"skipped=(\d+)", summary.group(1), re.IGNORECASE)
            skipped = int(skipped_match.group(1)) if skipped_match else 0
        else:
            skipped_match = re.search(r"OK\s*\(skipped=(\d+)\)", output, re.IGNORECASE)
            skipped = int(skipped_match.group(1)) if skipped_match else 0
        return {"tests_total": total, "tests_failed": failures,
                "tests_skipped": skipped, "tests_passed": max(0, total - failures - skipped)}
    pytest_pass = re.search(r"(\d+)\s+passed", output, re.IGNORECASE)
    pytest_fail = re.search(r"(\d+)\s+failed", output, re.IGNORECASE)
    if pytest_pass and (pytest_fail or "passed" in output.lower()):
        passed = int(pytest_pass.group(1)); failed = int(pytest_fail.group(1)) if pytest_fail else 0
        skipped = re.search(r"(\d+)\s+skipped", output, re.IGNORECASE)
        skipped_count = int(skipped.group(1)) if skipped else 0
        return {"tests_total": passed + failed + skipped_count, "tests_passed": passed,
                "tests_failed": failed, "tests_skipped": skipped_count}
    jest = re.search(r"Tests:\s*(.*?)\s*(\d+)\s+total", output, re.IGNORECASE)
    if jest:
        summary, total_text = jest.group(1), jest.group(2)
        passed_match = re.search(r"(\d+)\s+passed", summary, re.IGNORECASE)
        failed_match = re.search(r"(\d+)\s+failed", summary, re.IGNORECASE)
        failed = int(failed_match.group(1)) if failed_match else 0
        total = int(total_text)
        skipped_match = re.search(r"(\d+)\s+skipped", summary, re.IGNORECASE)
        skipped = int(skipped_match.group(1)) if skipped_match else 0
        return {"tests_total": total, "tests_passed": int(passed_match.group(1)) if passed_match else max(0, total-failed-skipped),
                "tests_failed": failed, "tests_skipped": skipped}
    return {"tests_total": None, "tests_passed": None, "tests_failed": None, "tests_skipped": None}


def _resolve_command(command: list[str]) -> list[str]:
    if Path(command[0]).name.lower() in {"python", "python3"}:
        return [sys.executable, *command[1:]]
    return command


def _failure_class(agent: Agent, evaluation: dict[str, Any]) -> str:
    if agent.stop_reason != "completed":
        return "FAIL_" + agent.stop_reason.upper()
    if evaluation.get("analysis") and not evaluation["analysis"]["passed"]:
        return "FAIL_TASK_UNDERSTANDING"
    for key in ("build", "public_tests", "hidden_tests"):
        if evaluation.get(key) and not evaluation[key]["passed"]:
            return "FAIL_TEST_FAILURE" if key != "build" else "FAIL_INCOMPLETE_IMPLEMENTATION"
    if agent.failed_tool_calls:
        return "FAIL_TOOL_USAGE"
    return "FAIL_UNKNOWN"
