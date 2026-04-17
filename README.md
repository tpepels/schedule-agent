# schedule-agent

Queue Codex and Claude CLI work so it runs **after your session expires**.

Persistent job scheduling for short-lived agent sessions.

---

## What

```bash
schedule-agent
````

`schedule-agent` is a CLI tool for scheduling Codex and Claude prompts to run later.

It lets you:

* queue agent work for a later time
* persist jobs on disk
* inspect scheduled jobs
* safely modify jobs that were already submitted

Jobs are executed through `at`, not interactively.

---

## Why

You run something like:

```bash
claude -p "do X"
```

or:

```bash
codex exec "do X"
```

Then one of the usual things happens:

* your session expires
* your usage resets later
* a long task gets interrupted
* you need the work to continue while you are away

```bash
schedule-agent
```

exists for exactly that situation.

It lets you queue work to run **after limits reset**, so short-lived agent sessions do not force short-lived workflows.

---

## Install


Clone the repository and run the install script:

```bash
git clone <repo>
cd schedule-agent
./install.sh
```

The install script now handles both installation and updates:

- If schedule-agent is not installed, it will perform a fresh install.
- If schedule-agent is already installed, it will notify you and update to the latest version (via pip if possible, or by reinstalling the local files).

Make sure the install location is on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Then run:

```bash
schedule-agent
```

---

## Requirements

This tool is intended for:

```bash
uname -s
```

Expected result:

```text
Linux
```

It depends on the system `at` scheduler, so these commands must exist:

```bash
which at
which atrm
```

And the scheduler daemon must be running:

```bash
systemctl status atd
```

You also need at least one supported agent CLI installed:

```bash
which claude
which codex
```

In practice, the minimum requirement set is:

* Linux
* `at`
* `atrm`
* `atd`
* `claude` and/or `codex`
* a text editor such as `nano`, `nvim`, or `code --wait`

---

## Use

### Create a job

```bash
schedule-agent
```

Typical flow:

* choose agent
* write prompt
* choose when to run
* submit

### Inspect jobs

```bash
schedule-agent list
```

### Show job details

```bash
schedule-agent show <id>
```

### Reschedule a job

```bash
schedule-agent reschedule <id> "now + 90 minutes"
```

or:

```bash
schedule-agent reschedule <id> "03:00 tomorrow"
```

### Change session

```bash
schedule-agent session <id> <session_id>
```

or reset to a new session:

```bash
schedule-agent session <id> --new
```

### Delete a job

```bash
schedule-agent delete <id>
```

### Preview before scheduling

```bash
schedule-agent --dry-run
```

---

## Development

Install the local quality tooling:

```bash
python3 -m pip install -e ".[dev]"
```

Run the same mandatory checks that CI runs:

```bash
make check
```

The enforced gate is:

* `ruff check .`
* `ruff format --check .`
* `pytest -q`

If you want GitHub to block merges on failures, mark the `quality` workflow as a required status check in branch protection.

---

## Remarks

### Agent permissions

Jobs run unattended and without interactive permission prompts.

By default, the tool uses agent commands in the “just do it” style:

```bash
codex exec --dangerously-bypass-approvals-and-sandbox
claude -p --dangerously-skip-permissions
```

That means scheduled agents may:

* modify files
* run shell commands
* change repository state

So this is the rule:

```bash
git status
```

Check your workspace first, and test prompts manually before scheduling them.

---

### Execution model

Jobs are scheduled through `at`:

```bash
at now + 10 minutes
```

That has a few consequences:

* jobs run non-interactively
* jobs run with a minimal environment
* jobs do not inherit your full interactive shell setup

This tool works around that by storing prompts on disk and executing agents with stdin detached, so the scheduled shell wrapper is not accidentally read by the agent.

---

### Mutation model

You can safely modify jobs after creating them:

```bash
schedule-agent reschedule <id> "03:00 tomorrow"
```

If the job was already submitted, the tool will automatically:

```bash
atrm <job_id>
at <new time>
```

In other words:

* old scheduled job is removed
* job definition is updated
* job is re-submitted automatically

The same applies when changing session. You do not need to manually track whether a job is queued or already submitted.

---

### Storage

State is stored in:

```bash
~/.local/state/schedule-agent
```

Prompt files are stored in:

```bash
~/.local/share/schedule-agent/agent_prompts
```

If `XDG_STATE_HOME` or `XDG_DATA_HOME` are set, those locations are used instead.

---

### Editor

Prompt editing uses this resolution order:

1. `SCHEDULE_AGENT_EDITOR`
2. `EDITOR`
3. fallback: `nano`

For example:

```bash
export SCHEDULE_AGENT_EDITOR="nvim"
```

or:

```bash
export EDITOR="code --wait"
```

---

## License

MIT

```

What changed from your version:

- removed the weak ASCII logo
- added a real **Requirements** section
- explicitly says **Linux**
- explicitly explains **`at` / `atrm` / `atd`**
- keeps the flow: what → why → install → requirements → use → remarks
- trims repetition a bit so it reads more like a tool README and less like a product page

The only extra thing I’d add to the repo after this is a tiny `install.sh` note in the README if your installer also bootstraps the Python deps internally.
