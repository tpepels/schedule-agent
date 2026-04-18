# Portability & Resilience

**Status:** draft
**Date:** 2026-04-18
**Scope:** hardening schedule-agent against inter-system differences on Linux and against future `claude`/`codex` CLI drift.

## 1. Scope & goals

Linux-only. GNU `at`, GNU coreutils, and `systemd` are assumed to be present. The work concentrates on the failures users actually hit:

- Scheduled jobs that run but can't find `claude` / `codex` / `git` / `node` at 3am because the at-script PATH is too narrow.
- Agent-CLI flag drift breaking jobs silently after an upstream update.
- Opaque diagnostics: today, failures surface as a generic log entry the user has to piece together.
- Cosmetic breakage of the TUI session picker when `~/.claude/projects` or `~/.codex/sessions` schemas shift.
- Fragile `at` output parsing under non-C locales.

### Out of scope

- BSD `at`, macOS, Windows, WSL.
- Moving agent flags into a user-editable config file (Level 3 of the drift-defence continuum). Deferred — may be revisited if drift becomes frequent.
- Auto-bumping known-good agent versions. Every new known-good version is an explicit code change.

### Non-functional constraint: no usability regressions

The software is usable today and this work must not regress that. Concretely:

- Submit-time preflight must be fast (target: under 200 ms on a warm filesystem).
- Preflight WARN-level issues never block a submit.
- A user on a working system (GNU `at`, `atd` running, agent in PATH, known-good version) must see no new prompts, no new confirmations, no new decisions in the new-job flow.
- Old jobs on disk (missing the new `provenance` field) must keep loading and running.
- `doctor` is opt-in — not auto-run, not surfaced in the TUI unless the user asks.

If any proposed change would violate these, the change is wrong.

## 2. Architecture

Two new modules, both pure (no global state, no implicit I/O, straightforward to unit-test):

- **`schedule_agent/environment.py`** — `capture_path()`, `probe_agent()`, and the known-good version / required-help-flag constants. Functions take inputs and return dataclasses. No reads or writes against the job model.
- **`schedule_agent/preflight.py`** — `run_checks()` returns a `PreflightReport`. Consumes `environment` helpers plus scheduler/`atd`/XDG checks. One report shape feeds both submit-time gating and the `doctor` command.

Surgical edits to existing files:

- `scheduler_backend.py` — snapshotted PATH + absolute agent binary path used in `build_script`; `LC_ALL=C` / `LANG=C` injected into all `at` / `atq` / `atrm` subprocess calls.
- `execution.py` — `build_agent_cmd` uses the absolute binary path from the job's `provenance` when present, falls back to the bare name otherwise.
- `operations.py::create_job` — runs preflight, populates `provenance`, raises `OperationError` on critical failure.
- `cli.py` — defensive wrapping of `discover_sessions`; "Paste session ID…" entry added to session pickers; new `doctor` subcommand wired into argparse.

## 3. Agent-CLI drift (Levels 1 + 2)

### Constants

```python
# schedule_agent/environment.py

KNOWN_GOOD_AGENT_VERSIONS: dict[str, set[str]] = {
    "claude": {"2.1.112"},
    "codex":  {"0.120.0"},
}

REQUIRED_AGENT_HELP_SUBSTRINGS: dict[str, list[str]] = {
    "claude": ["--resume", "--dangerously-skip-permissions"],
    "codex":  ["exec", "--dangerously-bypass-approvals-and-sandbox"],
}
```

Pinning 2.1.112 / 0.120.0 corresponds to the versions installed on the development machine at spec-writing time. Known-good version pinning is advisory: unknown versions warn, they do not block. Bumping requires a code change + commit (explicit acknowledgement that we have re-validated against the new CLI).

### Probe

`probe_agent(agent: str) -> AgentProbe` returns:

| Field | Source | Notes |
|-------|--------|-------|
| `resolved_path` | `shutil.which(AGENTS[agent]["bin"])` | `None` if not found |
| `version` | first `\d+\.\d+\.\d+` match in `<bin> --version` stdout | loose regex by design — "2.1.112 (Claude Code)" and "codex-cli 0.120.0" both parse without format-specific code |
| `version_known_good` | version ∈ `KNOWN_GOOD_AGENT_VERSIONS[agent]` | `False` if version is `None` |
| `help_ok` | `<bin> --help` exits 0 AND stdout/stderr contains every required substring | checked via plain substring, not regex — tolerant of help-text reflowing |
| `error` | short human string or `None` | populated if any step above raised |

### Severity mapping

| Condition | Severity |
|-----------|----------|
| `resolved_path is None` | FAIL |
| `help_ok is False` | FAIL |
| `version is None` (could not parse `--version`) | WARN |
| `version_known_good is False` | WARN |
| Happy path | PASS |

FAIL blocks a submit. WARN is recorded on the job's `provenance.preflight.warnings` and surfaced by `doctor`, but never blocks.

## 4. PATH handling (option C: snapshot + sanitize)

`environment.capture_path(raw: str | None = None) -> list[str]`:

1. Source: `raw` argument if provided, else `os.environ.get("PATH", "")`.
2. Split on `os.pathsep`, strip whitespace on each entry.
3. Drop empty entries and relative entries (anything not starting with `/`).
4. Drop entries whose directory does not exist at capture time (stat check).
5. Dedupe preserving order.
6. Append `/usr/local/bin`, `/usr/bin`, `/bin` as a guaranteed floor, skipping entries already present.

The cleaned list is what gets baked into the generated at-script. The raw input is kept too so that diagnostics can explain why a given entry was dropped.

### Provenance on the job

New top-level **`provenance`** object on job JSON:

```json
"provenance": {
  "submitted_at":          "2026-04-18T14:03:11+0200",
  "agent_path":            "/usr/bin/claude",
  "agent_version":         "2.1.112",
  "path_snapshot_raw":     "/home/tom/.local/bin:/usr/bin:.",
  "path_snapshot_cleaned": ["/home/tom/.local/bin", "/usr/bin", "/bin"],
  "preflight": {
    "critical_ok": true,
    "warnings":   ["codex 0.121.0 is untested; last known-good 0.120.0"]
  }
}
```

`provenance` is strictly additive. Legacy jobs without it continue to load. `build_script` reads `provenance.agent_path` and `provenance.path_snapshot_cleaned` when present; when absent, it falls back to the current hardcoded `/usr/local/bin:/usr/bin:/bin` export and the unresolved agent binary name.

## 5. Preflight checks

`preflight.run_checks(include_roundtrip: bool = False) -> PreflightReport`

| Check | Severity on failure | Notes |
|-------|---------------------|-------|
| `at_binary` | FAIL | `at`, `atrm`, `atq` all resolvable via `shutil.which` |
| `atd_active` | FAIL | `systemctl is-active --quiet atd` returns 0; if `systemctl` is absent, SKIP with explanatory message |
| `xdg_dirs` | FAIL | state, data, prompt dirs exist and are writable |
| `agent_claude` | per §3 | via `probe_agent("claude")` |
| `agent_codex` | per §3 | via `probe_agent("codex")` |
| `session_dir_claude` | WARN if dir exists-but-unreadable or no JSONL parses; SKIP if dir absent OR the corresponding agent probe FAILed | `~/.claude/projects` exists and contains at least one readable `.jsonl` file |
| `session_dir_codex` | WARN / SKIP | as above for `~/.codex/sessions` |
| `at_roundtrip` | FAIL | opt-in; submits `true` scheduled one minute out via `at`, confirms `parse_at_job_id` returns non-None, `atrm`s immediately. Only runs under `doctor --verbose`. |

`PreflightReport` exposes:

- `report.critical_failures() -> list[CheckResult]` — the subset used to gate submits
- `report.warnings() -> list[CheckResult]`
- `report.all() -> list[CheckResult]` — for `doctor` printing

### Submit-time gating

`operations.create_job` runs `preflight.run_checks(include_roundtrip=False)` before building the script:

- Any critical failure → raise `OperationError` with a user-facing message derived from the check's `.message` field. No submit attempted.
- Warnings append to `provenance.preflight.warnings`. Submit proceeds as normal.

### Performance

Fast-path preflight calls:
- `shutil.which` for three `at` binaries + two agent binaries (cheap).
- One `systemctl is-active --quiet atd` (cheap).
- Three `os.access` checks on XDG dirs (cheap).
- Two `<agent> --version` calls (the expensive piece; a handful of ms each).
- Two `<agent> --help` calls (similarly fast).

Expected total under 200 ms on a warm filesystem. If we ever observe drift from that target we add a short-lived memoization cache keyed on agent path + mtime.

## 6. `doctor` subcommand

Entry point: `schedule-agent doctor [--verbose] [--quiet] [--json]`.

### Human output

```
$ schedule-agent doctor
at binary           PASS  /usr/bin/at
at daemon           PASS  atd active (systemd)
XDG dirs            PASS  writable
claude CLI          PASS  /usr/bin/claude v2.1.112 (known-good)
codex CLI           WARN  /usr/bin/codex v0.121.0 (untested version; last known-good 0.120.0)
claude session dir  PASS  42 session files
codex session dir   SKIP  ~/.codex/sessions does not exist

1 warning. 0 failures.
```

### Flags

| Flag | Behaviour |
|------|-----------|
| `--verbose` | adds `at_roundtrip` to the check set |
| `--quiet` | prints only non-PASS rows and the summary line |
| `--json` | emits the full `PreflightReport` as JSON; suppresses human output |

### Exit code

`0` if no FAIL. `1` if any FAIL. WARN and SKIP do not affect exit code.

## 7. Locale + subprocess hardening

New private helper in `scheduler_backend`:

```python
def _run_at(cmd, **kwargs):
    env = dict(os.environ)
    env.update({"LC_ALL": "C", "LANG": "C"})
    kwargs.setdefault("env", env)
    return subprocess.run(cmd, **kwargs)
```

All `at`, `atq`, and `atrm` invocations route through `_run_at`. `parse_at_job_id` and `parse_atq_line` stay unchanged; the locale lock makes their regex behaviour deterministic regardless of the user's shell locale.

## 8. Session-discovery resilience

Two layers of defence:

- **In `cli.py::_discover_*`:** wrap `rglob` / `iterdir` in a top-level try/except; return `[]` on any `OSError`. Per-file failures in `extract_session_title` are already handled today and stay handled.
- **In `cli.py` session pickers:** wrap the `discover_sessions(...)` call; on exception, set `state.message = f"Session discovery failed: {exc}"` and present an empty list. The user still gets the picker, they just don't get auto-populated rows.

And the escape hatch:

- **"Paste session ID…"** is added as the last item in every session picker. Selecting it opens an input overlay (same mechanism used elsewhere in the TUI). A pasted ID is accepted without running discovery. This is the manual path when upstream changes have broken our coupling.

## 9. Testing plan

### Unit

- `environment.capture_path`
  - drops empty entries
  - drops relative entries
  - drops entries whose directory does not exist (tmp_path setup)
  - dedupes preserving order
  - appends `/usr/local/bin:/usr/bin:/bin` floor without duplicating
  - survives `PATH` being unset
- `environment.probe_agent` (monkeypatched `subprocess.run`)
  - binary missing → FAIL-shape result
  - `--version` raises → WARN on version, PASS on help if help still runs
  - `--version` returns unexpected text → version `None`, WARN
  - `--help` missing a required substring → FAIL
  - known-good version + full help → PASS
- `preflight.run_checks`
  - monkeypatch each check's underlying probe
  - assert `critical_failures()` contains exactly the FAIL items
  - assert warnings are collected even when critical_ok is True
- `scheduler_backend.build_script`
  - when job has `provenance.agent_path`, script uses that absolute path
  - when job has `provenance.path_snapshot_cleaned`, script exports that PATH
  - legacy job (no `provenance`) → script exports `/usr/local/bin:/usr/bin:/bin` and uses bare agent name (current behaviour)

### Integration

- `operations.create_job`
  - critical preflight failure → `OperationError`, no job on disk, no `at` invocation
  - preflight warnings → job created, warnings recorded in `provenance.preflight.warnings`
- `doctor` CLI
  - exit 0 when all PASS/SKIP
  - exit 0 with WARN present
  - exit 1 when any FAIL
  - `--json` produces parseable JSON matching the `PreflightReport` shape
  - `--quiet` suppresses PASS rows

No test actually invokes `at` / `atd`. The subprocess layer is mocked throughout.

## 10. Migration

- Fully additive to job JSON. Jobs saved before this change load unchanged.
- Retrying or rescheduling an old job goes through the new path — the rewritten script gets the new `provenance`.
- Jobs that never get touched again keep running the old script unchanged.
- Known-good version pins live in Python source; bumping is a code change + commit.
- `schedule-agent doctor` is new; no existing command changes shape or output.
