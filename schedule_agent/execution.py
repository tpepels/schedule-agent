from __future__ import annotations

import shlex

from .legacy.compat import resolve_session_id

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


def build_agent_cmd(job: dict) -> str:
    """Build the shell command to invoke the agent for this job.

    Deprecated compatibility with the old `session` field is isolated under
    `schedule_agent.legacy`.
    """
    cfg = AGENTS[job["agent"]]
    prompt_path = shlex.quote(job["prompt_file"])
    base = " ".join(cfg["base_args"])

    session_id = resolve_session_id(job)
    session_mode = job.get("session_mode", "resume" if session_id else "new")

    if session_id and session_mode == "resume":
        if job["agent"] == "codex":
            return (
                f"{cfg['bin']} exec resume {shlex.quote(session_id)} "
                f'"$(cat {prompt_path})" < /dev/null'
            )
        return (
            f"{cfg['bin']} --resume {shlex.quote(session_id)} {base} "
            f'"$(cat {prompt_path})" < /dev/null'
        )

    return f'{cfg["bin"]} {base} "$(cat {prompt_path})" < /dev/null'
