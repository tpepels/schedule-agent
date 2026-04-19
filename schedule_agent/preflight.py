from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .environment import (
    KNOWN_GOOD_AGENT_VERSIONS,
    AgentProbe,
    probe_agent,
)
from .persistence import _ensure_dirs
from .scheduler_backend import parse_at_job_id
from .time_utils import ISO_SECONDS_FORMAT, iso_to_at_time

Severity = str  # "PASS" | "WARN" | "FAIL" | "SKIP"


@dataclass(frozen=True)
class CheckResult:
    name: str
    label: str
    severity: Severity
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class PreflightReport:
    results: list[CheckResult]

    def critical_failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.severity == "FAIL"]

    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.severity == "WARN"]

    def all(self) -> list[CheckResult]:
        return list(self.results)

    def critical_ok(self) -> bool:
        return not self.critical_failures()


def check_at_binary() -> CheckResult:
    """Verify the `at`, `atrm`, and `atq` binaries are all resolvable on PATH."""
    resolved = {name: shutil.which(name) for name in ("at", "atrm", "atq")}
    missing = [name for name, path in resolved.items() if path is None]
    if missing:
        return CheckResult(
            name="at_binary",
            label="at binary",
            severity="FAIL",
            message=f"missing binaries: {', '.join(missing)}",
            detail={"resolved": resolved},
        )
    return CheckResult(
        name="at_binary",
        label="at binary",
        severity="PASS",
        message=str(resolved["at"]),
        detail={"resolved": resolved},
    )


def check_atd_active() -> CheckResult:
    """Verify the atd daemon is active via systemctl, when available."""
    if shutil.which("systemctl") is None:
        return CheckResult(
            name="atd_active",
            label="at daemon",
            severity="SKIP",
            message="systemctl not available",
        )
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", "--quiet", "atd"],
            timeout=5,
        )
    except Exception as exc:
        return CheckResult(
            name="atd_active",
            label="at daemon",
            severity="FAIL",
            message=f"systemctl invocation failed: {exc}",
        )
    if proc.returncode == 0:
        return CheckResult(
            name="atd_active",
            label="at daemon",
            severity="PASS",
            message="atd active (systemd)",
        )
    return CheckResult(
        name="atd_active",
        label="at daemon",
        severity="FAIL",
        message="atd not active; start with `sudo systemctl enable --now atd`",
        detail={"returncode": proc.returncode},
    )


def check_xdg_dirs() -> CheckResult:
    """Verify the XDG state/data/prompt dirs exist and are writable."""
    try:
        state_dir, data_dir, prompt_dir, _logs_dir, _queue_file = _ensure_dirs()
    except Exception as exc:
        return CheckResult(
            name="xdg_dirs",
            label="XDG dirs",
            severity="FAIL",
            message=f"failed to prepare dirs: {exc}",
        )
    dirs = {
        "state": state_dir,
        "data": data_dir,
        "prompt": prompt_dir,
    }
    not_writable = [
        f"{label} ({path})" for label, path in dirs.items() if not os.access(path, os.W_OK)
    ]
    detail = {k: str(v) for k, v in dirs.items()}
    if not_writable:
        return CheckResult(
            name="xdg_dirs",
            label="XDG dirs",
            severity="FAIL",
            message=f"not writable: {', '.join(not_writable)}",
            detail=detail,
        )
    return CheckResult(
        name="xdg_dirs",
        label="XDG dirs",
        severity="PASS",
        message="writable",
        detail=detail,
    )


def check_agent(agent: str, probe: AgentProbe | None = None) -> CheckResult:
    """Check an agent CLI: presence, help sanity, and known-good version."""
    if probe is None:
        probe = probe_agent(agent)
    label = f"{agent} CLI"
    detail = {
        "resolved_path": probe.resolved_path,
        "version": probe.version,
        "version_known_good": probe.version_known_good,
        "help_ok": probe.help_ok,
    }
    name = f"agent_{agent}"

    if probe.resolved_path is None:
        return CheckResult(
            name=name,
            label=label,
            severity="FAIL",
            message=f"{agent} binary not found in PATH",
            detail=detail,
        )
    if probe.help_ok is False:
        err_suffix = f" ({probe.error})" if probe.error else ""
        return CheckResult(
            name=name,
            label=label,
            severity="FAIL",
            message=f"{agent} --help failed or missing required flags{err_suffix}",
            detail=detail,
        )
    if probe.version is None:
        return CheckResult(
            name=name,
            label=label,
            severity="WARN",
            message="unable to parse version from --version output",
            detail=detail,
        )
    if probe.version_known_good is False:
        known = sorted(KNOWN_GOOD_AGENT_VERSIONS[agent])
        return CheckResult(
            name=name,
            label=label,
            severity="WARN",
            message=f"{probe.version} is untested; last known-good: {known}",
            detail=detail,
        )
    return CheckResult(
        name=name,
        label=label,
        severity="PASS",
        message=f"{probe.resolved_path} v{probe.version} (known-good)",
        detail=detail,
    )


_SESSION_DIRS = {
    "claude": Path.home() / ".claude" / "projects",
    "codex": Path.home() / ".codex" / "sessions",
}


def check_session_dir(agent: str, agent_probe_severity: str) -> CheckResult:
    """Check that the agent's session discovery directory is readable."""
    name = f"session_dir_{agent}"
    label = f"{agent} session dir"
    session_dir = _SESSION_DIRS[agent]

    if agent_probe_severity == "FAIL":
        return CheckResult(
            name=name,
            label=label,
            severity="SKIP",
            message="skipped because agent probe failed",
            detail={"path": str(session_dir)},
        )
    if not session_dir.exists():
        return CheckResult(
            name=name,
            label=label,
            severity="SKIP",
            message=f"{session_dir} does not exist",
            detail={"path": str(session_dir)},
        )
    if not os.access(session_dir, os.R_OK):
        return CheckResult(
            name=name,
            label=label,
            severity="WARN",
            message=f"{session_dir} is not readable",
            detail={"path": str(session_dir)},
        )
    try:
        jsonl_files = list(session_dir.rglob("*.jsonl"))
    except OSError as exc:
        return CheckResult(
            name=name,
            label=label,
            severity="WARN",
            message=f"error scanning {session_dir}: {exc}",
            detail={"path": str(session_dir)},
        )
    if not jsonl_files:
        return CheckResult(
            name=name,
            label=label,
            severity="WARN",
            message="no session files found",
            detail={"path": str(session_dir)},
        )
    return CheckResult(
        name=name,
        label=label,
        severity="PASS",
        message=f"{len(jsonl_files)} session files",
        detail={"path": str(session_dir), "count": len(jsonl_files)},
    )


def _c_locale_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"LC_ALL": "C", "LANG": "C"})
    return env


def check_at_roundtrip() -> CheckResult:
    """Submit a trivial `at` job one minute in the future and remove it."""
    label = "at roundtrip"
    name = "at_roundtrip"
    env = _c_locale_env()
    scheduled = (datetime.now().astimezone() + timedelta(minutes=1)).replace(
        second=0, microsecond=0
    )
    at_time = iso_to_at_time(scheduled.strftime(ISO_SECONDS_FORMAT))

    at_job_id: str | None = None
    try:
        try:
            proc = subprocess.run(
                ["at", "-t", at_time],
                input="true\n",
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )
        except Exception as exc:
            return CheckResult(
                name=name,
                label=label,
                severity="FAIL",
                message=f"at invocation failed: {exc}",
            )
        if proc.returncode != 0:
            return CheckResult(
                name=name,
                label=label,
                severity="FAIL",
                message=(proc.stderr or proc.stdout or "at submit failed").strip(),
                detail={"returncode": proc.returncode},
            )
        at_job_id = parse_at_job_id(proc.stderr) or parse_at_job_id(proc.stdout)
        if at_job_id is None:
            return CheckResult(
                name=name,
                label=label,
                severity="FAIL",
                message=f"could not parse at job id from: {(proc.stderr + proc.stdout).strip()!r}",
            )
        return CheckResult(
            name=name,
            label=label,
            severity="PASS",
            message=f"submitted and removed test job (id={at_job_id})",
            detail={"at_job_id": at_job_id},
        )
    finally:
        if at_job_id is not None:
            try:
                subprocess.run(
                    ["atrm", str(at_job_id)],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=5,
                )
            except Exception:
                pass


def run_checks(include_roundtrip: bool = False) -> PreflightReport:
    """Run every preflight check and return a PreflightReport."""
    results: list[CheckResult] = []
    results.append(check_at_binary())
    results.append(check_atd_active())
    results.append(check_xdg_dirs())

    claude_result = check_agent("claude")
    results.append(claude_result)
    codex_result = check_agent("codex")
    results.append(codex_result)

    results.append(check_session_dir("claude", claude_result.severity))
    results.append(check_session_dir("codex", codex_result.severity))

    if include_roundtrip:
        results.append(check_at_roundtrip())

    return PreflightReport(results=results)
