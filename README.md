# schedule-agent

Queue Codex and Claude CLI work so it can run **after your session expires**.

---

## Why this exists

You’re working with Codex or Claude.

```bash
schedule-agent
```

This tool turns them into a batch system for AI workflows.

## Installation

```bash
git clone <repo>
cd schedule-agent
./install.sh
```

Or from source as a package:

```bash
pip install .
```

Then ensure the install target is in your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Run:

```bash
schedule-agent
```

## Requirements

Check the required tools:

```bash
which at
which atrm
which codex   # optional
which claude  # optional
```

Ensure the scheduler daemon is running:

```bash
systemctl status atd
```

## Agent permissions

The scheduler runs agents in non-interactive, fully automated mode.

Codex uses:

```bash
codex exec --dangerously-bypass-approvals-and-sandbox
```

Claude uses:

```bash
claude -p --dangerously-skip-permissions
```

Implications:

```bash
git status
```

- no permission prompts
- no interactive confirmations
- full file and shell access

Always test prompts manually before scheduling.

## Configurable editor

Editor resolution order:

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

## Core commands

| Command | Description |
|---|---|
| `schedule-agent` | Open the interactive UI |
| `schedule-agent --dry-run` | Show what would be scheduled during interactive create |
| `schedule-agent list` | List jobs |
| `schedule-agent show <id>` | Show one job |
| `schedule-agent delete <id>` | Delete one job |
| `schedule-agent reschedule <id> "<when>"` | Change time for one job |
| `schedule-agent session <id> <session>` | Change session for one job |
| `schedule-agent session <id> --new` | Clear the session and use a new one |

## Common interactions

Create a job:

```bash
schedule-agent
```

List jobs:

```bash
schedule-agent list
```

Show one job:

```bash
schedule-agent show claude-20260414-101200
```

Delete one job:

```bash
schedule-agent delete claude-20260414-101200
```

Reschedule one job:

```bash
schedule-agent reschedule claude-20260414-101200 "03:00 tomorrow"
```

Change a job to a specific session:

```bash
schedule-agent session claude-20260414-101200 abc123session
```

Change a job back to a new session:

```bash
schedule-agent session claude-20260414-101200 --new
```

Preview the generated `at` script during create:

```bash
schedule-agent --dry-run
```

## Mutation model

The scheduler preserves intent.

Queued job:

```bash
schedule-agent reschedule <id> "now + 90 minutes"
```

- updated in place
- remains queued

Submitted job:

```bash
schedule-agent reschedule <id> "03:00 tomorrow"
```

Under the hood:

```bash
atrm <job_id>
at <new time>
```

- old `at` job removed
- job updated
- job re-submitted automatically

Delete a submitted job:

```bash
schedule-agent delete <id>
```

- `atrm` runs first
- then the job is removed from queue and state

Submitted jobs are marked with **`(S)`** in interactive and non-interactive job listings.

## Storage

Inspect state:

```bash
ls ~/.local/state/schedule-agent
```

Inspect prompt files:

```bash
ls ~/.local/share/schedule-agent/agent_prompts
```

The tool respects `XDG_STATE_HOME` and `XDG_DATA_HOME` if they are set.

## Session discovery

Inspect local session roots:

```bash
ls ~/.codex/sessions
ls ~/.claude/projects
```

The scheduler shows the last 10 discovered sessions when you create a job or change a session interactively.

## Asciinema demo

Record:

```bash
./demo/record-demo.sh
```

Play:

```bash
asciinema play demo/demo.cast
```

Upload:

```bash
asciinema upload demo/demo.cast
```

## Design intent

This tool is intentionally strict.

```bash
schedule-agent
```

It prioritizes:

- predictability over convenience
- explicit control over hidden behavior
- filesystem state over in-memory abstraction

It is designed for unattended execution.

## License

MIT