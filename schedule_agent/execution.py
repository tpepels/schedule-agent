from __future__ import annotations

import shlex

from .legacy.compat import resolve_session_id


def _agent_bin(job: dict, cfg: dict) -> str:
    """Absolute binary path from provenance when present, else the bare name."""
    provenance = job.get("provenance") or {}
    return provenance.get("agent_path") or cfg["bin"]


AGENTS: dict[str, dict] = {
    "codex": {
        "label": "Codex",
        "bin": "codex",
        "base_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
    },
    "claude": {
        "label": "Claude",
        "bin": "claude",
        "base_args": ["-p", "--dangerously-skip-permissions"],
    },
}


def _prompt_expr(prefix_snapshot: str | None, prompt_file: str) -> str:
    """Shell expression that cats the frozen prefix snapshot + prompt file.

    The prefix is snapshotted into the job's log_dir at submit time so a
    later edit of the live prefix (or tampering with it between schedule
    and fire) cannot change what the scheduled job actually sends to the
    agent. `2>/dev/null` keeps the command silent if the snapshot was
    never written (e.g. job scheduled before snapshot plumbing existed).
    """
    prompt = shlex.quote(prompt_file)
    if prefix_snapshot is None:
        return f'"$(cat {prompt})"'
    prefix = shlex.quote(prefix_snapshot)
    return f'"$(cat {prefix} 2>/dev/null; echo; cat {prompt})"'


def build_agent_cmd(job: dict) -> str:
    """Build the shell command to invoke the agent for this job.

    Deprecated compatibility with the old `session` field is isolated under
    `schedule_agent.legacy`. The prefix snapshot is resolved from
    job["prefix_snapshot_file"] if present (written at submit time), else
    omitted — legacy jobs submitted before snapshotting just get the prompt.
    """
    cfg = AGENTS[job["agent"]]
    prompt_expr = _prompt_expr(job.get("prefix_snapshot_file"), job["prompt_file"])
    base = " ".join(cfg["base_args"])
    bin_ = shlex.quote(_agent_bin(job, cfg))

    session_id = resolve_session_id(job)
    session_mode = job.get("session_mode", "resume" if session_id else "new")

    if session_id and session_mode == "resume":
        if job["agent"] == "codex":
            return f"{bin_} exec resume {shlex.quote(session_id)} {prompt_expr} < /dev/null"
        return f"{bin_} --resume {shlex.quote(session_id)} {base} {prompt_expr} < /dev/null"

    return f"{bin_} {base} {prompt_expr} < /dev/null"
