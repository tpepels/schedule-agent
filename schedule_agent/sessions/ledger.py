from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..time_utils import parse_iso_datetime
from .git_context import GitContext
from .model import SessionCandidate, SourceDiagnostic
from .paths import session_ledger_path
from .providers import DiscoveryResult

LEDGER_SCHEMA_VERSION = 1


def _parse_datetime(value: Any):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_iso_datetime(value).astimezone()
    except ValueError:
        return None


def append_ledger_entry(entry: dict[str, Any]) -> None:
    path = session_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = dict(entry)
    payload.setdefault("schema_version", LEDGER_SCHEMA_VERSION)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False))
        handle.write("\n")


def _candidate_from_row(row: dict[str, Any]) -> SessionCandidate | None:
    agent = row.get("agent")
    session_id = row.get("discovered_session_id")
    resume_id = row.get("discovered_resume_id") or session_id
    if not isinstance(agent, str) or not agent.strip():
        return None
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    cwd = row.get("cwd")
    git_root = row.get("git_root")
    source_path = row.get("discovered_source_path")
    evidence = tuple(
        str(item) for item in row.get("evidence", []) if isinstance(item, str) and item.strip()
    )
    warnings = tuple(
        str(item) for item in row.get("warnings", []) if isinstance(item, str) and item.strip()
    )
    archived = bool(row.get("archived"))
    subagent = bool(row.get("subagent"))
    return SessionCandidate(
        agent=agent.strip(),
        session_id=session_id.strip(),
        resume_id=str(resume_id).strip(),
        title=row.get("discovered_title") if isinstance(row.get("discovered_title"), str) else None,
        cwd=Path(cwd) if isinstance(cwd, str) and cwd.strip() else None,
        git_root=Path(git_root) if isinstance(git_root, str) and git_root.strip() else None,
        git_branch=row.get("git_branch") if isinstance(row.get("git_branch"), str) else None,
        source_path=(
            Path(source_path) if isinstance(source_path, str) and source_path.strip() else None
        ),
        source_kind="ledger",
        provider_version=None,
        created_at=_parse_datetime(row.get("started_at")),
        modified_at=_parse_datetime(row.get("finished_at") or row.get("started_at")),
        last_user_message=(
            row.get("last_user_message") if isinstance(row.get("last_user_message"), str) else None
        ),
        message_count=(
            row.get("message_count") if isinstance(row.get("message_count"), int) else None
        ),
        archived=archived,
        subagent=subagent,
        resumable=bool(resume_id) and not archived and not subagent,
        confidence=int(row.get("confidence", 95)),
        evidence=tuple(dict.fromkeys(("ledger entry", *evidence))),
        warnings=warnings,
    )


def discover_ledger_sessions(*, current: GitContext) -> DiscoveryResult:
    del current
    path = session_ledger_path()
    if not path.exists():
        return DiscoveryResult(
            sources=(
                SourceDiagnostic(
                    agent="all",
                    source_kind="ledger",
                    root=path,
                    checked=True,
                    available=False,
                    candidate_count=0,
                    error=None,
                ),
            ),
        )

    candidates: list[SessionCandidate] = []
    warnings: list[str] = []
    invalid_rows = 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    invalid_rows += 1
                    warnings.append(f"{path}: invalid JSON on line {line_number}")
                    continue
                if not isinstance(row, dict):
                    invalid_rows += 1
                    warnings.append(f"{path}: non-object row on line {line_number}")
                    continue
                candidate = _candidate_from_row(row)
                if candidate is None:
                    invalid_rows += 1
                    warnings.append(f"{path}: invalid ledger row on line {line_number}")
                    continue
                candidates.append(candidate)
    except OSError as exc:
        warnings.append(f"{path}: {exc}")
    if invalid_rows:
        warnings.append(f"{path}: skipped {invalid_rows} invalid row(s)")

    return DiscoveryResult(
        candidates=tuple(candidates),
        sources=(
            SourceDiagnostic(
                agent="all",
                source_kind="ledger",
                root=path,
                checked=True,
                available=True,
                candidate_count=len(candidates),
                error=None,
            ),
        ),
        warnings=tuple(dict.fromkeys(warnings)),
    )
