# CLI + Interaction Refinement Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the multi-dimensional state model fully visible and safely actionable from the CLI, without adding features.

**Architecture:** The CLI is a thin layer over the transition engine (`transitions.py`). All display changes live in `format_job_label` / `show_job_text`. All messaging is printed stdout before/after the transition call. No raw state mutations in the CLI layer.

**Tech Stack:** Python, argparse, prompt_toolkit (interactive TUI), `at`/`atrm` scheduler backend.

---

## Codebase map (files in scope)

| File | Role |
|------|------|
| `schedule_agent/state_model.py` | `derive_display_state`, `can_*` guards |
| `schedule_agent/transitions.py` | All state mutations (`on_reschedule`, `on_cancel`, etc.) |
| `schedule_agent/cli.py` | All CLI commands, display helpers, `apply_job_update` |
| `tests/test_cli.py` | CLI unit tests |
| `tests/test_cli_external_interaction.py` | Integration tests for CLI commands |

---

## 1. CLI Command Surface Refinement

### Keep unchanged (semantics are correct)
| Command | Status |
|---------|--------|
| `list` | Keep — output format needs refinement only |
| `show <id>` | Keep — detail view needs more fields |
| `delete <id>` | Keep — add state messaging |
| `reschedule <id> <when>` | Keep — add state messaging |
| `retry <id>` | Keep — add output |
| `mark running\|done\|failed <id>` | Keep — internal, no user-facing changes needed |
| `notify-dependency <id> success\|failed` | Keep — internal |

### Rename
| Old | New | Reason |
|-----|-----|--------|
| `session <id> [session] [--new]` | `set-session <id> [session] [--new]` | `session` is a noun; `set-session` is a verb phrase that matches the other command verbs (`retry`, `reschedule`, `delete`) |

### Add
| Command | Purpose |
|---------|---------|
| `cancel <id>` | Cancel without deleting. Sets `submission=cancelled`, removes `at` job if scheduled. Distinct from `delete` — the job record remains, giving the user audit trail. |
| `submit <id>` | Manually submit a queued/ready job to `at`. Needed because a job may be saved as queued (e.g. after `retry`) without being submitted. |

### Evaluate `--dry-run`
Currently only wired to the interactive create flow. `reschedule` and `submit` should also respect it in a future pass (out of scope for this plan).

---

## 2. State Display Specification

### 2a. Combined display state (primary)

`derive_display_state()` already collapses the four dimensions into one of:
`cancelled` | `running` | `failed` | `done` | `blocked` | `waiting` | `scheduled` | `queued`

This is the primary label shown in all list views.

### 2b. When to expose raw dimensions

| View | Show |
|------|------|
| `list` | Display state only (primary) |
| `show <id>` | Display state + all four raw dimensions + `at_job_id` + `depends_on` + timestamps |

Rationale: `list` must be scannable. `show` is where users debug.

### 2c. `schedule-agent list` — output format

Columns (fixed-width, tab-separated):

```
<id>                     <display_state>  <agent>   <session>  [depends: <dep_id>]
```

Rules:
- `<id>`: left-aligned, padded to longest id in the list (max 40 chars, truncated with `…` if longer)
- `<display_state>`: left-aligned, fixed width 10 (longest value is `cancelled` = 9)
- `<agent>`: left-aligned, fixed width 8 (`claude` = 6, `codex` = 5)
- `<session>`: `new` or `resume` (not the full session UUID — too noisy for a list)
- `[depends: <dep_id>]`: only shown if `depends_on` is set; same truncation as `<id>`

**Example:**

```text
claude-20260414-030000  scheduled   claude    resume
codex-20260414-090000   waiting     codex     new      depends: claude-20260414-030000
codex-20260414-120000   failed      codex     resume
codex-20260414-150000   blocked     claude    new      depends: codex-20260414-120000
claude-20260414-180000  done        claude    new
```

**Edge case markers** (suffix, space-separated after last column):
- `[prompt missing]` — prompt file does not exist on disk
- `[!]` — invariant violation detected (should never happen in practice)

**No jobs case:**
```text
No jobs.
```

### 2d. `schedule-agent show <id>` — output format

```text
id:            claude-20260414-030000
display:       scheduled
  submission:  scheduled
  execution:   pending
  readiness:   ready
  session:     resume
agent:         claude
when:          03:00 tomorrow
at_job_id:     42
depends_on:    -
session_id:    abc123def456...  (truncated to 40 chars)
cwd:           /home/tom/project
log:           /home/tom/project/log-20260414-030000.txt
prompt_file:   /home/tom/.local/share/schedule-agent/agent_prompts/claude-20260414-030000.md
prompt_exists: yes
created_at:    2026-04-14 01:00:00
updated_at:    2026-04-14 01:00:05
last_run_at:   -
```

Rules:
- `at_job_id`: show `-` if None
- `depends_on`: show `-` if not set; if set also show `dependency_condition` (default `success`): `depends_on: claude-20260414-030000 (condition: success)`
- `session_id`: show `-` if `session_mode=new`; truncate UUID at 40 chars
- `last_run_at`: show `-` if None
- `prompt_exists`: check `Path(job["prompt_file"]).exists()` and print `yes` or `no (file not found)`
- Raw dimensions (submission/execution/readiness/session) are shown indented under `display:` to make the hierarchy clear

---

## 3. Transition Messaging Rules

### Principle

Every mutating command prints a structured message to stdout. Format:

```
<job_id>: <human action summary>
  <before/after lines for changed fields>
  [warning lines if applicable]
```

Warnings are printed but do not block the action. No interactive confirmation in non-interactive mode. This is a power tool.

### Rule table

| Condition | Warning message |
|-----------|----------------|
| Job was `scheduled`; `at` job will be removed | `  note: at job <N> removed` |
| `reschedule` on `done`/`failed` job resets execution | `  note: execution state reset to pending` |
| `retry` on a job with `depends_on` where dependency hasn't succeeded | `  note: dependency <dep_id> has not resolved; readiness forced to ready` |
| `delete` on a job that has dependents | `  warning: dependent jobs [<id>, ...] remain in their current state` |
| `set-session` on a `running` job | `  warning: job is currently running; session change takes effect on next run` |
| At job removal fails during `cancel`/`delete`/`reschedule` | `  warning: atrm failed for at job <N>: <error>` |

### Per-command messaging

#### `schedule-agent list` (read-only, no messaging)

#### `schedule-agent show <id>` (read-only, no messaging)

#### `schedule-agent reschedule <id> <when>`

```
<job_id>: rescheduled
  when:     <old_when> → <new_when>
  [note: at job <N> removed]         (if was scheduled)
  [note: resubmitted as at job <M>]  (if was scheduled and resubmit succeeded)
  [note: execution state reset to pending]  (if execution was success|failed)
  [warning: resubmit failed: <error>; job left as queued]  (if resubmit failed)
```

#### `schedule-agent set-session <id> <session>`

```
<job_id>: session updated
  session:  <old_mode>[:<old_id>] → <new_mode>[:<new_id>]
  [note: at job <N> removed]
  [note: resubmitted as at job <M>]
  [warning: job is currently running; session change takes effect on next run]
```

#### `schedule-agent retry <id>`

```
<job_id>: reset for retry
  execution: failed → pending
  readiness: <old> → ready
  [note: dependency <dep_id> has not resolved; readiness forced to ready]
```

#### `schedule-agent cancel <id>`

```
<job_id>: cancelled
  submission: <old> → cancelled
  [note: at job <N> removed]
  [warning: atrm failed for at job <N>: <error>]
```

#### `schedule-agent submit <id>`

```
<job_id>: submitted
  submission: queued → scheduled
  at_job_id: <N>  (for: <when>)
```

Error cases:

```
error: job <id> is not in a submittable state
  current: <display_state> (submission=<s>, execution=<e>, readiness=<r>)
  required: submission=queued, execution=pending, readiness=ready
```

#### `schedule-agent delete <id>`

```
<job_id>: deleted
  [note: at job <N> removed]
  [warning: atrm failed for at job <N>: <error>]
  [warning: dependent jobs [<dep_id>, ...] remain in their current state]
```

#### `schedule-agent mark running|done|failed <id>` (internal, minimal output)

No output on success. On error:
```
error: no such job: <id>
```

#### `schedule-agent notify-dependency <id> success|failed` (internal)

No output on success. On error:
```
error: no such job: <id>
```

---

## 4. Example Outputs

### `schedule-agent list`

```
claude-20260414-030000  scheduled   claude    resume
codex-20260414-090000   waiting     codex     new      depends: claude-20260414-030000
codex-20260414-120000   failed      codex     resume   [prompt missing]
codex-20260414-150000   blocked     claude    new      depends: codex-20260414-120000
claude-20260414-180000  done        claude    new
```

### `schedule-agent show claude-20260414-030000`

```
id:            claude-20260414-030000
display:       scheduled
  submission:  scheduled
  execution:   pending
  readiness:   ready
  session:     resume
agent:         claude
when:          03:00 tomorrow
at_job_id:     42
depends_on:    -
session_id:    abc123def456ghi789jkl012mno345pqr678stu9
cwd:           /home/tom/Projects/myproject
log:           /home/tom/Projects/myproject/log-20260414-030000.txt
prompt_file:   /home/tom/.local/share/schedule-agent/agent_prompts/claude-20260414-030000.md
prompt_exists: yes
created_at:    2026-04-14 01:00:00
updated_at:    2026-04-14 01:00:05
last_run_at:   -
```

### `schedule-agent reschedule claude-20260414-030000 "04:00 tomorrow"`

```
claude-20260414-030000: rescheduled
  when:     03:00 tomorrow → 04:00 tomorrow
  note: at job 42 removed
  note: resubmitted as at job 51
```

### `schedule-agent reschedule codex-20260414-120000 "now + 30 minutes"`

(job was in `failed` state)

```
codex-20260414-120000: rescheduled
  when:     03:00 tomorrow → now + 30 minutes
  note: execution state reset to pending
```

### `schedule-agent set-session claude-20260414-030000 newid999`

```
claude-20260414-030000: session updated
  session:  resume:abc123def456... → resume:newid999
  note: at job 42 removed
  note: resubmitted as at job 52
```

### `schedule-agent retry codex-20260414-120000`

```
codex-20260414-120000: reset for retry
  execution: failed → pending
  readiness: ready → ready
```

### `schedule-agent cancel claude-20260414-030000`

```
claude-20260414-030000: cancelled
  submission: scheduled → cancelled
  note: at job 42 removed
```

### `schedule-agent submit codex-20260414-120000`

```
codex-20260414-120000: submitted
  submission: queued → scheduled
  at_job_id: 43  (for: now + 30 minutes)
```

### `schedule-agent delete codex-20260414-090000`

(job has a dependent)

```
codex-20260414-090000: deleted
  warning: dependent jobs [codex-20260414-150000] remain in their current state
```

---

## 5. Edge Case Handling

### Blocked jobs

Display: `blocked` in list. In `show`, show `readiness: blocked` and `depends_on: <dep_id> (condition: success)`.

Available actions: `delete`, `set-session`, `reschedule` (valid per `can_reschedule`), `cancel`.

NOT available: `retry` (execution is `pending`, not `failed`), `submit` (readiness is `blocked`).

If user tries `retry` on a blocked job, print:

```
error: job <id> cannot be retried
  current execution state: pending (retry requires: failed)
```

### Orphaned at jobs

Condition: `submission=scheduled` but `at_job_id` is no longer in `atq`.

Detection: not done at `list` time (too slow; requires `atq` call per job). Detected opportunistically when `atrm` fails during a mutation.

Handling: when `atrm` fails, print warning and continue. Do not abort the mutation.

Future: a `schedule-agent check` command could call `atq` and reconcile — out of scope for this plan.

### Missing prompt files

In `list`: append `[prompt missing]` marker.
In `show`: `prompt_exists: no (file not found)`.
On `submit`: fail with:

```
error: prompt file not found: <path>
  job <id> cannot be submitted
```

Implementation: check `Path(job["prompt_file"]).exists()` before calling `schedule()`.

### Inconsistent state (invariant violations)

If `check_invariants` raises during a load or mutation, the CLI should catch it and print:

```
error: job <id> has inconsistent state
  <invariant violation message>
  use `schedule-agent delete <id>` to remove it
```

In `list`, mark the job with `[!]` rather than crashing.

Implementation: wrap `migrate_job` in a try/except in `persistence.load_jobs()` and return a sentinel `{"id": id, "_invalid": True, "_error": str(e)}`. `format_job_label` checks for `_invalid`.

### Running jobs that receive a mutation

`reschedule` and `set-session` are permitted on running jobs (`can_reschedule` and `can_change_session` return True). The `apply_job_update` function does not cancel any `at` job for running jobs (correct, since `submission=running` != `scheduled`). Only `when` or session is updated; the running execution is not interrupted.

Print a note when this happens:

```
  note: job is currently running; change takes effect after current execution
```

---

## 6. Alignment with Transition Engine

### Current issues

1. **`apply_job_update` resubmit-failure path** (cli.py:596–604) manually sets `submission="queued"` and `at_job_id=None` without going through a transition. This is the only direct mutation in the CLI layer.

   Fix: extract a transition `on_resubmit_failed(job)` in `transitions.py` that resets `submission=queued` and `at_job_id=None` with invariant check.

2. **`cancel_at_job`** (cli.py:389–426) manually sets `at_job_id=None` in the job record. This is not a transition violation (no state dimension changes), but it bypasses the job update pattern.

   Fix: after `atrm`, call `on_cancel(job)` if the intent is cancellation, or simply clear `at_job_id` via an explicit partial update helper (for the reschedule/session case where we're clearing before resubmitting).

3. **`set_state`** (cli.py:150–186) is a legacy bridge that directly writes `submission`/`execution`. It is called only during the migration shim paths. This should be preserved as-is for now and removed when the legacy state file is retired.

### Rule going forward

The CLI layer is allowed to:
- Call `can_*` guards from `state_model.py`
- Call `on_*` transitions from `transitions.py`
- Call `schedule()` / `cancel_at_job()` for scheduler side effects
- Call `save_jobs()` with the result of a transition

The CLI layer must not:
- Set `submission`, `execution`, `readiness`, `at_job_id`, `session_mode`, `session_id` directly on a job dict except through a transition function
- Bypass `check_invariants` by constructing partial job dicts

---

## 7. Implementation Order

Tasks are ordered by risk (lowest first) and dependency.

### Task 1: Update `show_job_text` in cli.py

**Files:** Modify `schedule_agent/cli.py`

Show all raw dimensions + `at_job_id` + `depends_on` + timestamps + `prompt_exists`.

No behavior change. Safe to do first.

### Task 2: Update `format_job_label` in cli.py

**Files:** Modify `schedule_agent/cli.py`

Implement columnar format: id / display_state / agent / session / [depends: dep_id] / [prompt missing] / [!].

No behavior change. Must update `test_cli.py` and `test_cli_external_interaction.py` to match new format.

### Task 3: Add transition messaging to mutating commands

**Files:** Modify `schedule_agent/cli.py`

Add before/after print statements to: `cli_reschedule_job`, `cli_change_session`, `cli_retry_job`, `remove_job`, and the new `cancel`/`submit` commands.

Messaging should be computed from the job state *before* calling the transition, and the result state *after*.

### Task 4: Add `cancel` command

**Files:** Modify `schedule_agent/cli.py`, `schedule_agent/transitions.py` (verify `on_cancel` covers all cases)

Wire `cancel <id>` to `on_cancel()`. If `submission=scheduled`, call `cancel_at_job()` first. Print messaging per Section 3.

Guard: if `submission=running`, print error (can't cancel running job — would need SIGTERM, out of scope).

### Task 5: Add `submit` command

**Files:** Modify `schedule_agent/cli.py`

Wire `submit <id>` to `can_submit()` guard → prompt file existence check → `schedule()`. Print messaging per Section 3.

### Task 6: Rename `session` → `set-session`

**Files:** Modify `schedule_agent/cli.py` (argparse definition + dispatch), `tests/test_cli.py`, `tests/test_cli_external_interaction.py`

Add `set-session` subparser. Keep `session` as an undocumented alias for one release (add `help=argparse.SUPPRESS`).

### Task 7: Fix transition alignment in `apply_job_update`

**Files:** Modify `schedule_agent/transitions.py`, `schedule_agent/cli.py`

Add `on_resubmit_failed(job: dict) -> dict` to `transitions.py`. Use it in `apply_job_update`'s failure path.

### Task 8: Edge case — inconsistent state handling in `load_jobs`

**Files:** Modify `schedule_agent/persistence.py`, `schedule_agent/cli.py`

Wrap `migrate_job` call in try/except in `load_jobs`. Return sentinel dict for invalid jobs. Handle sentinel in `format_job_label`.
