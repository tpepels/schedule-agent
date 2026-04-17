from __future__ import annotations

import shlex

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

    Supports both the new model (session_mode/session_id) and the legacy
    model (session field) so it works transparently during migration.
    """
    cfg = AGENTS[job["agent"]]
    prompt_path = shlex.quote(job["prompt_file"])
    base = " ".join(cfg["base_args"])

    # Support both new model and legacy model
    session_id = job.get("session_id") or job.get("session")
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
