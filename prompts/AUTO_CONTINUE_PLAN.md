# Auto-Continuation on Allowance Exhaustion

**Executive Summary**
- Replace the current trap-based merged-log wrapper with a Python-run execution path that captures full `stdout`, full `stderr`, a synthesized combined log, final tail lines, and a structured terminal outcome for every job run.
- Add a hybrid allowance-exhaustion detector that is language-tolerant, prioritizes time-bearing retry/reset lines, assigns confidence, and only auto-schedules when the reset time and resumable session are both confirmed strongly enough.
- Model continuations as auto-generated child jobs, not retries: same agent, same `cwd`, same session, prompt exactly `Please continue.`, clear parent/root linkage, and per-job UI controls for `enabled`, `delay after reset` (`5` or `10` minutes, default `5`), and `max auto continuations` (`1-5`, default `3`).

**Behavior Specification**
- Output capture and allowance detection run for every job; scheduling a continuation additionally requires `auto_continue_on_limit=true`.
- A continuation is created only when detection confidence is `medium` or `high`, a future-or-resolved reset time is extracted, a resumable session id is confirmed, and the chain has not exceeded that job’s cap.
- The continuation is a new child job with `job_kind=auto_continuation`, `continuation_root_id=<root manual job>`, `continuation_parent_id=<triggering job>`, `continuation_index=parent+1`, `session_mode=resume`, and the same agent and `cwd`.
- The continuation prompt is exactly `Please continue.`; no extra transcript or explanation is appended because session context and linkage are stored in state.
- Continuation time is `ceil_to_minute(max(parsed_next_allowed_at, finished_at) + offset_minutes)` in scheduler local time; this keeps the configured buffer even when the reported reset time is already in the past by the time parsing finishes.
- Ordinary failures never auto-schedule unless the detector independently finds a qualifying allowance-exhaustion signal; exit code alone never triggers continuation.
- If a continuation child record is created but `at` submission fails, keep the child in normal queued state, mark it auto-generated, and record the submission failure in audit metadata instead of dropping it.
- Dependents of the root job must not be released or blocked when an intermediate chain job schedules another continuation; dependency resolution happens only when the chain ends without another continuation, using the final chain outcome.

**Detection and Parsing Strategy**
- Add a dedicated detector module that consumes captured `stdout`, `stderr`, combined tail, final exit metadata, and finish time; strip ANSI, normalize Unicode with NFKC, lowercase, and collapse whitespace before analysis.
- Use a hybrid detector: concept-based multilingual anchors for `limit/quota/messages`, `retry/wait/again`, and `reset/available`, plus time extractors; do not match a single hardcoded English sentence.
- Scan the whole captured output but prioritize the last `stderr` lines, then the last combined lines, then broader nearby context; this biases toward the provider’s terminal rate-limit message without depending on a fixed position.
- Time extraction order: explicit absolute timestamps with zone/offset, localized absolute date-times via `dateparser`, relative durations via custom regex plus `dateparser`, then time-only strings interpreted as the next valid future local time.
- Timezone rules: trust explicit offsets or zones when present; if the text says “local time” or equivalent, use scheduler local time; if no timezone is present, default to scheduler local timezone because `at`, UI display, and current scheduler behavior are local-time based.
- If multiple candidate times are found, choose the highest-confidence candidate nearest the end of `stderr`; if equally strong candidates conflict, record `ambiguous_time` and do not auto-schedule.
- Confidence levels: `high` = strong allowance anchor plus parseable time on the same line or immediate neighbor; `medium` = strong allowance anchor plus parseable time in nearby context; `low` = allowance hint without reliable time; `none` = no qualifying signal.
- Add `dateparser` as the only new runtime dependency; it is justified because localized absolute and relative time parsing is core to the feature and not realistic to hand-roll robustly.

**State / Data Model Changes**
- Add per-job controls: `auto_continue_on_limit` `bool` default `false`, `auto_continue_offset_minutes` `int` allowed `{5,10}` default `5`, and `auto_continue_max_chain` `int` allowed `1..5` default `3`.
- Add lineage fields: `job_kind` (`manual|auto_continuation`), `continuation_root_id`, `continuation_parent_id`, and `continuation_index`; set legacy and new manual jobs to `manual`, root=self, parent=`null`, index=`0`.
- Add execution artifact fields: `last_stdout_file`, `last_stderr_file`, `last_combined_file`, `last_output_tail` (store last 20 combined lines), and keep `last_log_file` as a compatibility alias to the combined file.
- Add `last_terminal_outcome` with `started_at`, `finished_at`, `exit_code`, `status` (`exited|signaled`), `signal`, byte counts, and `run_dir`.
- Add `resolved_session_id` to record the actual resumable session id obtained from the last run, even if the job started in `session_mode=new`.
- Add `last_limit_detection` with `detected`, `confidence`, `matched_stream`, `matched_excerpt`, `next_allowed_at`, `timezone_assumption`, `decision` (`scheduled|skipped|not_detected`), `skip_reason`, `scheduled_continuation_at`, `continuation_job_id`, and any `continuation_submit_error`.
- Do not persist a separate continuation count; derive it by scanning jobs with the same `continuation_root_id` under the queue lock. Add invariants that auto-generated jobs must be `resume` jobs with a non-null session id, parent id, root id, and index `>=1`.

**UI Integration Plan**
- In the create flow, add an `Auto-continue on allowance limit?` yes/no step defaulting to `No`; when `Yes`, show two more controls: `Delay after reset` (`5 minutes` or `10 minutes`) and `Max auto continuations` (`1-5`, default `3`).
- In the edit flow, add a dedicated continuation-settings action in the TUI, bound to a new key such as `a`, and mirror it with a noninteractive command such as `schedule-agent set-auto-continue <job_id> --on|--off [--delay 5|10] [--max-chain N]`.
- Disable continuation-settings edits while a job is running, matching the existing mutation rules for prompt/session/reschedule changes.
- In list views, add a compact `Continue` column with values like `Off`, `On`, or `Auto#2`; display auto-generated jobs distinctly, for example by rendering their title with an `[AUTO]` prefix in the view layer.
- In detail views, show the current setting, offset, chain cap, job kind, root/parent/index, resolved session id, whether a continuation was created, when it will run, and which session it will continue in.
- In detail views for completed runs, also show the last 5 combined tail lines and the paths to full `stdout`, `stderr`, and combined logs so users can inspect both the reason and the raw artifacts.

**Scheduler + Session Behavior**
- Replace direct agent execution in the `at` script with a minimal wrapper that `cd`s, sets `PATH`, and calls a new internal command such as `schedule-agent run-job <job_id>`.
- The runner creates a per-run directory under the job log dir, writes `stdout.log`, `stderr.log`, `combined.log`, and `run.json`, and drains both subprocess pipes fully before finalizing state.
- Refactor the execution layer to expose provider-aware argv building instead of relying on shell-quoted `$(cat ...)`; keep a string-preview helper only for dry-run output.
- For Claude new-session jobs with auto-continuation enabled, pre-generate a UUID and pass `--session-id` so the resulting session is deterministic and resumable.
- For Codex new-session jobs, resolve the session id by comparing session files created or modified during the run; only schedule a continuation if that resolution yields exactly one confirmed resumable session id.
- Keep resume jobs simple: if the job already has `session_id`, the continuation reuses it directly and no session discovery is needed.
- Replace the current `mark done/failed` end-state path with one atomic finish operation that records run artifacts, stores detection results, resolves the effective session id, optionally creates/submits a continuation child, and only then updates dependency state.
- Future descendants inherit the parent job’s current continuation settings; editing a queued child affects later chain steps, but never retroactively rewrites already-created continuation jobs.

**Logging and Auditability Requirements**
- Every run must produce plain-text logs `stdout.log`, `stderr.log`, and `combined.log`; the combined log is synthesized in capture order and is convenient for humans, while per-stream logs remain authoritative.
- Every run must also produce a structured `run.json` containing terminal outcome, session-resolution result, detection inputs and outputs, continuation decision, and file paths.
- Append explicit scheduler markers to `combined.log` for `start`, `finish`, `limit_detected`, `session_resolved`, `continuation_scheduled`, `continuation_skipped`, and `continuation_submit_failed`.
- Parent job state must retain the matched excerpt and scheduled continuation metadata so `show` can answer “why was this continuation created or skipped?” without opening raw files first.
- Auto-generated child jobs must be cross-linked in UI and state to the triggering parent and root job so the entire chain is auditable from either side.

**Phased Implementation Plan**
- [ ] Phase 1: replace the shell-redirection execution path with a Python runner that captures full streams, combined tail, and terminal outcome, while preserving current scheduling behavior for jobs with auto-continuation disabled.
- [ ] Phase 2: add job model fields, migrations, invariants, and view-layer derived fields for continuation controls, lineage, run artifacts, resolved session id, and last detection outcome.
- [ ] Phase 3: implement the hybrid detector and time parser, add `dateparser`, seed multilingual concept dictionaries, and define conflict, ambiguity, and confidence rules.
- [ ] Phase 4: implement atomic finish-time continuation creation, child submission, same-session reuse, chain-cap enforcement, non-advancing-reset protection, and dependency gating until chain completion.
- [ ] Phase 5: extend the TUI and CLI with create/edit controls, list/detail rendering, auto-generated job markers, and explicit display of continuation decisions and session reuse.
- [ ] Phase 6: add fixture-heavy tests, update README/help text, and document the new internal runner plus the user-facing continuation controls and audit outputs.

**Test Plan**
- Verify full capture of `stdout`, `stderr`, combined log, combined tail, and terminal outcome for success, failure, signaled exit, large output, and interleaved streams.
- Verify English fixtures for message-limit, usage-limit, quota, retry-at, reset-at, and mixed wording variants.
- Verify non-English fixtures at minimum for Spanish, French, German, Portuguese, and one non-Latin-script example; include variants where the limit wording and the retry time appear on separate lines.
- Verify absolute-time parsing with ISO timestamps, offset-bearing strings, zone-bearing strings, and localized absolute dates; verify relative parsing like “in 2 hours”, “resets in 35 minutes”, and localized equivalents.
- Verify timezone handling for explicit zones, zone-less local times, “local time” phrasing, past reset times, and ambiguous/conflicting time candidates.
- Verify no continuation is created for ordinary failures, generic “try again” text without allowance anchors, low-confidence detections, missing times, ambiguous times, or unresolved sessions.
- Verify continuation creation preserves same agent, same `cwd`, same resumable session, exact prompt `Please continue.`, correct root/parent/index linkage, inherited controls, and visible auto-generated status.
- Verify Claude new-session continuation via deterministic `--session-id`; verify Codex new-session continuation only when a unique session id is resolved and is skipped with audit metadata otherwise.
- Verify loop prevention for caps `1`, `3`, and `5`, repeated exhaustion across a chain, and repeated identical or non-advancing reset times.
- Verify dependency behavior: dependents of the root job remain waiting while the continuation chain is still active, and only transition when the final chain job finishes without scheduling another continuation.
- Verify UI behavior for create, edit, list, and show flows, including conditional control visibility, default values, disabled edits while running, and user-visible continuation reason/run/session info.
- Verify migrations for existing jobs default the feature off and the lineage to manual/self-rooted, and verify continuation-child submission failures leave a queued child plus inspectable error metadata.

**Risks / Edge Cases / Assumptions**
- Codex currently has no documented first-class new-session id flag in the locally available CLI help, so new-session Codex continuation must remain confirmed-session-only; if session discovery is not unique, skip scheduling rather than violate same-session semantics.
- The detector should prefer false negatives over false positives; an unparseable or ambiguous rate-limit message is safer to show as “detected but not scheduled” than to create the wrong child at the wrong time.
- The feature relies on local scheduler time because `at` and the current UI are local-time based; explicit provider timezones are honored, but zone-less strings are assumed local by design.
- Combined-log ordering is best-effort because `stdout` and `stderr` are captured concurrently; per-stream files are the source of truth when ordering matters.
- Per-job configurability is limited intentionally: delay is only `5` or `10` minutes, and chain cap is `1-5`; broader free-form knobs are out of scope for v1.
- Dependents are treated as depending on the logical completion of the continuation root chain, not the first interrupted attempt; this is a necessary semantic change to avoid downstream work running against partial progress.
- Editing continuation settings on a job affects that job and any descendants it may generate later; it does not retroactively mutate already-created continuation children.
- Legacy jobs and jobs with auto-continuation disabled still benefit from the new capture and detection pipeline, so observability improves even when scheduling behavior does not change.
