from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class SessionCandidate:
    agent: str
    session_id: str
    resume_id: str
    title: str | None

    cwd: Path | None
    git_root: Path | None
    git_branch: str | None

    source_path: Path | None
    source_kind: str
    provider_version: str | None

    created_at: datetime | None
    modified_at: datetime | None

    last_user_message: str | None
    message_count: int | None

    archived: bool
    subagent: bool
    resumable: bool

    confidence: int
    evidence: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SourceDiagnostic:
    agent: str
    source_kind: str
    root: Path | None
    checked: bool
    available: bool
    candidate_count: int
    error: str | None


@dataclass(frozen=True)
class SessionDiscoveryDiagnostic:
    sources: tuple[SourceDiagnostic, ...]
    excluded: tuple[SessionCandidate, ...]
    warnings: tuple[str, ...]
