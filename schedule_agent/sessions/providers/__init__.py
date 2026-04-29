from __future__ import annotations

from dataclasses import dataclass, field

from ..model import SessionCandidate, SourceDiagnostic


@dataclass(frozen=True)
class DiscoveryResult:
    candidates: tuple[SessionCandidate, ...] = field(default_factory=tuple)
    excluded: tuple[SessionCandidate, ...] = field(default_factory=tuple)
    sources: tuple[SourceDiagnostic, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
