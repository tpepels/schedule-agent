## State dimensions

Use these as the source of truth.

```text
submission:
- queued
- scheduled
- running
- cancelled

execution:
- pending
- running
- success
- failed

session_mode:
- new
- resume

readiness:
- ready
- waiting_dependency
- blocked
```

Supporting fields:

```text
session_id                 # required when session_mode=resume
depends_on                 # optional parent job id
dependency_condition       # probably just "success" for now
at_job_id                  # only when submission=scheduled
created_at
updated_at
last_run_at
```

## Combined display state

Do not store this. Derive it.

Suggested output set:

```text
queued
scheduled
running
waiting
blocked
done
failed
cancelled
```

Derivation order:

```python
def derive_display_state(job):
    if job["submission"] == "cancelled":
        return "cancelled"
    if job["submission"] == "running" or job["execution"] == "running":
        return "running"
    if job["execution"] == "failed":
        return "failed"
    if job["execution"] == "success":
        return "done"
    if job["readiness"] == "blocked":
        return "blocked"
    if job["readiness"] == "waiting_dependency":
        return "waiting"
    if job["submission"] == "scheduled":
        return "scheduled"
    return "queued"
```

## Invariants

These should always hold.

```text
1. submission=scheduled  => at_job_id is present
2. submission!=scheduled => at_job_id is absent
3. session_mode=resume   => session_id is present
4. session_mode=new      => session_id is absent
5. execution=running     => submission=running
6. readiness=waiting_dependency => depends_on is present
7. readiness=blocked     => depends_on is present
8. execution=success|failed => last_run_at is present
```

## Initial states

### New standalone job

```json
{
  "submission": "queued",
  "execution": "pending",
  "session_mode": "new",
  "readiness": "ready"
}
```

### New dependent job

```json
{
  "submission": "queued",
  "execution": "pending",
  "session_mode": "new",
  "readiness": "waiting_dependency",
  "depends_on": "job-a",
  "dependency_condition": "success"
}
```

## Transition table

### 1. Create job

Standalone:

```text
submission: queued
execution: pending
readiness: ready
```

Dependent:

```text
submission: queued
execution: pending
readiness: waiting_dependency
```

### 2. Submit job

Allowed only when:

```text
submission=queued
execution=pending
readiness=ready
```

Transition:

```text
submission: queued    -> scheduled
execution: pending    -> pending
readiness: ready      -> ready
at_job_id: absent     -> present
```

### 3. Start running

Triggered by the scheduled wrapper when execution begins.

```text
submission: scheduled -> running
execution: pending    -> running
```

### 4. Finish successfully

```text
submission: running   -> queued
execution: running    -> success
last_run_at: set
at_job_id: cleared
```

I recommend `submission -> queued` after completion rather than inventing `completed`. Submission is about scheduler membership, and after completion the job is no longer in `at`.

### 5. Finish with failure

```text
submission: running   -> queued
execution: running    -> failed
last_run_at: set
at_job_id: cleared
```

### 6. Cancel job

If scheduled, first `atrm`.

```text
submission: queued|scheduled -> cancelled
execution: stays as-is unless pending/running policy says otherwise
at_job_id: cleared
```

For a not-yet-run job, I’d keep:

```text
execution: pending
```

For a completed job being cancelled, I’d usually not allow it. Delete is cleaner than cancel there.

### 7. Delete job

This removes the record entirely. Not really a state transition, more a removal event.

If scheduled:

* `atrm`
* clear state
* remove job
* remove prompt file

### 8. Reschedule job

This is state-preserving with respect to “is it live”.

#### If queued

```text
submission: queued    -> queued
execution: pending    -> pending
readiness: unchanged
when: old             -> new
```

#### If scheduled

* `atrm old at_job_id`
* update time
* re-submit

Net result:

```text
submission: scheduled -> scheduled
execution: pending    -> pending
readiness: unchanged
at_job_id: old        -> new
when: old             -> new
```

#### If done or failed

You have two choices.

The cleaner one:

* rescheduling a completed job creates a new run intent

So:

```text
submission: queued
execution: pending
when: new
```

That means reschedule acts like “run again later”.

### 9. Change session

#### If queued

```text
session_mode/session_id: updated
submission: queued
execution: unchanged
```

#### If scheduled

* `atrm`
* update session
* re-submit

Net:

```text
session_mode/session_id: updated
submission: scheduled
execution: pending
at_job_id: replaced
```

### 10. Dependency succeeded

For child jobs waiting on parent success:

```text
readiness: waiting_dependency -> ready
```

No automatic submission unless you explicitly want that policy.

If you do want it, then:

```text
readiness: waiting_dependency -> ready
submission: queued           -> scheduled
```

I would make this configurable.

### 11. Dependency failed

```text
readiness: waiting_dependency -> blocked
submission: queued            -> queued
execution: pending            -> pending
```

### 12. Retry failed job

```text
submission: queued
execution: failed -> pending
readiness: ready
```

Then submit again.

## Practical policy decisions

These are the choices I’d lock down.

### Should success/failed jobs remain visible?

Yes. Keep them until deleted.

### Should completed jobs be reschedulable?

Yes. Reschedule means “make this runnable again”, so reset:

```text
execution -> pending
submission -> queued or scheduled depending on path
```

### Should dependency children auto-submit?

Optional, but very useful. I’d support both:

* manual release
* auto-submit on parent success

### Should blocked jobs be editable?

Yes. You should be able to:

* change dependency
* remove dependency
* delete
* reschedule after unblocking

## Suggested enum-like constants

```python
SUBMISSION = {"queued", "scheduled", "running", "cancelled"}
EXECUTION = {"pending", "running", "success", "failed"}
SESSION_MODE = {"new", "resume"}
READINESS = {"ready", "waiting_dependency", "blocked"}
```

## Recommended helper functions

These will keep the code sane.

```python
def derive_display_state(job) -> str: ...
def can_submit(job) -> bool: ...
def can_reschedule(job) -> bool: ...
def can_change_session(job) -> bool: ...
def can_delete(job) -> bool: ...
def on_submit(job, at_job_id) -> dict: ...
def on_start(job) -> dict: ...
def on_success(job) -> dict: ...
def on_failure(job) -> dict: ...
def on_dependency_success(job) -> dict: ...
def on_dependency_failure(job) -> dict: ...
```

## Minimal migration from current model

Current rough mapping:

```text
status=queued     -> submission=queued, execution=pending
status=submitted  -> submission=scheduled, execution=pending
status=running    -> submission=running, execution=running
status=success    -> submission=queued, execution=success
status=failed     -> submission=queued, execution=failed
session missing   -> session_mode=new
session present   -> session_mode=resume
```

And new jobs without dependency default to:

```text
readiness=ready
```

Dependent jobs:

```text
readiness=waiting_dependency
depends_on=<job_id>
```

## The one-line model

If you want the shortest useful summary:

* **submission** = where the job is in the scheduler
* **execution** = what happened when it ran
* **session_mode** = whether the agent starts fresh or resumes
* **readiness** = whether the job is allowed to be scheduled yet