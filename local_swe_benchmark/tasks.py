from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from .config import load_yaml


@dataclass(frozen=True)
class Task:
    task_id: str
    category: str
    version: int
    language: str
    directory: Path
    repository: Path
    repository_bundle: Path | None
    base_commit: str
    description: str
    build_command: list[str] | None
    public_command: list[str] | None
    hidden_command: list[str] | None
    hidden_files: list[str | dict[str, str]]
    analysis_required_terms: list[str]
    analysis_required_paths: list[str]
    timeout_seconds: int
    max_iterations: int
    max_tool_calls: int
    max_tokens: int
    allowed_commands: list[str]
    raw: dict[str, Any]


def load_task(task_file: Path) -> Task:
    data = load_yaml(task_file)
    if data.get("schema_version") != 1:
        raise ValueError(f"Unsupported task schema_version in {task_file}")
    task = data.get("task", {})
    repo = data.get("repository", {})
    tests = data.get("tests", {})
    limits = data.get("limits", {})
    rubric = data.get("evaluation", {}).get("analysis_rubric", {})
    if data.get("category") not in {"creation", "refactoring", "analysis"}:
        raise ValueError(f"Invalid category in {task_file}")
    base_commit = str(repo["base_commit"])
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", base_commit):
        raise ValueError(f"repository.base_commit must be a full immutable Git commit hash in {task_file}")
    repository = Path(repo["path"])
    if not repository.is_absolute():
        repository = (task_file.parent / repository).resolve()
    bundle = Path(repo["bundle"]) if repo.get("bundle") else None
    if bundle is not None and not bundle.is_absolute():
        bundle = (task_file.parent / bundle).resolve()
    description = task.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"Task description is required in {task_file}")
    if not task.get("acceptance_criteria") or not all(isinstance(x, str) and x.strip() for x in task["acceptance_criteria"]):
        raise ValueError(f"Task acceptance_criteria must contain one or more non-empty strings in {task_file}")
    if data["category"] in {"creation", "refactoring"} and not (tests.get("public_command") or tests.get("hidden_command")):
        raise ValueError(f"Code-changing tasks require public or hidden tests in {task_file}")
    limits_values = {"timeout_seconds": int(limits.get("timeout_seconds", 900)),
                     "max_iterations": int(limits.get("max_iterations", 30)),
                     "max_tool_calls": int(limits.get("max_tool_calls", 100)),
                     "max_tokens": int(limits.get("max_tokens", 32768))}
    if any(value <= 0 for value in limits_values.values()):
        raise ValueError(f"All task limits must be positive in {task_file}")
    hidden_files = list(tests.get("hidden_files", []))
    if any(not isinstance(entry, (str, dict)) for entry in hidden_files):
        raise ValueError(f"tests.hidden_files entries must be strings or mappings in {task_file}")
    for entry in hidden_files:
        if isinstance(entry, dict) and not all(isinstance(entry.get(key), str) and entry[key]
                                               for key in ("source", "destination")):
            raise ValueError(f"Hidden file mappings require non-empty source and destination in {task_file}")
    for field in ("required_terms", "required_paths"):
        values = rubric.get(field, [])
        if not isinstance(values, list) or any(not isinstance(x, str) or not x.strip() for x in values):
            raise ValueError(f"Analysis rubric {field} must be a list of non-empty strings in {task_file}")
    if data["category"] == "analysis" and not rubric.get("required_terms") and not rubric.get("required_paths"):
        raise ValueError(f"Analysis tasks require a non-empty machine-checkable analysis_rubric in {task_file}")
    return Task(
        task_id=str(data["id"]), category=data["category"],
        version=int(data.get("version", 1)), language=str(data["language"]),
        directory=task_file.parent.resolve(), repository=repository, repository_bundle=bundle,
        base_commit=base_commit, description=description,
        build_command=_command(data.get("build", {}).get("command")),
        public_command=_command(tests.get("public_command")),
        hidden_command=_command(tests.get("hidden_command")),
        hidden_files=hidden_files,
        analysis_required_terms=list(rubric.get("required_terms", [])),
        analysis_required_paths=list(rubric.get("required_paths", [])),
        timeout_seconds=limits_values["timeout_seconds"],
        max_iterations=limits_values["max_iterations"],
        max_tool_calls=limits_values["max_tool_calls"],
        max_tokens=limits_values["max_tokens"],
        allowed_commands=list(limits.get("allowed_commands", [])), raw=data,
    )


def _command(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
        raise ValueError("Commands must be non-empty arrays of strings (never shell strings)")
    return value


def discover_tasks(root: Path) -> list[Path]:
    return sorted(root.glob("**/task.yaml"))
