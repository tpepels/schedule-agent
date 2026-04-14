# schedule-agent

A terminal UI for scheduling Codex and Claude CLI jobs using `at`, with safe mutation of already scheduled work.

## Why this exists

Agent CLIs are powerful but time-bound and rate-limited. `schedule-agent` lets you queue work for off-hours, keep it inspectable on disk, and safely edit jobs that are already scheduled.

## Features

- schedule jobs for **Codex** or **Claude**
- start a **new session** or attach to one of the last 10 discovered local sessions
- schedule work:
  - by offset from now
  - today at a specific time
  - tomorrow at a specific time
- interactive jobs list with keyboard actions
- safe mutation of already submitted jobs:
  - reschedule
  - change session
  - delete
- real `at` cancellation via `atrm`
- prompt files stored on disk instead of inlining huge shell-quoted blobs

Submitted jobs are marked with **`(S)`** in the jobs list.

## Requirements

You need:

- Python 3.10+
- `at`
- `atrm`
- a text editor available in your environment
- one or both of:
  - `codex`
  - `claude`

Also ensure the `atd` service is running.

## Installation

```bash
git clone <repo>
cd schedule-agent
./install.sh
```

After installation:

```bash
schedule-agent
```

Uninstall the scheduler using:

```bash
./uninstall.sh
```

## Configurable editor

Prompt editing is not hardcoded to `nano` anymore.

The tool resolves the editor in this order:

1. `SCHEDULE_AGENT_EDITOR`
2. `EDITOR`
3. fallback: `nano`

Examples:

```bash
export SCHEDULE_AGENT_EDITOR="nvim"
```

```bash
export EDITOR="code --wait"
```

The editor command is parsed with shell-style splitting, so values like `code --wait` work correctly.

## Safety and execution model

Jobs are scheduled through the system `at` daemon.

That means jobs run:

- non-interactively
- with a minimal environment
- without your interactive shell startup files

### Prompt handling

Prompts are written to dedicated files under the tool’s data directory.

Scheduled agent commands read prompt text from disk and run with:

```bash
</dev/null
```

This is intentional. It prevents the agent from accidentally reading the `at` wrapper script from stdin.

## Storage locations

The tool is installable and uses XDG-style paths rather than hardcoded script-relative files.

### State

By default:

```text
~/.local/state/schedule-agent/
```

Contains:

- queue file
- job state

### Data

By default:

```text
~/.local/share/schedule-agent/
```

Contains:

- generated prompt files

If `XDG_STATE_HOME` or `XDG_DATA_HOME` are set, those are respected.

## Session discovery

Session selection is explicit. The tool does not guess “most recent” automatically.

It discovers the last 10 local sessions from:

- Codex:
  - `~/.codex/sessions`
- Claude:
  - `~/.claude/projects`

## Controls

Top-level menu:

- **Create job**
- **Jobs**

Jobs view keys:

- **Enter** → view job
- **R** → reschedule
- **D** → delete
- **C** → change session
- **Q / Esc** → quit
- **Up / Down** or **K / J** → move

## Mutation rules

The scheduler preserves intent.

### If a job is queued

Mutating it updates the job and leaves it queued.

### If a job is submitted

Mutating it will:

1. cancel the old `at` job with `atrm`
2. update the job
3. re-submit it automatically

Deleting a submitted job also removes its queued `at` job first.

This behavior is centralized, not implemented case by case.

## CLI assumptions

The default command templates are:

### Codex

```bash
codex exec ...
codex exec resume <session> ...
```

### Claude

```bash
claude -p ...
claude --resume <session> ...
```

If your local CLI differs, adjust the `AGENTS` mapping in `schedule_agent/cli.py`.

## Development

Run directly:

```bash
python -m schedule_agent.cli
```

## License

MIT