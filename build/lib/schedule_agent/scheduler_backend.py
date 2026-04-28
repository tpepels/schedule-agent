from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import load_prompt_prefix
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


def _write_prefix_snapshot(job: dict) -> str | None:
    """Freeze the current prompt prefix into the job's log_dir.

    Returns the snapshot path, or None if no prefix is configured. The
    snapshot is written with 0600 perms since it may contain sensitive
    user instructions.
    """
    log_dir = job.get("log_dir")
    if not log_dir:
        return None
    content = load_prompt_prefix(job["agent"])
    snapshot_dir = Path(log_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / "prefix.snapshot"
    snapshot_path.write_text(content, encoding="utf-8")
    try:
        os.chmod(snapshot_path, 0o600)
    except OSError:
        pass
    return str(snapshot_path)


def build_script(job: dict) -> str:
    # Snapshot the prompt prefix at submit time. Mutating the job dict
    # locally (not persisting) means build_script is idempotent while the
    # at-script stably references an immutable per-job snapshot path.
    snapshot_path = _write_prefix_snapshot(job)
    job_with_snapshot = dict(job)
    job_with_snapshot["prefix_snapshot_file"] = snapshot_path
    cmd = build_agent_cmd(job_with_snapshot)
    sa_bin = shlex.quote(shutil.which("schedule-agent") or "schedule-agent")
    provenance = job.get("provenance") or {}
    path_entries = provenance.get("path_snapshot_cleaned") or DEFAULT_PATH_ENTRIES
    path_export = ":".join(path_entries)
    return "\n".join(
        [
            # pipefail: claude's output is piped through a decoder; without
            # this the script would record decoder's exit (always 0) as
            # the job's exit code and silently swallow agent failures.
            "set -o pipefail",
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
    # Consult atq first: if the id is not present, treat as already-gone
    # rather than calling atrm on a potentially-reused id. atq -o format
    # preserves the id's owner so a simple presence check is sufficient —
    # atrm only acts on the invoking user's jobs.
    entry, query_err = query_atq_entry(str(at_job_id))
    if entry is None and not query_err:
        return True, ""
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
