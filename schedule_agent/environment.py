from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from .execution import AGENTS

# Minimum known-good version per agent. Any version >= this is treated as
# compatible; preflight only warns when the probed version is strictly
# older. Override per-agent via env:
#   SCHEDULE_AGENT_MIN_CLAUDE=2.1.112
#   SCHEDULE_AGENT_MIN_CODEX=0.120.0
KNOWN_GOOD_MIN_VERSIONS: dict[str, str] = {
    "claude": "2.1.112",
    "codex": "0.120.0",
}

# Retained for backwards-compat with tests / external callers; now derived.
KNOWN_GOOD_AGENT_VERSIONS: dict[str, set[str]] = {
    agent: {minimum} for agent, minimum in KNOWN_GOOD_MIN_VERSIONS.items()
}


def _parse_version(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for segment in value.split("."):
        digits = "".join(ch for ch in segment if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _min_version_for(agent: str) -> str:
    env_key = f"SCHEDULE_AGENT_MIN_{agent.upper()}"
    return os.environ.get(env_key) or KNOWN_GOOD_MIN_VERSIONS[agent]


def _version_at_least(probed: str, minimum: str) -> bool:
    try:
        return _parse_version(probed) >= _parse_version(minimum)
    except Exception:
        return False


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

    version_known_good = version is not None and _version_at_least(version, _min_version_for(agent))

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
