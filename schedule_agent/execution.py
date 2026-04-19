from __future__ import annotations

import shlex

from .config import prompt_prefix_path
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


def _prompt_expr(agent: str, prompt_file: str) -> str:
    """Shell expression that cats the prefix (if present) + prompt file.

    Using `2>/dev/null` on the prefix keeps the command silent if the user
    deletes it between edits. An intermediate `echo` guarantees a newline
    separator even if the prefix file lacks a trailing newline.
    """
    prefix = shlex.quote(str(prompt_prefix_path(agent)))
    prompt = shlex.quote(prompt_file)
    return f'"$(cat {prefix} 2>/dev/null; echo; cat {prompt})"'


def build_agent_cmd(job: dict) -> str:
    """Build the shell command to invoke the agent for this job.

    Deprecated compatibility with the old `session` field is isolated under
    `schedule_agent.legacy`.
    """
    cfg = AGENTS[job["agent"]]
    prompt_expr = _prompt_expr(job["agent"], job["prompt_file"])
    base = " ".join(cfg["base_args"])
    bin_ = shlex.quote(_agent_bin(job, cfg))

    session_id = resolve_session_id(job)
    session_mode = job.get("session_mode", "resume" if session_id else "new")

    if session_id and session_mode == "resume":
        if job["agent"] == "codex":
            return f"{bin_} exec resume {shlex.quote(session_id)} {prompt_expr} < /dev/null"
        return f"{bin_} --resume {shlex.quote(session_id)} {base} {prompt_expr} < /dev/null"

    return f"{bin_} {base} {prompt_expr} < /dev/null"
