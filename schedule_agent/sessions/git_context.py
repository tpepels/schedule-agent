from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitContext:
    cwd: Path
    git_root: Path | None
    git_branch: str | None
    worktree_root: Path | None


def _run_git(cwd: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def detect_git_context(cwd: Path | None = None) -> GitContext:
    resolved_cwd = (cwd or Path.cwd()).resolve()
    git_root_raw = _run_git(resolved_cwd, "rev-parse", "--show-toplevel")
    git_root = Path(git_root_raw).resolve() if git_root_raw else None
    git_branch = _run_git(resolved_cwd, "branch", "--show-current")
    return GitContext(
        cwd=resolved_cwd,
        git_root=git_root,
        git_branch=git_branch,
        worktree_root=git_root,
    )
