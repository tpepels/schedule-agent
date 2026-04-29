from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from ...time_utils import parse_iso_datetime
from ..git_context import GitContext
from ..jsonl import read_first_json_object, read_jsonl_sample
from ..model import SessionCandidate, SourceDiagnostic
from ..paths import (
    codex_archived_session_roots,
    codex_session_index_paths,
    codex_session_roots,
    codex_sqlite_roots,
    safe_rglob,
)
from ..sqlite import connect_readonly, decode_json_cell, list_tables, table_columns
from . import DiscoveryResult

_SQLITE_LIMIT = 500


def _parse_provider_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_iso_datetime(value).astimezone()
    except ValueError:
        return None


def _find_nested_value(payload: Any, names: tuple[str, ...], *, depth: int = 0) -> Any:
    if depth > 3:
        return None
    if isinstance(payload, dict):
        for name in names:
            value = payload.get(name)
            if value not in (None, ""):
                return value
        for value in payload.values():
            found = _find_nested_value(value, names, depth=depth + 1)
            if found not in (None, ""):
                return found
    if isinstance(payload, list):
        for value in payload:
            found = _find_nested_value(value, names, depth=depth + 1)
            if found not in (None, ""):
                return found
    return None


def _is_subagent_source(source: Any) -> bool:
    return isinstance(source, dict) and any(
        key in source for key in ("subagent", "review", "child")
    )


def _source_tag(source: Any) -> str | None:
    if isinstance(source, str) and source.strip():
        return source.strip()
    if _is_subagent_source(source):
        return "subagent"
    return None


def _title_and_messages(
    sample,
) -> tuple[str | None, str | None, int | None, list[str], dict[str, Any]]:
    title: str | None = None
    last_user_message: str | None = None
    message_count = 0
    evidence: list[str] = []
    meta: dict[str, Any] = {}
    for record in list(sample.head_records) + list(sample.tail_records):
        record_type = record.get("type")
        if record_type == "session_meta":
            payload = record.get("payload")
            if isinstance(payload, dict):
                meta = payload
                provider_title = _find_nested_value(payload, ("title", "thread_name", "name"))
                if title is None and isinstance(provider_title, str) and provider_title.strip():
                    title = provider_title.strip()
                    evidence.append("provider title")
        elif record_type == "event_msg":
            payload = record.get("payload", {})
            if payload.get("type") == "user_message":
                message = payload.get("message")
                if isinstance(message, str) and message.strip():
                    line = message.strip().splitlines()[0]
                    if title is None:
                        title = line
                        evidence.append("first user message title")
                    last_user_message = line
                    message_count += 1
        elif record_type == "response_item":
            payload = record.get("payload", {})
            if payload.get("type") == "message" and payload.get("role") == "user":
                for item in payload.get("content", []):
                    if item.get("type") != "input_text":
                        continue
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        line = text.strip().splitlines()[0]
                        if title is None:
                            title = line
                            evidence.append("first user message title")
                        last_user_message = line
                        message_count += 1
                        break
    return title, last_user_message, message_count or None, evidence, meta


def _mapping_to_candidate(
    payload: dict[str, Any],
    *,
    current: GitContext,
    source_path: Path | None,
    source_kind: str,
    archived: bool,
    source_confidence: int,
) -> SessionCandidate | None:
    session_id = _find_nested_value(payload, ("session_id", "id", "thread_id"))
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    session_id = session_id.strip()
    resume_id = _find_nested_value(payload, ("resume_id", "session_id", "id", "thread_id"))
    if not isinstance(resume_id, str) or not resume_id.strip():
        resume_id = session_id
    title = _find_nested_value(payload, ("title", "thread_title", "thread_name", "name"))
    cwd_raw = _find_nested_value(payload, ("cwd", "workdir", "working_dir", "path"))
    cwd = Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw.strip() else None
    modified_at = _parse_provider_datetime(
        _find_nested_value(payload, ("modified_at", "updated_at", "last_updated_at"))
    )
    created_at = _parse_provider_datetime(_find_nested_value(payload, ("created_at", "started_at")))
    source = _find_nested_value(payload, ("source", "origin"))
    source_tag = _source_tag(source)
    subagent = bool(
        _find_nested_value(payload, ("subagent", "is_subagent"))
    ) or _is_subagent_source(source)
    if isinstance(_find_nested_value(payload, ("kind", "session_kind")), str):
        kind = str(_find_nested_value(payload, ("kind", "session_kind"))).lower()
        subagent = subagent or kind in {"subagent", "review", "child"}
    archived = archived or bool(_find_nested_value(payload, ("archived", "is_archived")))
    evidence = [source_kind]
    warnings: list[str] = []
    confidence = source_confidence
    if source_tag and source_tag != "subagent":
        evidence.append(f"source is {source_tag}")
        confidence += 10
    if title:
        evidence.append("provider title")
        confidence += 10
    if cwd is not None:
        evidence.append("provider cwd")
        if cwd == current.cwd:
            evidence.append("cwd matches current cwd")
            confidence += 20
        else:
            confidence -= 35
            warnings.append("cwd mismatch")
    if archived:
        confidence -= 25
    if subagent:
        confidence -= 60
        warnings.append("subagent session")
    return SessionCandidate(
        agent="codex",
        session_id=session_id,
        resume_id=resume_id,
        title=title,
        cwd=cwd,
        git_root=None,
        git_branch=None,
        source_path=source_path,
        source_kind=source_kind,
        provider_version=None,
        created_at=created_at,
        modified_at=modified_at,
        last_user_message=None,
        message_count=None,
        archived=archived,
        subagent=subagent,
        resumable=bool(resume_id) and not subagent,
        confidence=max(0, min(confidence, 100)),
        evidence=tuple(dict.fromkeys(evidence)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _candidate_from_rollout(
    path: Path,
    *,
    current: GitContext,
    archived: bool,
    weak_source: bool,
) -> SessionCandidate | None:
    sample = read_jsonl_sample(path)
    title, last_user_message, message_count, title_evidence, meta = _title_and_messages(sample)
    source = meta.get("source")
    subagent = _is_subagent_source(source)
    session_id = meta.get("id") if isinstance(meta.get("id"), str) else path.stem
    resume_id = session_id
    cwd_raw = _find_nested_value(meta, ("cwd", "workdir", "working_dir", "path"))
    cwd = Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw.strip() else None
    confidence = 10 if weak_source else 30
    evidence = ["archived_jsonl" if archived else "rollout"]
    evidence.extend(title_evidence)
    warnings = list(sample.warnings)
    if sample.head_records or sample.tail_records:
        evidence.append("valid session metadata")
    else:
        confidence -= 40
        warnings.append("no plausible transcript rows")
    if cwd is not None:
        evidence.append("provider cwd")
        if cwd == current.cwd:
            evidence.append("cwd matches current cwd")
            confidence += 20
        else:
            confidence -= 35
            warnings.append("cwd mismatch")
    source_tag = _source_tag(source)
    if source_tag and source_tag != "subagent":
        confidence += 10
        evidence.append(f"source is {source_tag}")
    if archived:
        confidence -= 25
    if subagent:
        confidence -= 60
        warnings.append("subagent session")
    modified_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    return SessionCandidate(
        agent="codex",
        session_id=session_id,
        resume_id=resume_id,
        title=title or path.stem[:8],
        cwd=cwd,
        git_root=None,
        git_branch=None,
        source_path=path,
        source_kind="archived_jsonl" if archived else "rollout",
        provider_version=None,
        created_at=None,
        modified_at=modified_at,
        last_user_message=last_user_message,
        message_count=message_count,
        archived=archived,
        subagent=subagent,
        resumable=not subagent and bool(resume_id),
        confidence=max(0, min(confidence, 100)),
        evidence=tuple(dict.fromkeys(evidence)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _sqlite_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _discover_sqlite(
    path: Path,
    *,
    current: GitContext,
) -> tuple[list[SessionCandidate], list[str], str | None]:
    tables, error = list_tables(path)
    if error is not None:
        return [], [], error

    warnings: list[str] = []
    candidates: list[SessionCandidate] = []
    usable_table = False
    try:
        with closing(connect_readonly(path)) as conn:
            conn.row_factory = sqlite3.Row
            for table in tables:
                if "session" not in table.lower() and "conversation" not in table.lower():
                    continue
                columns, columns_error = table_columns(path, table)
                if columns_error is not None:
                    warnings.append(f"{path}:{table}: {columns_error}")
                    continue
                if not columns:
                    continue
                usable_table = True
                query = f"SELECT * FROM {_sqlite_identifier(table)} LIMIT {_SQLITE_LIMIT}"
                try:
                    rows = conn.execute(query).fetchall()
                except sqlite3.Error as exc:
                    warnings.append(f"{path}:{table}: {exc}")
                    continue
                for row in rows:
                    payload = {key: row[key] for key in row.keys()}
                    for key, value in list(payload.items()):
                        nested = decode_json_cell(value)
                        if nested is not None:
                            payload[f"{key}_json"] = nested
                    candidate = _mapping_to_candidate(
                        payload,
                        current=current,
                        source_path=path,
                        source_kind="sqlite",
                        archived=False,
                        source_confidence=35,
                    )
                    if candidate is not None:
                        candidates.append(candidate)
    except sqlite3.Error as exc:
        return [], warnings, str(exc)

    if not usable_table:
        warnings.append(f"{path}: unknown schema")
    return candidates, warnings, None


def _discover_index(path: Path, *, current: GitContext) -> tuple[list[SessionCandidate], list[str]]:
    sample = read_jsonl_sample(path, head_lines=250, tail_lines=250)
    warnings = list(sample.warnings)
    candidates: list[SessionCandidate] = []
    for record in list(sample.head_records) + list(sample.tail_records):
        candidate = _mapping_to_candidate(
            record,
            current=current,
            source_path=path,
            source_kind="session_index",
            archived=False,
            source_confidence=30,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates, warnings


def _discover_rollout_root(
    root: Path,
    *,
    current: GitContext,
    archived: bool,
    weak_source: bool,
) -> tuple[list[SessionCandidate], str | None]:
    files, error = safe_rglob(root, "*.jsonl")
    candidates: list[SessionCandidate] = []
    for path in files:
        first = read_first_json_object(path)
        if weak_source:
            if "archived_sessions" in path.parts or "sessions" not in path.parts:
                continue
            if first is None or first.get("type") != "session_meta":
                continue
        elif first is None:
            continue
        candidate = _candidate_from_rollout(
            path,
            current=current,
            archived=archived,
            weak_source=weak_source,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates, error


def discover_codex_sessions(
    *,
    current: GitContext,
) -> DiscoveryResult:
    candidates: list[SessionCandidate] = []
    sources: list[SourceDiagnostic] = []
    warnings: list[str] = []

    for root in codex_sqlite_roots():
        if not root.exists():
            sources.append(
                SourceDiagnostic(
                    agent="codex",
                    source_kind="sqlite",
                    root=root,
                    checked=True,
                    available=False,
                    candidate_count=0,
                    error=None,
                )
            )
            continue
        sqlite_files = sorted(root.glob("state*.sqlite"))
        root_candidates: list[SessionCandidate] = []
        root_error: str | None = None
        for path in sqlite_files:
            extracted, extracted_warnings, error = _discover_sqlite(path, current=current)
            root_candidates.extend(extracted)
            warnings.extend(extracted_warnings)
            if error is not None:
                root_error = error
        sources.append(
            SourceDiagnostic(
                agent="codex",
                source_kind="sqlite",
                root=root,
                checked=True,
                available=True,
                candidate_count=len(root_candidates),
                error=root_error,
            )
        )
        candidates.extend(root_candidates)

    for path in codex_session_index_paths():
        if not path.exists():
            sources.append(
                SourceDiagnostic(
                    agent="codex",
                    source_kind="session_index",
                    root=path,
                    checked=True,
                    available=False,
                    candidate_count=0,
                    error=None,
                )
            )
            continue
        extracted, extracted_warnings = _discover_index(path, current=current)
        warnings.extend(extracted_warnings)
        sources.append(
            SourceDiagnostic(
                agent="codex",
                source_kind="session_index",
                root=path,
                checked=True,
                available=True,
                candidate_count=len(extracted),
                error=None,
            )
        )
        candidates.extend(extracted)

    rollout_roots = list(codex_session_roots(current.cwd))
    archived_roots = list(codex_archived_session_roots())
    for root in rollout_roots + archived_roots:
        archived = root in archived_roots
        weak_source = root == current.cwd / ".codex"
        source_kind = (
            "archived_jsonl" if archived else ("local/jsonl" if weak_source else "rollout")
        )
        if not root.exists():
            sources.append(
                SourceDiagnostic(
                    agent="codex",
                    source_kind=source_kind,
                    root=root,
                    checked=True,
                    available=False,
                    candidate_count=0,
                    error=None,
                )
            )
            continue
        extracted, error = _discover_rollout_root(
            root,
            current=current,
            archived=archived,
            weak_source=weak_source,
        )
        if error is not None:
            warnings.append(f"{root}: {error}")
        sources.append(
            SourceDiagnostic(
                agent="codex",
                source_kind=source_kind,
                root=root,
                checked=True,
                available=True,
                candidate_count=len(extracted),
                error=error,
            )
        )
        candidates.extend(extracted)
    return DiscoveryResult(
        candidates=tuple(candidates),
        sources=tuple(sources),
        warnings=tuple(dict.fromkeys(warnings)),
    )
