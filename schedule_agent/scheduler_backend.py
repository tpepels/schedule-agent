from __future__ import annotations

import re
import shlex
import subprocess
from datetime import datetime

from .execution import build_agent_cmd


def parse_at_job_id(stdout: str) -> str | None:
    """Extract the numeric at job id from `at` stdout."""
    match = re.search(r"\bjob\s+(\d+)\s+at\b", stdout)
    return match.group(1) if match else None


def build_script(job: dict) -> str:
    """Build the shell script that `at` will execute."""
    cmd = build_agent_cmd(job)
    return (
        f"cd {shlex.quote(job['cwd'])}\n"
        f"export PATH=/usr/local/bin:/usr/bin:/bin\n"
        f"{cmd} >> {shlex.quote(job['log'])} 2>&1\n"
    )


def submit_job(job: dict, dry_run: bool = False) -> tuple[str | None, str]:
    """Submit the job to `at`.

    Returns (at_job_id, output_text).
    When dry_run=True, returns (None, preview_text) without touching at.
    Raises RuntimeError if `at` exits non-zero.
    """
    script = build_script(job)
    if dry_run:
        preview = f"Would schedule at: {job['when']}\n\n{script}"
        return None, preview

    proc = subprocess.run(
        ["at", job["when"]],
        input=script,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to schedule job")

    # `at` writes its job confirmation ("job N at ...") to stderr on Linux;
    # try stderr first, fall back to stdout for non-standard implementations.
    at_job_id = parse_at_job_id(proc.stderr) or parse_at_job_id(proc.stdout)
    output = (proc.stderr + proc.stdout).strip()

    if at_job_id is None:
        raise RuntimeError(f"Could not determine at job id from output: {output!r}")

    return at_job_id, output


def remove_at_job(at_job_id: str) -> tuple[bool, str]:
    """Remove a job from `at` via atrm.

    Returns (success, error_message).
    """
    proc = subprocess.run(
        ["atrm", str(at_job_id)],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, proc.stderr.strip()
