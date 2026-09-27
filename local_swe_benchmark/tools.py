from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .vcs import git_argv


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 repository file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or replace a UTF-8 repository file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Replace one exact text occurrence in a UTF-8 file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "list_directory", "description": "List files and directories at a repository-relative path.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_files", "description": "Search text in repository files using a literal or regular expression.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "regex": {"type": "boolean"}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "run_command", "description": "Run an allowlisted executable with argument array in the repository. Shell syntax is not supported.", "parameters": {"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string"}}}, "required": ["argv"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "run_tests", "description": "Run the task's public test command.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "git_diff", "description": "Show the current tracked and untracked repository patch.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "git_status", "description": "Show repository status.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
]


class ToolError(RuntimeError):
    pass


class ToolBox:
    def __init__(self, root: Path, allowed_commands: list[str], public_command: list[str] | None,
                 timeout_seconds: int, event: Any):
        self.root = root.resolve()
        self.allowed_commands = {Path(x).name.lower() for x in allowed_commands}
        self.public_command = public_command
        self.timeout_seconds = timeout_seconds
        self.event = event
        self.files_read: set[str] = set()
        self.files_modified: set[str] = set()
        self.commands_executed = 0

    def _path(self, value: str) -> Path:
        candidate = (self.root / value).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ToolError("Path escapes the task repository")
        return candidate

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        try:
            result = self._dispatch(name, arguments)
            output = {"ok": True, **result}
        except Exception as exc:
            output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.event({"type": "tool", "name": name, "arguments": arguments, "result": output,
                    "duration_seconds": time.monotonic() - started})
        return output

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "read_file":
            p = self._path(args["path"])
            content = p.read_text(encoding="utf-8")
            self.files_read.add(p.relative_to(self.root).as_posix())
            return {"content": content[:100_000], "content_chars": len(content),
                    "truncated": len(content) > 100_000}
        if name == "write_file":
            p = self._path(args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8", newline="")
            self.files_modified.add(p.relative_to(self.root).as_posix())
            return {"path": p.relative_to(self.root).as_posix(), "bytes": p.stat().st_size}
        if name == "edit_file":
            p = self._path(args["path"])
            old = args["old_text"]
            if not old:
                raise ToolError("old_text must not be empty")
            content = p.read_text(encoding="utf-8")
            count = content.count(old)
            if count != 1:
                raise ToolError(f"Expected exactly one match; found {count}")
            p.write_text(content.replace(old, args["new_text"], 1), encoding="utf-8", newline="")
            self.files_modified.add(p.relative_to(self.root).as_posix())
            return {"path": p.relative_to(self.root).as_posix(), "replacements": 1}
        if name == "list_directory":
            p = self._path(args.get("path", "."))
            return {"entries": [{"name": x.name, "directory": x.is_dir()} for x in sorted(p.iterdir())]}
        if name == "search_files":
            query = args["query"]
            matcher = re.compile(query) if args.get("regex") else None
            found = []
            for p in sorted(self._path(args.get("path", ".")).rglob("*")):
                if not p.is_file() or any(x in p.parts for x in (".git", ".venv", "node_modules")):
                    continue
                try:
                    resolved = p.resolve()
                    if resolved != self.root and self.root not in resolved.parents:
                        continue
                    lines = p.read_text(encoding="utf-8").splitlines()
                    self.files_read.add(p.relative_to(self.root).as_posix())
                    for n, line in enumerate(lines, 1):
                        hit = bool(matcher.search(line)) if matcher else query in line
                        if hit:
                            found.append({"path": p.relative_to(self.root).as_posix(), "line": n, "text": line[:1000]})
                            if len(found) >= 200:
                                return {"matches": found, "truncated": True, "match_limit": 200}
                except (UnicodeError, OSError):
                    continue
            return {"matches": found, "truncated": False, "match_limit": 200}
        if name in {"run_command", "run_tests"}:
            argv = self.public_command if name == "run_tests" else args["argv"]
            if not argv:
                raise ToolError("No public test command is configured")
            if Path(argv[0]).name.lower() not in self.allowed_commands:
                raise ToolError(f"Executable is not allowlisted: {argv[0]}")
            return self._run(argv)
        if name == "git_status":
            return self._run(git_argv(self.root, "status", "--short"))
        if name == "git_diff":
            tracked = self._run(git_argv(self.root, "diff", "--no-ext-diff", "--"))
            untracked = self._run(git_argv(self.root, "ls-files", "--others", "--exclude-standard"))
            return {"tracked": tracked, "untracked_files": untracked.get("stdout", "")}
        raise ToolError(f"Unknown tool: {name}")

    def _run(self, argv: list[str]) -> dict[str, Any]:
        self.commands_executed += 1
        if Path(argv[0]).name.lower() in {"python", "python3"}:
            argv = [sys.executable, *argv[1:]]
        elif Path(argv[0]).name.lower() in {"git", "git.exe"}:
            argv = git_argv(self.root, *argv[1:])
        try:
            p = subprocess.run(argv, cwd=self.root, shell=False, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=self.timeout_seconds)
            return {"ok": p.returncode == 0, "argv": argv, "exit_code": p.returncode,
                    "stdout": p.stdout[-30_000:], "stderr": p.stderr[-30_000:],
                    "stdout_chars": len(p.stdout), "stderr_chars": len(p.stderr),
                    "stdout_truncated": len(p.stdout) > 30_000,
                    "stderr_truncated": len(p.stderr) > 30_000}
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "argv": argv, "exit_code": None, "timeout": True,
                    "stdout": str(exc.stdout or "")[-30_000:], "stderr": str(exc.stderr or "")[-30_000:]}
