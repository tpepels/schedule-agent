from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..time_utils import parse_iso_datetime, title_from_prompt
from .git_context import GitContext, detect_git_context
from .ledger import append_ledger_entry, discover_ledger_sessions
from .model import SessionCandidate, SessionDiscoveryDiagnostic
from .paths import (
    claude_project_roots,
    codex_archived_session_roots,
    codex_session_index_paths,
    codex_session_roots,
    codex_sqlite_roots,
    safe_glob,
    safe_iterdir,
    safe_rglob,
)
from .providers import DiscoveryResult
from .providers.claude import _candidate_from_file as _claude_candidate_from_file
from .providers.claude import discover_claude_sessions
from .providers.codex import _candidate_from_rollout as _codex_candidate_from_rollout
from .providers.codex import _discover_index as _codex_discover_index
from .providers.codex import _discover_sqlite as _codex_discover_sqlite
from .providers.codex import discover_codex_sessions

DEFAULT_DISCOVERY_LIMIT = 20
UNRELATED_CONFIDENCE_FLOOR = 40
RECONCILE_MIN_SCORE = 85
RECONCILE_MIN_MARGIN = 10
SOURCE_PRIORITY = {
    "ledger": 0,
    "sqlite": 1,
    "session_index": 2,
    "rollout": 3,
    "projects/jsonl": 4,
    "local/jsonl": 5,
    "archived_jsonl": 6,
}


def _source_sort_key(kind: str) -> tuple[int, str]:
    return (SOURCE_PRIORITY.get(kind, 99), kind)


def _unique_strings(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _title_rank(candidate: SessionCandidate) -> int:
    evidence = set(candidate.evidence)
    if "custom title" in evidence:
        return 5
    if "provider title" in evidence:
        return 4
    if "index title" in evidence or "thread title" in evidence:
        return 3
    if "first user message title" in evidence:
        return 2
    return 1 if candidate.title else 0


def _cwd_rank(candidate: SessionCandidate) -> int:
    evidence = set(candidate.evidence)
    if "provider cwd" in evidence:
        return 2
    if "inferred cwd" in evidence:
        return 1
    return 0


def _modified_ts(candidate: SessionCandidate) -> float:
    if candidate.modified_at is None:
        return 0.0
    return candidate.modified_at.timestamp()


def _best_title(candidates: list[SessionCandidate]) -> str | None:
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            _title_rank(candidate),
            candidate.confidence,
            _modified_ts(candidate),
        ),
        reverse=True,
    )
    for candidate in ordered:
        if candidate.title:
            return candidate.title
    return None


def _best_cwd(candidates: list[SessionCandidate]) -> Path | None:
    ordered = sorted(
        candidates,
        key=lambda candidate: (_cwd_rank(candidate), candidate.confidence, _modified_ts(candidate)),
        reverse=True,
    )
    for candidate in ordered:
        if candidate.cwd is not None:
            return candidate.cwd
    return None


def _best_git_root(candidates: list[SessionCandidate]) -> Path | None:
    ordered = sorted(
        candidates,
        key=lambda candidate: (candidate.confidence, _modified_ts(candidate)),
        reverse=True,
    )
    for candidate in ordered:
        if candidate.git_root is not None:
            return candidate.git_root
    return None


def _best_git_branch(candidates: list[SessionCandidate]) -> str | None:
    ordered = sorted(
        candidates,
        key=lambda candidate: (candidate.confidence, _modified_ts(candidate)),
        reverse=True,
    )
    for candidate in ordered:
        if candidate.git_branch:
            return candidate.git_branch
    return None


def _merge_candidates(candidates: list[SessionCandidate]) -> SessionCandidate:
    base = max(
        candidates,
        key=lambda candidate: (candidate.confidence, _modified_ts(candidate)),
    )
    combined_sources = "+".join(
        source
        for source in sorted(
            {candidate.source_kind for candidate in candidates},
            key=_source_sort_key,
        )
    )
    combined_evidence = _unique_strings(
        [item for candidate in candidates for item in candidate.evidence]
    )
    combined_warnings = _unique_strings(
        [item for candidate in candidates for item in candidate.warnings]
    )
    modified_at = max(
        (candidate.modified_at for candidate in candidates if candidate.modified_at is not None),
        default=base.modified_at,
    )
    created_at = min(
        (candidate.created_at for candidate in candidates if candidate.created_at is not None),
        default=base.created_at,
    )
    confidence = min(
        100,
        max(candidate.confidence for candidate in candidates) + 5 * (len(candidates) - 1),
    )
    archived = all(candidate.archived for candidate in candidates)
    subagent = any(candidate.subagent for candidate in candidates)
    resumable = (
        any(candidate.resumable for candidate in candidates) and not archived and not subagent
    )
    return replace(
        base,
        title=_best_title(candidates),
        cwd=_best_cwd(candidates),
        git_root=_best_git_root(candidates),
        git_branch=_best_git_branch(candidates),
        source_kind=combined_sources,
        created_at=created_at,
        modified_at=modified_at,
        archived=archived,
        subagent=subagent,
        resumable=resumable,
        confidence=confidence,
        evidence=combined_evidence,
        warnings=combined_warnings,
    )


def _enrich_candidate(candidate: SessionCandidate, *, current: GitContext) -> SessionCandidate:
    git_root = candidate.git_root
    git_branch = candidate.git_branch
    if (
        candidate.cwd is not None
        and candidate.cwd.exists()
        and (git_root is None or git_branch is None)
    ):
        context = detect_git_context(candidate.cwd)
        git_root = git_root or context.git_root
        git_branch = git_branch or context.git_branch
    evidence = list(candidate.evidence)
    warnings = list(candidate.warnings)
    confidence = candidate.confidence
    if git_root is not None and current.git_root is not None and git_root == current.git_root:
        if "git root matches current repo" not in evidence:
            evidence.append("git root matches current repo")
            confidence += 15
    return replace(
        candidate,
        git_root=git_root,
        git_branch=git_branch,
        confidence=min(100, confidence),
        evidence=_unique_strings(evidence),
        warnings=_unique_strings(warnings),
    )


def _merge_results(results: list[DiscoveryResult], *, current: GitContext) -> DiscoveryResult:
    grouped: dict[tuple[str, str], list[SessionCandidate]] = {}
    sources = []
    warnings = []
    for result in results:
        sources.extend(result.sources)
        warnings.extend(result.warnings)
        for candidate in result.candidates:
            enriched = _enrich_candidate(candidate, current=current)
            warnings.extend(enriched.warnings)
            grouped.setdefault((enriched.agent, enriched.session_id), []).append(enriched)
    merged = [_merge_candidates(group) for group in grouped.values()]
    return DiscoveryResult(
        candidates=tuple(merged),
        sources=tuple(sources),
        warnings=_unique_strings(warnings),
    )


def _is_current_cwd(candidate: SessionCandidate, current: GitContext) -> bool:
    return candidate.cwd is not None and candidate.cwd == current.cwd


def _is_same_git_root(candidate: SessionCandidate, current: GitContext) -> bool:
    return (
        candidate.git_root is not None
        and current.git_root is not None
        and candidate.git_root == current.git_root
    )


def _sort_key(candidate: SessionCandidate, *, current: GitContext):
    has_ledger = "ledger" in candidate.source_kind.split("+")
    return (
        0 if _is_current_cwd(candidate, current) else 1,
        0 if _is_same_git_root(candidate, current) else 1,
        0 if has_ledger else 1,
        0 if candidate.resumable else 1,
        -candidate.confidence,
        -_modified_ts(candidate),
        0 if candidate.title else 1,
        len(candidate.warnings),
        candidate.agent,
        candidate.session_id,
    )


def _visible_by_default(
    candidate: SessionCandidate,
    *,
    current: GitContext,
    include_non_resumable: bool,
    all_projects: bool,
) -> bool:
    if include_non_resumable:
        return True
    if not candidate.resumable or candidate.archived or candidate.subagent:
        return False
    if all_projects:
        if _is_current_cwd(candidate, current) or _is_same_git_root(candidate, current):
            return True
        return candidate.confidence >= UNRELATED_CONFIDENCE_FLOOR
    return _is_current_cwd(candidate, current) or _is_same_git_root(candidate, current)


def _discover_provider(agent: str, *, current: GitContext) -> DiscoveryResult:
    if agent == "claude":
        return discover_claude_sessions(current=current)
    if agent == "codex":
        return discover_codex_sessions(current=current)
    return DiscoveryResult()


def _discover(
    *,
    agents: list[str],
    current: GitContext,
    limit: int,
    include_non_resumable: bool,
    all_projects: bool,
) -> tuple[list[SessionCandidate], SessionDiscoveryDiagnostic]:
    results = [discover_ledger_sessions(current=current)]
    for agent in agents:
        results.append(_discover_provider(agent, current=current))
    merged = _merge_results(results, current=current)
    ordered = sorted(merged.candidates, key=lambda candidate: _sort_key(candidate, current=current))
    included = [
        candidate
        for candidate in ordered
        if _visible_by_default(
            candidate,
            current=current,
            include_non_resumable=include_non_resumable,
            all_projects=all_projects,
        )
    ][:limit]
    excluded = [candidate for candidate in ordered if candidate not in included]
    diagnostic = SessionDiscoveryDiagnostic(
        sources=merged.sources,
        excluded=tuple(excluded),
        warnings=merged.warnings,
    )
    return included, diagnostic


def discover_sessions(
    agent: str,
    cwd: Path | None = None,
    limit: int = DEFAULT_DISCOVERY_LIMIT,
    include_non_resumable: bool = False,
    all_projects: bool = False,
) -> list[SessionCandidate]:
    current = detect_git_context(cwd)
    sessions, _diagnostic = _discover(
        agents=[agent],
        current=current,
        limit=limit,
        include_non_resumable=include_non_resumable,
        all_projects=all_projects,
    )
    return sessions


def discover_all_sessions(
    cwd: Path | None = None,
    limit: int = DEFAULT_DISCOVERY_LIMIT,
    include_non_resumable: bool = False,
    all_projects: bool = False,
) -> list[SessionCandidate]:
    current = detect_git_context(cwd)
    sessions, _diagnostic = _discover(
        agents=["claude", "codex"],
        current=current,
        limit=limit,
        include_non_resumable=include_non_resumable,
        all_projects=all_projects,
    )
    return sessions


def diagnose_sessions(
    agent: str | None = None,
    cwd: Path | None = None,
) -> SessionDiscoveryDiagnostic:
    current = detect_git_context(cwd)
    agents = [agent] if agent else ["claude", "codex"]
    _sessions, diagnostic = _discover(
        agents=agents,
        current=current,
        limit=DEFAULT_DISCOVERY_LIMIT,
        include_non_resumable=True,
        all_projects=True,
    )
    return diagnostic


def _artifact_paths(agent: str, *, cwd: Path) -> list[Path]:
    files: list[Path] = []
    if agent == "claude":
        for root in claude_project_roots(cwd):
            if not root.exists():
                continue
            if root == cwd / ".claude":
                root_files, _error = safe_rglob(root, "*.jsonl")
                files.extend(root_files)
                continue
            project_dirs, _error = safe_iterdir(root)
            for project_dir in project_dirs:
                if not project_dir.is_dir():
                    continue
                root_files, _file_error = safe_glob(project_dir, "*.jsonl")
                files.extend(root_files)
        return files

    if agent != "codex":
        return files

    for root in codex_sqlite_roots():
        if not root.exists():
            continue
        files.extend(path for path in root.glob("state*.sqlite") if path.is_file())
    for path in codex_session_index_paths():
        if path.exists():
            files.append(path)
    archived_roots = set(codex_archived_session_roots())
    for root in list(codex_session_roots(cwd)) + list(archived_roots):
        if not root.exists():
            continue
        root_files, _error = safe_rglob(root, "*.jsonl")
        for path in root_files:
            if root == cwd / ".codex" and (
                "archived_sessions" in path.parts or "sessions" not in path.parts
            ):
                continue
            files.append(path)
    return files


def snapshot_provider_artifacts(agent: str, cwd: Path | None = None) -> dict[str, dict[str, int]]:
    current = detect_git_context(cwd)
    snapshot: dict[str, dict[str, int]] = {}
    for path in _artifact_paths(agent, cwd=current.cwd):
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path)] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
    return snapshot


def _diff_artifact_snapshots(
    before: dict[str, dict[str, int]] | None,
    after: dict[str, dict[str, int]],
) -> list[Path]:
    previous = before or {}
    changed: list[Path] = []
    for path_str, current in after.items():
        prior = previous.get(path_str)
        if (
            prior is None
            or prior.get("mtime_ns") != current.get("mtime_ns")
            or prior.get("size") != current.get("size")
        ):
            changed.append(Path(path_str))
    return changed


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _parse_changed_candidates(
    agent: str,
    *,
    current: GitContext,
    changed_paths: list[Path],
) -> list[SessionCandidate]:
    candidates: list[SessionCandidate] = []
    for path in changed_paths:
        if agent == "claude":
            weak_source = _path_is_under(path, current.cwd / ".claude")
            source_kind = "local/jsonl" if weak_source else "projects/jsonl"
            candidate = _claude_candidate_from_file(
                path,
                current=current,
                source_kind=source_kind,
                weak_source=weak_source,
            )
            if candidate is not None:
                candidates.append(candidate)
            continue

        if path.name.startswith("state") and path.suffix == ".sqlite":
            extracted, _warnings, _error = _codex_discover_sqlite(path, current=current)
            candidates.extend(extracted)
            continue
        if path.name == "session_index.jsonl":
            extracted, _warnings = _codex_discover_index(path, current=current)
            candidates.extend(extracted)
            continue
        if path.suffix != ".jsonl":
            continue
        archived = "archived_sessions" in path.parts
        weak_source = _path_is_under(path, current.cwd / ".codex") and not archived
        candidate = _codex_candidate_from_rollout(
            path,
            current=current,
            archived=archived,
            weak_source=weak_source,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _job_prompt_fragment(job: dict[str, Any]) -> str | None:
    prompt_file = job.get("prompt_file")
    if not isinstance(prompt_file, str) or not prompt_file:
        return None
    path = Path(prompt_file)
    if not path.exists():
        return None
    return title_from_prompt(path.read_text(encoding="utf-8"))


def _normalize_text(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _score_reconcile_candidate(
    candidate: SessionCandidate,
    *,
    current: GitContext,
    job: dict[str, Any],
    changed_paths: set[str],
    prompt_fragment: str | None,
    finished_at: str,
) -> tuple[int, list[str]]:
    score = candidate.confidence
    evidence: list[str] = []
    if candidate.source_path is not None and str(candidate.source_path) in changed_paths:
        score += 30
        evidence.append("artifact changed during job")
    if candidate.cwd is not None and candidate.cwd == current.cwd:
        score += 20
        evidence.append("cwd matched job cwd")
    job_git_root = job.get("git_root")
    if (
        isinstance(job_git_root, str)
        and candidate.git_root is not None
        and str(candidate.git_root) == job_git_root
    ):
        score += 15
        evidence.append("git root matched job git root")
    requested_resume_id = job.get("session_id")
    if (
        isinstance(requested_resume_id, str)
        and requested_resume_id
        and (
            candidate.resume_id == requested_resume_id
            or candidate.session_id == requested_resume_id
        )
    ):
        score += 25
        evidence.append("requested resume id matched candidate")
    started_at = job.get("last_started_at")
    if (
        isinstance(started_at, str)
        and candidate.modified_at is not None
        and parse_iso_datetime(started_at).timestamp() - 60
        <= candidate.modified_at.timestamp()
        <= parse_iso_datetime(finished_at).timestamp() + 60
    ):
        score += 20
        evidence.append("new artifact appeared after job start")
    normalized_prompt = _normalize_text(prompt_fragment)
    normalized_last = _normalize_text(candidate.last_user_message)
    if (
        normalized_prompt
        and normalized_last
        and (
            normalized_prompt.startswith(normalized_last)
            or normalized_last.startswith(normalized_prompt)
        )
    ):
        score += 15
        evidence.append("first user message matched prompt prefix")
    if candidate.archived or candidate.subagent:
        score -= 50
    return score, evidence


def reconcile_job_session(
    job: dict[str, Any],
    *,
    finished_at: str,
    exit_code: int,
) -> dict[str, Any]:
    cwd = Path(job.get("cwd") or Path.cwd())
    current = detect_git_context(cwd)
    before_snapshot = job.get("session_artifact_snapshot")
    if not isinstance(before_snapshot, dict):
        before_snapshot = {}
    after_snapshot = snapshot_provider_artifacts(job["agent"], cwd=current.cwd)
    changed_paths = _diff_artifact_snapshots(before_snapshot, after_snapshot)
    candidates = _parse_changed_candidates(
        job["agent"], current=current, changed_paths=changed_paths
    )

    requested_resume_id = job.get("session_id")
    if isinstance(requested_resume_id, str) and requested_resume_id:
        known_sessions = discover_sessions(
            job["agent"],
            cwd=current.cwd,
            limit=50,
            include_non_resumable=True,
            all_projects=True,
        )
        for candidate in known_sessions:
            if (
                candidate.resume_id == requested_resume_id
                or candidate.session_id == requested_resume_id
            ):
                candidates.append(candidate)

    seen: dict[tuple[str, str], SessionCandidate] = {}
    for candidate in candidates:
        seen[(candidate.agent, candidate.session_id)] = candidate
    unique_candidates = list(seen.values())

    prompt_fragment = _job_prompt_fragment(job)
    scored: list[tuple[int, SessionCandidate, list[str]]] = []
    changed_set = {str(path) for path in changed_paths}
    for candidate in unique_candidates:
        score, evidence = _score_reconcile_candidate(
            candidate,
            current=current,
            job=job,
            changed_paths=changed_set,
            prompt_fragment=prompt_fragment,
            finished_at=finished_at,
        )
        scored.append((score, candidate, evidence))
    scored.sort(key=lambda item: item[0], reverse=True)

    summary: dict[str, Any] = {
        "changed_paths": [str(path) for path in changed_paths],
        "matched_session_id": None,
        "matched_resume_id": None,
        "confidence": None,
        "warning": None,
        "ledger_written": False,
    }
    if not scored:
        summary["warning"] = "no changed session artifacts matched job"
        return summary

    best_score, best_candidate, best_evidence = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else None
    if best_score < RECONCILE_MIN_SCORE:
        summary["warning"] = f"no high-confidence session match ({best_score})"
        return summary
    if second_score is not None and best_score - second_score < RECONCILE_MIN_MARGIN:
        summary["warning"] = f"ambiguous session match ({best_score} vs {second_score})"
        return summary

    ledger_entry = {
        "schema_version": 1,
        "agent": job["agent"],
        "schedule_job_id": job["id"],
        "cwd": str(current.cwd),
        "git_root": str(current.git_root) if current.git_root is not None else job.get("git_root"),
        "git_branch": current.git_branch or job.get("git_branch"),
        "requested_session_mode": job.get("session_mode"),
        "requested_resume_id": requested_resume_id,
        "discovered_session_id": best_candidate.session_id,
        "discovered_resume_id": best_candidate.resume_id,
        "discovered_title": best_candidate.title,
        "discovered_source_kind": best_candidate.source_kind,
        "discovered_source_path": str(best_candidate.source_path)
        if best_candidate.source_path
        else None,
        "started_at": job.get("last_started_at"),
        "finished_at": finished_at,
        "exit_code": exit_code,
        "confidence": best_score,
        "evidence": [*best_evidence, *best_candidate.evidence],
        "warnings": list(best_candidate.warnings),
        "archived": best_candidate.archived,
        "subagent": best_candidate.subagent,
        "message_count": best_candidate.message_count,
        "last_user_message": best_candidate.last_user_message,
    }
    append_ledger_entry(ledger_entry)
    summary.update(
        {
            "matched_session_id": best_candidate.session_id,
            "matched_resume_id": best_candidate.resume_id,
            "confidence": best_score,
            "ledger_written": True,
        }
    )
    return summary
