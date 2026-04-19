from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from .execution import AGENTS

KNOWN_GOOD_AGENT_VERSIONS: dict[str, set[str]] = {
    "claude": {"2.1.112"},
    "codex": {"0.120.0"},
}

REQUIRED_AGENT_HELP_SUBSTRINGS: dict[str, list[str]] = {
    "claude": ["--resume", "--dangerously-skip-permissions"],
    "codex": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
}

PATH_FLOOR = ["/usr/local/bin", "/usr/bin", "/bin"]

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


@dataclass(frozen=True)
class AgentProbe:
    agent: str
    resolved_path: str | None
    version: str | None
    version_known_good: bool
    help_ok: bool
    error: str | None


def capture_path(raw: str | None = None) -> list[str]:
    """Return a sanitized PATH entry list with a guaranteed floor."""
    source = raw if raw is not None else os.environ.get("PATH", "")
    result: list[str] = []
    seen: set[str] = set()
    for entry in source.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        if not entry.startswith("/"):
            continue
        if not os.path.isdir(entry):
            continue
        if entry in seen:
            continue
        seen.add(entry)
        result.append(entry)
    for floor in PATH_FLOOR:
        if floor in seen:
            continue
        if not os.path.isdir(floor):
            continue
        seen.add(floor)
        result.append(floor)
    return result


def probe_agent(agent: str) -> AgentProbe:
    """Probe an agent binary for presence, version, and help output."""
    bin_name = AGENTS[agent]["bin"]
    resolved_path = shutil.which(bin_name)
    if resolved_path is None:
        return AgentProbe(
            agent=agent,
            resolved_path=None,
            version=None,
            version_known_good=False,
            help_ok=False,
            error="binary not found",
        )

    version: str | None = None
    error: str | None = None
    try:
        proc = subprocess.run(
            [resolved_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        match = _VERSION_RE.search((proc.stdout or "") + (proc.stderr or ""))
        if match:
            version = match.group(0)
    except Exception as exc:
        error = str(exc)
        version = None

    version_known_good = version is not None and version in KNOWN_GOOD_AGENT_VERSIONS[agent]

    help_ok = False
    try:
        proc = subprocess.run(
            [resolved_path, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        help_ok = proc.returncode == 0 and all(
            token in combined for token in REQUIRED_AGENT_HELP_SUBSTRINGS[agent]
        )
    except Exception as exc:
        help_ok = False
        error = str(exc)

    return AgentProbe(
        agent=agent,
        resolved_path=resolved_path,
        version=version,
        version_known_good=version_known_good,
        help_ok=help_ok,
        error=error,
    )
