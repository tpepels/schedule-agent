from __future__ import annotations

import importlib
import inspect
from datetime import datetime
from pathlib import Path
from typing import Any

from ..git_context import GitContext
from ..jsonl import read_jsonl_sample
from ..model import SessionCandidate, SourceDiagnostic
from ..paths import claude_project_roots, safe_glob, safe_iterdir, safe_rglob
from . import DiscoveryResult

_SDK_MODULE_NAMES = ("claude_code_sdk", "claude_sdk", "claude")
_SDK_CALL_NAMES = ("list_sessions", "get_sessions")


def _decode_project_key(value: str) -> Path | None:
    if not value.startswith("-"):
        return None
    decoded = "/" + value.lstrip("-").replace("-", "/")
    return Path(decoded)


def _project_key(value: Path) -> str:
    return value.as_posix().replace("/", "-")


def _item_value(item: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        if isinstance(item, dict):
            value = item.get(name)
        else:
            value = getattr(item, name, None)
        if value not in (None, ""):
            return value
    return None


def _parse_sdk_items(
    current: GitContext,
) -> tuple[list[SessionCandidate], bool, str | None, list[str]]:
    warnings: list[str] = []
    module = None
    for module_name in _SDK_MODULE_NAMES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        break
    if module is None:
        return [], False, None, warnings

    session_func = None
    for name in _SDK_CALL_NAMES:
        candidate = getattr(module, name, None)
        if callable(candidate):
            session_func = candidate
            break
    if session_func is None:
        return [], True, "no session listing API", warnings

    kwargs = {}
    try:
        signature = inspect.signature(session_func)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and "cwd" in signature.parameters:
        kwargs["cwd"] = str(current.cwd)

    try:
        items = session_func(**kwargs)
    except Exception as exc:  # noqa: BLE001
        return [], True, str(exc), warnings

    if items is None:
        return [], True, None, warnings

    candidates: list[SessionCandidate] = []
    for item in items:
        session_id = _item_value(item, ("session_id", "id"))
        if not isinstance(session_id, str) or not session_id.strip():
            continue
        session_id = session_id.strip()
        resume_id = _item_value(item, ("resume_id", "session_id", "id"))
        if not isinstance(resume_id, str) or not resume_id.strip():
            resume_id = session_id
        title = _item_value(item, ("custom_title", "title", "summary"))
        cwd_raw = _item_value(item, ("cwd", "path", "workdir"))
        cwd = Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw.strip() else None
        resumable_value = _item_value(item, ("resumable",))
        evidence = ["sdk"]
        confidence = 40
        if isinstance(_item_value(item, ("custom_title",)), str):
            evidence.append("custom title")
            confidence += 10
        elif isinstance(title, str) and title.strip():
            evidence.append("provider title")
            confidence += 10
        warnings_for_item: list[str] = []
        if cwd is not None:
            evidence.append("provider cwd")
            if cwd == current.cwd:
                evidence.append("cwd matches current cwd")
                confidence += 20
            else:
                confidence -= 30
                warnings_for_item.append("cwd mismatch")
        candidates.append(
            SessionCandidate(
                agent="claude",
                session_id=session_id,
                resume_id=resume_id,
                title=title if isinstance(title, str) and title.strip() else None,
                cwd=cwd,
                git_root=None,
                git_branch=None,
                source_path=None,
                source_kind="sdk",
                provider_version=None,
                created_at=None,
                modified_at=None,
                last_user_message=None,
                message_count=None,
                archived=False,
                subagent=False,
                resumable=bool(resumable_value) if resumable_value is not None else True,
                confidence=max(0, min(confidence, 100)),
                evidence=tuple(dict.fromkeys(evidence)),
                warnings=tuple(dict.fromkeys(warnings_for_item)),
            )
        )
    return candidates, True, None, warnings


def _parse_title_and_messages(sample) -> tuple[str | None, str | None, int | None, list[str]]:
    title: str | None = None
    last_user_message: str | None = None
    message_count = 0
    evidence: list[str] = []
    for record in list(sample.head_records) + list(sample.tail_records):
        record_type = record.get("type")
        if title is None and record_type in {"ai-title", "aiTitle"}:
            ai_title = record.get("aiTitle") or record.get("title")
            if isinstance(ai_title, str) and ai_title.strip():
                title = ai_title.strip()
                evidence.append("custom title")
        if record_type != "user" or record.get("isMeta"):
            continue
        message = record.get("message", {})
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            line = content.strip().splitlines()[0]
            if title is None:
                title = line
                evidence.append("first user message title")
            last_user_message = line
            message_count += 1
    return title, last_user_message, message_count or None, evidence


def _candidate_from_file(
    path: Path,
    *,
    current: GitContext,
    source_kind: str,
    weak_source: bool,
) -> SessionCandidate | None:
    sample = read_jsonl_sample(path)
    records = len(sample.head_records) + len(sample.tail_records)
    title, last_user_message, message_count, title_evidence = _parse_title_and_messages(sample)
    evidence = [source_kind]
    evidence.extend(title_evidence)
    warnings = list(sample.warnings)

    project_dir = path.parent
    inferred_cwd: Path | None = None
    if source_kind == "local/jsonl":
        inferred_cwd = current.cwd
        evidence.append("provider cwd")
    else:
        decoded = _decode_project_key(project_dir.name)
        if decoded is not None:
            inferred_cwd = decoded
            evidence.append("inferred cwd")

    confidence = 5 if weak_source else 25
    if records:
        confidence += 10
        evidence.append("valid transcript records")
    else:
        confidence -= 40
        warnings.append("no plausible transcript rows")
    if title:
        confidence += 10
    if inferred_cwd is not None and inferred_cwd == current.cwd:
        confidence += 20
        evidence.append("cwd matches current cwd")
    elif inferred_cwd is not None:
        confidence -= 30
        warnings.append("cwd mismatch")

    modified_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    return SessionCandidate(
        agent="claude",
        session_id=path.stem,
        resume_id=path.stem,
        title=title or path.stem[:8],
        cwd=inferred_cwd,
        git_root=None,
        git_branch=None,
        source_path=path,
        source_kind=source_kind,
        provider_version=None,
        created_at=None,
        modified_at=modified_at,
        last_user_message=last_user_message,
        message_count=message_count,
        archived=False,
        subagent=False,
        resumable=bool(path.stem) and records > 0,
        confidence=max(0, min(confidence, 100)),
        evidence=tuple(dict.fromkeys(evidence)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def discover_claude_sessions(
    *,
    current: GitContext,
) -> DiscoveryResult:
    sdk_candidates, sdk_available, sdk_error, sdk_warnings = _parse_sdk_items(current)
    sources: list[SourceDiagnostic] = [
        SourceDiagnostic(
            agent="claude",
            source_kind="sdk",
            root=None,
            checked=True,
            available=sdk_available,
            candidate_count=len(sdk_candidates),
            error=sdk_error,
        )
    ]
    candidates: list[SessionCandidate] = []
    warnings: list[str] = list(sdk_warnings)
    candidates.extend(sdk_candidates)
    roots = claude_project_roots(current.cwd)
    preferred_key = _project_key(current.cwd)

    for root in roots:
        root_kind = "local/jsonl" if root == current.cwd / ".claude" else "projects/jsonl"
        if not root.exists():
            sources.append(
                SourceDiagnostic(
                    agent="claude",
                    source_kind=root_kind,
                    root=root,
                    checked=True,
                    available=False,
                    candidate_count=0,
                    error=None,
                )
            )
            continue

        root_candidates: list[SessionCandidate] = []
        root_error: str | None = None
        if root_kind == "local/jsonl":
            files, root_error = safe_rglob(root, "*.jsonl")
            for path in files:
                candidate = _candidate_from_file(
                    path,
                    current=current,
                    source_kind=root_kind,
                    weak_source=True,
                )
                if candidate is not None:
                    root_candidates.append(candidate)
        else:
            project_dirs, root_error = safe_iterdir(root)
            if root_error is None:
                ordered = sorted(
                    project_dirs,
                    key=lambda value: (value.name != preferred_key, value.name),
                )
                for project_dir in ordered:
                    if not project_dir.is_dir():
                        continue
                    files, file_error = safe_glob(project_dir, "*.jsonl")
                    if file_error is not None:
                        warnings.append(f"{project_dir}: {file_error}")
                        continue
                    for path in files:
                        candidate = _candidate_from_file(
                            path,
                            current=current,
                            source_kind=root_kind,
                            weak_source=False,
                        )
                        if candidate is not None:
                            root_candidates.append(candidate)
        if root_error is not None:
            warnings.append(f"{root}: {root_error}")
        sources.append(
            SourceDiagnostic(
                agent="claude",
                source_kind=root_kind,
                root=root,
                checked=True,
                available=True,
                candidate_count=len(root_candidates),
                error=root_error,
            )
        )
        candidates.extend(root_candidates)
    return DiscoveryResult(
        candidates=tuple(candidates),
        sources=tuple(sources),
        warnings=tuple(dict.fromkeys(warnings)),
    )
