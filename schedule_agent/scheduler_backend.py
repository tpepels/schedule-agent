from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass

from .execution import build_agent_cmd
from .time_utils import (
    iso_to_at_time,
    parse_iso_datetime,
    resolve_schedule_input,
)

ATQ_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
DEFAULT_PATH_ENTRIES = ["/usr/local/bin", "/usr/bin", "/bin"]


def _run_at(cmd, **kwargs):
    """Invoke an `at`/`atq`/`atrm` subprocess under a C locale.

    Parsing `at` stderr/stdout relies on English phrasings like "job N at";
    forcing LC_ALL=C/LANG=C makes the output deterministic regardless of
    the user's shell locale.
    """
    env = kwargs.pop("env", None)
    env = dict(os.environ) if env is None else dict(env)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return subprocess.run(cmd, env=env, **kwargs)


@dataclass(frozen=True)
class AtqEntry:
    at_job_id: str
    scheduled_for: str
    queue: str
    owner: str


def parse_at_job_id(output: str) -> str | None:
    match = re.search(r"\bjob\s+(\d+)\s+at\b", output)
    return match.group(1) if match else None


def parse_atq_line(line: str) -> AtqEntry | None:
    parts = line.split()
    if len(parts) < 4 or not parts[0].isdigit():
        return None
    at_job_id, scheduled_for, queue, owner = parts[:4]
    # Validate the timestamp so malformed rows are dropped.
    parse_iso_datetime(scheduled_for)
    return AtqEntry(
        at_job_id=at_job_id,
        scheduled_for=scheduled_for,
        queue=queue,
        owner=owner,
    )


def build_script(job: dict) -> str:
    cmd = build_agent_cmd(job)
    sa_bin = shlex.quote(shutil.which("schedule-agent") or "schedule-agent")
    provenance = job.get("provenance") or {}
    path_entries = provenance.get("path_snapshot_cleaned") or DEFAULT_PATH_ENTRIES
    path_export = ":".join(path_entries)
    return "\n".join(
        [
            f"cd {shlex.quote(job['cwd'])} || exit 1",
            f"export PATH={path_export}",
            f"mkdir -p {shlex.quote(job['log_dir'])} || exit 1",
            'started_at="$(date --iso-8601=seconds)"',
            f'log_file={shlex.quote(job["log_dir"])}/"$started_at".log',
            'exec >>"$log_file" 2>&1',
            (
                f"{sa_bin} mark running {shlex.quote(job['id'])} "
                '--started-at "$started_at" --log-file "$log_file"'
            ),
            (
                'trap \'code=$?; finished_at="$(date --iso-8601=seconds)"; '
                f'if [ "$code" -eq 0 ]; then {sa_bin} mark done {shlex.quote(job["id"])} '
                '--finished-at "$finished_at" --exit-code "$code" --log-file "$log_file"; '
                f"else {sa_bin} mark failed {shlex.quote(job['id'])} "
                '--finished-at "$finished_at" --exit-code "$code" --log-file "$log_file"; fi\' EXIT'
            ),
            f'echo "[schedule-agent] start job={job["id"]} scheduled_for={job["scheduled_for"]}"',
            cmd,
            "",
        ]
    )


def resolve_schedule_spec(value: str) -> str:
    return resolve_schedule_input(value)


def submit_job(job: dict, dry_run: bool = False) -> tuple[str | None, str]:
    script = build_script(job)
    at_time = iso_to_at_time(job["scheduled_for"])
    if dry_run:
        preview = f"Would schedule at {job['scheduled_for']} via `at -t {at_time}`\n\n{script}"
        return None, preview

    proc = _run_at(
        ["at", "-t", at_time],
        input=script,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to schedule job")

    at_job_id = parse_at_job_id(proc.stderr) or parse_at_job_id(proc.stdout)
    output = (proc.stderr + proc.stdout).strip()
    if at_job_id is None:
        raise RuntimeError(f"Could not determine at job id from output: {output!r}")
    return at_job_id, output


def remove_at_job(at_job_id: str) -> tuple[bool, str]:
    proc = _run_at(
        ["atrm", str(at_job_id)],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, proc.stderr.strip()


def query_atq(job_ids: list[str] | None = None) -> tuple[dict[str, AtqEntry], str | None]:
    cmd = ["atq", "-o", ATQ_TIME_FORMAT]
    if job_ids:
        cmd.extend(job_ids)
    try:
        proc = _run_at(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return {}, "atq unavailable"
    if proc.returncode != 0:
        return {}, proc.stderr.strip() or "atq failed"

    entries: dict[str, AtqEntry] = {}
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = parse_atq_line(stripped)
        except Exception:
            entry = None
        if entry:
            entries[entry.at_job_id] = entry
    return entries, None


def query_atq_entry(at_job_id: str) -> tuple[AtqEntry | None, str | None]:
    entries, error = query_atq([at_job_id])
    return entries.get(at_job_id), error


def normalize_legacy_when(value: str | None) -> str | None:
    """Deprecated compatibility wrapper. See `schedule_agent.legacy.compat`."""

    from .legacy.compat import normalize_legacy_when as legacy_normalize_when

    return legacy_normalize_when(value)
