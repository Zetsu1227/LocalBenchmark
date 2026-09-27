from __future__ import annotations

import os
import shutil
from pathlib import Path


def resolve_git_executable() -> str:
    """Find Git even when Python was launched with a restricted Windows PATH."""
    configured = os.environ.get("LOCAL_SWE_BENCHMARK_GIT")
    if configured:
        candidate = Path(os.path.expandvars(configured)).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        raise RuntimeError(
            "LOCAL_SWE_BENCHMARK_GIT points to a missing executable: " + str(candidate)
        )

    discovered = shutil.which("git")
    if discovered:
        return discovered

    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
             os.path.join(os.environ["LOCALAPPDATA"], "Programs")
             if os.environ.get("LOCALAPPDATA") else None]
    candidates = [Path(root) / "Git" / "cmd" / "git.exe" for root in roots if root]
    candidates += [Path(root) / "Git" / "bin" / "git.exe" for root in roots if root]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())

    raise RuntimeError(
        "Git was not found on PATH or in standard Git for Windows locations. "
        "Install Git or set LOCAL_SWE_BENCHMARK_GIT to the full path of git.exe."
    )


def git_argv(repository: Path, *args: str) -> list[str]:
    """Build a Git command trusting only its explicit repository for this process."""
    safe_path = repository.resolve().as_posix()
    return [resolve_git_executable(), "-c", f"safe.directory={safe_path}", *args]
