# COMBINED AUDIT

Scope: `schedule-agent` v0.2.0 — a Python CLI/TUI that queues Claude/Codex invocations onto the system `at(1)` scheduler. Linux-only, single-user, local. ~4,600 LOC source + ~3,000 LOC tests.

## EXECUTIVE SUMMARY

The project has a clean domain model (state_model / transitions / operations) and decent preflight tooling. Real release blockers are narrow and concrete:

1. `save_jobs` rewrites the queue jsonl non-atomically — a crash mid-write corrupts every user's job queue.
2. Documented CLI flag `--dry-run` is not wired through `argparse` — the README lies.
3. The empty-state hint tells users to press `N` to create a job, but `N` = "run now"; `A` = add. First-run users hit a dead end.
4. The TUI (1,200+ lines inside `cli.py`) has almost no automated coverage.

The product is otherwise coherent: invariants are checked on every write, the `at`-queue roundtrip is covered by `doctor --roundtrip`, shell-quoting of user-controlled strings is consistent. By-design risk (running agents with `--dangerously-skip-permissions` unattended) is clearly documented in the README.

Verdict: **not ready** — see blocker list.

---

## ARCHITECTURE

### BLOCKERS

**Non-atomic queue write**
- type: architectural
- severity: blocker
- evidence: `schedule_agent/persistence.py:106-112` — `save_jobs` calls `queue_file.write_text(...)`. No tempfile-then-rename, no fsync. fcntl lock in `operations.py:81-88` serializes writers but does not protect against mid-write crashes (SIGKILL, power loss, OOM).
- risk: partial write corrupts `agent_queue.jsonl`; `load_jobs` then marks every record `_invalid` or loses records entirely. No backup. Users lose their whole queue including scheduled `at_job_id` bindings, leaving orphan `at` jobs whose callbacks no longer match.
- fix: write to `queue_file.with_suffix(".jsonl.tmp")`, `os.replace` onto target, optional `fsync`. Keep the existing fcntl lock.

### FINDINGS

**`cli.py` is a 2,209-line grab bag**
- type: code quality
- severity: high
- evidence: `schedule_agent/cli.py` holds argparse, all non-TUI command handlers, the full `prompt_toolkit` jobs screen (state, rendering, keybindings, overlays), column-layout helpers, and the staged new-job flow.
- risk: any TUI change has to round-trip the whole file; unit testability is low (see tests section); new contributors can't find anything. Structural drift as the TUI grows.
- fix: split at least `cli.py` into `cli/commands.py` (argparse + non-interactive handlers), `cli/tui/screen.py`, `cli/tui/render.py`, `cli/tui/actions.py`. No behavior change required.

**Legacy compatibility leaks out of `legacy/`**
- type: architectural
- severity: medium
- evidence: `persistence.py:40-62,132-141`, `scheduler_backend.py:161-166`, `time_utils.py:74-79` are thin wrappers that re-export `legacy/compat.py`. `cli.py:19-20,52-56` still exposes `load_state/save_state/set_state/clear_state/cancel_at_job` at the module level.
- risk: the stated `schedule_agent.legacy` boundary is decorative. Any "remove legacy" task has to touch the core modules, which re-introduces the risk of breaking live job records.
- fix: make the wrappers raise `DeprecationWarning` at import time, or delete them and inline the two or three real call sites.

**Module-level side effects at import**
- type: architectural
- severity: medium
- evidence: `cli.py:72` — `STATE_DIR, DATA_DIR, QUEUE_FILE, STATE_FILE, PROMPT_DIR = _make_paths()` runs `_ensure_dirs()` at import, which `mkdir -p`s four XDG locations before `main()` is even called.
- risk: importing `schedule_agent.cli` (e.g. from a test, a packaging tool, `schedule-agent --help`) creates directories in the user's home. Breaks hermetic tests, surprises packagers, and couples import order to filesystem state.
- fix: move directory preparation to `main()` / lazy accessors.

**Known-good agent versions hardcoded**
- type: operational
- severity: medium
- evidence: `environment.py:11-14` pins `claude=2.1.112`, `codex=0.120.0`.
- risk: every upstream bump produces a WARN in `doctor` output. Users will learn to ignore it, which defeats the purpose of the check.
- fix: widen to a version range (minimum known-good), or move the list to config and refresh it via CI.

**Stale-running recovery threshold via env only**
- type: operational
- severity: low
- evidence: `operations.py:91-97` reads `SCHEDULE_AGENT_STALE_MINUTES`, defaults to 60.
- risk: undocumented (not in README). A long but quiet agent run (64 minutes with no log writes) will be force-marked failed mid-execution.
- fix: document in README; consider using log-mtime only (not the started-at wall clock) as the primary signal.

---

## SECURITY

### BLOCKERS

_None._ The product deliberately runs agents with `--dangerously-bypass-approvals-and-sandbox` / `--dangerously-skip-permissions`; this is documented as the threat model, not a vulnerability.

### FINDINGS

**Prompt-prefix file read at execution time via shell concat**
- category: injection / tampering
- type: risk
- severity: medium
- evidence: `execution.py:29-38` — script line is literally `"$(cat $prefix 2>/dev/null; echo; cat $prompt)"`. The prefix is resolved when `atd` fires the job, not when the user scheduled it.
- attack: any process with write access to `~/.config/schedule-agent/prompt-prefix-claude.md` (e.g. a hostile dependency dropped into a venv the user ran once) can substitute a malicious prefix between schedule-time and run-time and exfiltrate via the agent's tool use. Because the run happens unattended, the user never reviews it.
- fix: snapshot the prefix into the prompt file (or a sibling file) at submit time; the script `cat`s that immutable snapshot. The prefix edit UI remains, but edits affect future jobs only.

**Log files may contain agent-generated secrets**
- category: data exposure
- type: hardening
- severity: medium
- evidence: `scheduler_backend.py:76` — `exec >>"$log_file" 2>&1` captures stdout+stderr of the agent under `~/.local/state/schedule-agent/logs/<job>/`. No rotation, no redaction, default 0644 create perms inherited from umask.
- attack: any other local user reads `~/.local/state/...` if umask is permissive or the home dir mode is 0755 (default on many distros). Agents regularly echo API keys, credentials, repo paths into output.
- fix: `os.umask(0o077)` or `chmod 0700` the logs dir; document that logs may contain secrets; offer a retention policy (delete logs on job delete — currently `_apply_scheduler_mutation` does delete the directory, good, but orphan jobs leave logs).

**`at -t` output parsing relies on forced C locale only**
- category: injection / parser
- type: hardening
- severity: low
- evidence: `scheduler_backend.py:28-32` sets `LC_ALL=C, LANG=C`. `parse_at_job_id` regex `\bjob\s+(\d+)\s+at\b`.
- attack: if a vendor `at` fork emits a different phrasing ("job #42 scheduled at..."), `submit_job` raises `"Could not determine at job id"`. Failure mode is a thrown RuntimeError, not a silent mis-binding, so it is self-healing — but the critical dependency on exact wording is not tested across `at` implementations.
- fix: fall back to `atq` diff (list before, list after, take the new id) when regex fails.

**No verification that `at_job_id` still belongs to us before `atrm`**
- category: abuse / race
- type: risk
- severity: low
- evidence: `scheduler_backend.py:122-128` — `remove_at_job` calls `atrm <id>` by number only.
- attack: if the stored `at_job_id` ever collides with a later unrelated `at` job (e.g. after atd restart + id reuse, or manual `atrm`), we remove the wrong job. `query_atq_entry` would notice the mismatch but `remove_at_job` doesn't consult it.
- fix: compare `entry.owner` / `entry.queue` against expectation before removing; treat absence as success without calling `atrm`.

**`install.sh` runs prerequisite checks above `set -e`**
- category: install-time integrity
- type: hardening
- severity: low
- evidence: `install.sh:1-45` executes `check_atd_running` and `check_prereq` calls before the `#!/usr/bin/env bash` / `set -e` declaration on line 47. Script still works (shebang is only read at `exec`), but errors in the pre-check region don't abort cleanly.
- attack: none — local install script. But a failing prereq check that exits non-zero is dependent on each function's explicit `exit 1`; anything else silently proceeds.
- fix: move `set -e` / `set -u` to the top; the stray shebang on line 47 is dead.

**No CSRF/XSS/SSRF surface**
- category: n/a
- type: hardening
- severity: low
- evidence: no network, no server, no browser-facing code.
- fix: none.

---

## FEATURE

### BLOCKERS

**`--dry-run` is documented but not wired**
- type: missing
- severity: blocker
- evidence: `README.md:139` advertises `schedule-agent --dry-run`; `schedule()` in `cli.py:384-386` accepts the flag and `submit_job(..., dry_run=True)` in `scheduler_backend.py:99-104` prints a preview. But `build_arg_parser` (`cli.py:1985-2074`) defines no `--dry-run` global flag and no code path calls `schedule(..., dry_run=True)`. The documented command silently falls through to the interactive TUI.
- impact: first-time users following the README to preview before submitting get a full-screen TUI instead of the stated preview output. Hurts trust in the docs.
- fix: add `parser.add_argument("--dry-run", action="store_true")` at the top level, plumb through `create_job` → `_resubmit` → `submit_job`. Or remove the line from README.

**Wrong hint in empty-state TUI**
- type: broken
- severity: blocker
- evidence: `cli.py:1373` — `"No jobs. Press N to create one.\n"`. But `cli.py:1696-1699` binds `a` to `start_new_job_flow`, and `cli.py:1701-1708` binds `n` to "run now" (reschedule selected job to now + 1m). With zero jobs, pressing `N` does nothing (`state.message = "No job selected."`), leaving the screen looking frozen.
- impact: blocks the primary happy path — a new user opens the TUI, sees one job of instruction, presses it, gets nothing. Hot path for the product.
- fix: change the hint to "Press A to add one." Also update the inline help-hint footer (`cli.py:1432-1448`) — it already says `Add`, but the user still needs to map that to the `A` key.

### FINDINGS

**CLI has no way to edit prompt prefix**
- type: missing
- severity: medium
- evidence: prompt prefix is edited only via the TUI `P` key (`cli.py:1719-1722` → `start_prefix_edit_flow` → `action_edit_prefix`). There is no `schedule-agent edit-prefix {claude|codex}` subcommand.
- impact: scripted deployment / CI setups can't seed prefixes. Users with `EDITOR` set to a GUI-hostile editor in terminal sessions can't escape the TUI to edit.
- fix: add `edit-prefix <agent>` to `build_arg_parser`.

**No `--version` flag**
- type: missing
- severity: low
- evidence: `build_arg_parser` has no `version` action. `pyproject.toml:7` declares `0.2.0` but users can't ask the CLI.
- impact: bug reports lack version. `doctor` doesn't print own version either.
- fix: `parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")`.

**No notification/callback when scheduled job finishes**
- type: missing
- severity: medium
- evidence: README frames the whole value prop as "come back to the result." Only surfacing is TUI list + `schedule-agent list` polling. `mark_finished` (`operations.py:525-552`) writes to disk but fires no hook.
- impact: the user has to re-check manually. Defeats the "just let it run overnight" story for the target use case.
- fix: optional post-job hook — command from config fired after `mark_finished`. Keep opt-in (desktop notifications are out of scope for Linux-only).

**Deprecated subcommands still aliased with `SUPPRESS`**
- type: polish
- severity: low
- evidence: `cancel` → `unschedule` (`cli.py:2007-2008,2150-2152`), `session` → `set-session` (`cli.py:2025-2028`). Both emit deprecation warnings.
- impact: noise. If you're considering release, decide whether to keep or drop them before 1.0 — removing post-release is harder.
- fix: for v0.2.x keep, for v1.0 cut.

**`retry` command requires a schedule spec**
- type: ux
- severity: low
- evidence: `cli.py:2030-2032` — `schedule-agent retry <id> <when>`. No default like "now + 1 minute".
- impact: the common case ("re-run this failed job now") is two tokens longer than it should be.
- fix: make `when` optional, default to `now + 1 minute` to match the `N` key behavior.

**No doctor check for prompt-prefix file existence/readability**
- type: operational
- severity: low
- evidence: `preflight.py:333-351` — no check for `~/.config/schedule-agent/prompt-prefix-*.md`. The file is auto-created on first TUI edit / submit via `ensure_prompt_prefix`; a scheduled job running before that file exists will hit the `2>/dev/null` silent-skip path.
- impact: silently missing prefix = user's "you are executing autonomously" instruction never reaches the agent. Degrades output quality without warning.
- fix: `check_prompt_prefix(agent)` in preflight: warn if missing, pass if readable.

---

## TESTS

### BLOCKERS

**Interactive TUI is effectively untested**
- type: gap
- severity: blocker
- evidence: `tests/test_cli.py` is 137 lines; only `test_jobs_menu_requires_prompt_toolkit_when_run_interactively` touches `jobs_menu`. None of: overlay state machine, staged new-job flow, picker navigation, keybinding dispatch, summary column computation, renderers, search filtering. These are >1,000 of the 2,209 lines in `cli.py`.
- impact: the primary UX path is a single regression away from breaking, and because it fails at runtime in a full-screen app, CI won't catch it.
- fix: test the parts that don't need a live terminal first — `_summary_columns`, `render_summary_row`, `_layout_mode`, `_resolve_offset_pick`/`_resolve_clock_pick`, overlay transitions driven directly through the `overlay_*_key` helpers. `prompt_toolkit` can be exercised in its mock mode for keybinding tests.

### FINDINGS

**Empty-state hint bug slipped through**
- type: gap
- severity: high
- evidence: see feature blocker above. No test asserts the text of `summary_fragments()` when `cached_jobs` is empty, so the `N`/`A` mismatch was invisible.
- impact: confirms the test gap is not theoretical.
- fix: add a test that renders the empty-state fragments and checks the hinted key matches a live keybinding.

**`conftest.py` reloads every module on every test**
- type: noise
- severity: low
- evidence: `tests/conftest.py:26-31` — `importlib.reload(...)` on six modules per `app_modules` fixture invocation.
- impact: hides import-time side effects (see architecture finding about `_make_paths` at import). A test that depends on module-level state picks up whatever the last reload produced.
- fix: move the module-level state out; the reloads can then go.

**Heavy monkeypatching on `cli.*` names**
- type: weak test
- severity: medium
- evidence: `test_cli.py:24-37,48-66,73-93` replaces `cli.list_job_views`, `cli.get_job_view`, `cli.cli_reschedule_job`, etc. These tests assert dispatch, not behavior.
- impact: refactors that rename internals pass the tests while breaking real flows. Many tests are verifying the argparse wiring only.
- fix: complement with at least one end-to-end test that creates a job via `create_job`, runs `main(["list"])`, and parses the printed output.

**No test touches `at`/`atq`/`atrm` via real processes**
- type: gap
- severity: medium
- evidence: `test_scheduler_backend.py` (168 lines) and `test_scheduler_backend_extra.py` (12 lines) mock `subprocess.run`. The only real invocation is `preflight.check_at_roundtrip` — behind `--roundtrip` and not exercised in CI.
- impact: a change to the `at` output regex or the `-t` format wouldn't be caught until a user hits it.
- fix: opt-in integration test marked `@pytest.mark.integration` that runs when `at` is present in CI (ubuntu-latest has it). Low cost; high realism for a tool whose entire purpose is shelling out to `at`.

**Test files use module-level `_` prefixed APIs**
- type: misaligned
- severity: low
- evidence: e.g. `test_cli.py:27` calls `app_modules.operations._job_with_scheduler(job)`.
- impact: tests are coupled to private helpers that don't have stability guarantees.
- fix: expose a thin public `job_view(job)` for tests and callers alike.

---

## UI

Scope: `schedule-agent` ships a terminal TUI built with `prompt_toolkit`, not a web UI. Findings are about TUI clarity, not visual design.

### BLOCKERS

**Empty-state instructs the wrong key**
- severity: blocker
- evidence: `cli.py:1373` says `Press N to create one`, but the binding is `A` (`cli.py:1696`).
- problem: new users stare at a screen that tells them to do something that doesn't work. The footer hint `Add` (with the underlined `A`) contradicts the inline copy, compounding confusion.
- fix: change the string to `Press A to add one.` — one character.

### FINDINGS

**Status is conveyed primarily by colour**
- severity: medium
- evidence: `cli.py:1932-1953` — status classes `status-running` / `status-failed` / `status-completed` etc. each set distinct `fg:` colours. The textual label (`Running`, `Failed`, etc.) is also rendered, mitigating the issue.
- problem: terminals without colour (CI logs, screen readers, NO_COLOR env) lose the fast-scan affordance. Only the label differentiates; colour difference between `Waiting` and `Blocked` is the only cue on most terminals.
- fix: prepend a glyph per status (`✓`, `✗`, `·`, `!`) so that colour is redundant, not primary.

**Footer help hint is easy to miss**
- severity: low
- evidence: `cli.py:1450-1456` renders 15 single-letter hints space-separated across one line. On narrow terminals it wraps ugly; on wide ones it blends into the reverse-video footer band.
- problem: discoverability of less common keys (`F` scope, `/` search, `U` unschedule) is low. New users will only find `?`.
- fix: show only 5-6 high-signal hints by default; rest via `?`. Or group them with dividers.

**Narrow mode drops detail pane**
- severity: low
- evidence: `cli.py:1878-1883` — detail pane hidden when width < 80 cols. There is an overlay detail pane for narrow mode, but `show_detail` has to be toggled.
- problem: acceptable on tiny terminals but there's no visible hint that detail exists / how to reveal it in narrow mode.
- fix: footer hint "d: detail" when narrow.

**"Paste session ID" uses a label-as-sentinel**
- severity: low
- evidence: `cli.py:325` — `PASTE_SESSION_LABEL = "Paste session ID..."` is both the label displayed and the sentinel value passed through the picker callback (`cli.py:1247-1256`, `cli.py:1761-1767`).
- problem: if a discovered session ever has the title `Paste session ID...` (very unlikely, but possible from the extracted first line of a prompt), the picker treats it as the paste sentinel.
- fix: use a distinct object sentinel (`PASTE_SESSION = object()`) as the value; keep the label as the display string.

**Job search is title-only**
- severity: low
- evidence: `cli.py:630-632` — substring match on `job["title"]`. No match against agent, session id prefix, status.
- problem: if you titled two jobs similarly but want to find "the Codex one that failed", you can't.
- fix: match against a concatenation of title, agent, display_label.

---

## TOP RELEASE BLOCKERS

1. **Non-atomic queue write** (`persistence.py:106-112`) — corruption on crash. Fix: write-then-rename.
2. **`--dry-run` documented, not implemented** (`cli.py:1985-2074` / `README.md:139`) — docs lie.
3. **TUI empty-state hint points to wrong key** (`cli.py:1373`) — first-run dead end.
4. **TUI has no automated coverage** (`tests/test_cli.py`) — primary UX path is untested.

## QUICK WINS

- Rewrite queue save as atomic rename (~10 lines, `persistence.py`).
- Fix `"Press N"` → `"Press A"` in empty-state fragment.
- Add `--version` to argparse.
- Widen known-good agent version pin to a minimum range or env override.
- Status glyph prepended to `display_label` for colour-blind / no-colour terminals.
- `os.umask(0o077)` on startup or `chmod 0700` the `logs/` dir.
- Default `retry <id>` to `now + 1 minute` when `when` is omitted.
- Add `check_prompt_prefix` to preflight.

## DEEPER REFACTORS

- Split `cli.py` (2,209 lines) into `cli/commands.py` + `cli/tui/`. Unblocks TUI testability.
- Move module-level directory creation out of `cli.py` import side effects; lazy-init in `main()`.
- Collapse `legacy/compat.py` re-exports now living in `persistence`/`scheduler_backend`/`time_utils` back into the `legacy/` boundary, or delete the wrappers.
- Snapshot prompt prefix at submit time instead of resolving it via shell `cat` when the job fires. Removes the tamper-between-schedule-and-run risk and gives a stable record of what ran.
- Post-job hook (user-configured command fired after `mark_finished`) so the "come back to the result" promise doesn't require polling.
- Integration test tier that exercises real `at`/`atq`/`atrm` on ubuntu-latest — the whole product is glue between Python and `at(1)`; mocking that interface hides the only real risk.

## VERDICT

**not ready**

reason: the queue jsonl is rewritten non-atomically (single crash destroys a user's entire state), a documented CLI flag (`--dry-run`) is missing, the empty-state hint in the main TUI points to the wrong key (blocking first-run onboarding), and the ~1,200-line TUI ships with essentially no automated coverage. Each is individually fixable in under a day, but all four need to land before a clean public release.
