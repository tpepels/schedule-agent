# schedule-agent

**Queue Codex and Claude CLI work so it runs after your session expires.**

Persistent job scheduling for short-lived agent sessions.

---

## The problem

You're deep in a task. You fire off:

```bash
claude -p "finish the migration and open a PR"
```

Then one of these happens:

- your session expires mid-run
- your usage quota resets in four hours
- a long task gets cut off
- you want work to keep going after you close the laptop

`schedule-agent` is for exactly that. Write the prompt now, let it run at 3am, come back to the result. Short-lived sessions stop forcing short-lived workflows.

---

## Quick start

```bash
git clone <repo>
cd schedule-agent
./install.sh

export PATH="$HOME/.local/bin:$PATH"   # if not already set

schedule-agent
```

From there, the interactive TUI walks you through:

1. pick agent (Codex or Claude)
2. pick or reuse a session
3. write your prompt
4. choose when to run (`now + 90 minutes`, `03:00 tomorrow`, etc.)
5. submit

The install script handles both fresh installs and updates.

---

## Requirements

schedule-agent is **Linux / *nix only**. It leans on the system `at` scheduler — there is no Windows or macOS equivalent baked in.

Minimum set:

| Tool | Why |
|------|-----|
| Linux / *nix | relies on `at` + `atd` |
| Python **3.10+** | runtime |
| `at`, `atrm`, `atd` | job scheduler |
| `claude` and/or `codex` | at least one agent CLI |
| `nano` / `nvim` / `code --wait` / any `$EDITOR` | for writing prompts |

Verify before installing:

```bash
which at atrm claude codex
systemctl status atd            # daemon must be running
```

If `at` is missing, install it via your package manager (`pacman -S at`, `apt install at`, `dnf install at`, …) and enable the daemon:

```bash
sudo systemctl enable --now atd
```

---

## How jobs actually run

Understanding this before you schedule something is worth thirty seconds.

### Default agent invocation

schedule-agent invokes agents in **unattended, permissionless mode** — there is no human sitting at the terminal when the job fires, so interactive approval prompts would just hang forever. The defaults are:

```bash
codex exec --dangerously-bypass-approvals-and-sandbox "<your prompt>"
claude -p --dangerously-skip-permissions            "<your prompt>"
```

These flags are the **default prefix applied to every prompt you schedule**. Your prompt text is passed as the final argument; everything before it is fixed.

That means a scheduled job may, without asking:

- read, create, modify, or delete files
- run arbitrary shell commands
- make network calls
- change the state of any repository it can reach

**Rule of thumb:** `git status` before you schedule. Test the prompt interactively first. Treat scheduled prompts like cron jobs with a language model attached — because that is what they are.

### Execution model

Jobs are handed off to `at`:

```bash
at now + 10 minutes
```

Consequences:

- jobs run non-interactively, with a minimal environment
- jobs do **not** inherit your full interactive shell (no `direnv`, no shell aliases, no `.zshrc` sugar)
- stdin is detached so the scheduled shell wrapper never gets swallowed as input

Prompts are written to disk first, then read back in when the job fires.

---

## Using it

Once you've created a job via the TUI, all management is via subcommands.

```bash
schedule-agent                               # interactive TUI
schedule-agent list                          # all jobs
schedule-agent show <id>                     # full detail

schedule-agent reschedule <id> "now + 90 minutes"
schedule-agent reschedule <id> "03:00 tomorrow"

schedule-agent session <id> <session_id>     # attach a session
schedule-agent session <id> --new            # reset to a fresh session

schedule-agent delete <id>
schedule-agent --dry-run                     # preview without submitting
```

### Safe mutations

Change your mind after submitting? Fine. If a job is already queued with `at`, schedule-agent automatically:

1. `atrm`s the old submission
2. rewrites the job definition
3. resubmits under the new time / session

No manual bookkeeping.

---

## Configuration

### Editor

Prompt editing resolves in this order:

1. `$SCHEDULE_AGENT_EDITOR`
2. `$EDITOR`
3. `nano`

```bash
export SCHEDULE_AGENT_EDITOR="nvim"
# or
export EDITOR="code --wait"
```

### Storage

| Path | Contents |
|------|----------|
| `~/.local/state/schedule-agent/` | job queue + state |
| `~/.local/share/schedule-agent/agent_prompts/` | prompt files |

`$XDG_STATE_HOME` and `$XDG_DATA_HOME` are honoured if set.

---

## Development

```bash
python3 -m pip install -e ".[dev]"
make check
```

`make check` runs the same gate CI enforces:

- `ruff check .`
- `ruff format --check .`
- `pytest -q`

To block merges on failures, mark the `quality` workflow as a required status check in branch protection.

---

## License

MIT
