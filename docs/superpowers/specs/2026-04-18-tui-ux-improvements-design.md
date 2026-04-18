# TUI UX Improvements — Design

Date: 2026-04-18
Scope: `schedule_agent/cli.py` jobs screen only. No new TUI framework, no new overlay
kinds beyond the existing `confirm` / `input` / `picker` / `message`. Existing layout
(header / body / footer + detail pane) remains unchanged.

## Motivation

Five quality-of-life fixes to the full-screen jobs UI:

1. Free-text schedule input is error-prone — users can type specs that either
   fail at submit time or schedule for a time they didn't mean.
2. New-job flow currently auto-submits to `atq`; users want an explicit choice
   so forgetting to submit isn't possible either.
3. Job list grows stale between refreshes.
4. Footer key hints are a plain sentence; the mnemonic first letters don't stand out.
5. No in-app reference for status meanings or action keys.

## 1. Constrained schedule picker

Replace every freeform schedule input (new-job, `t` reschedule, `r` retry) with a
three-stage `picker`-overlay flow:

**Stage A — mode**: two items, `Offset from now` and `Specific time`.

**Offset branch — Stage B(offset)**: picker with presets
`5 min, 10 min, 15 min, 30 min, 1 hour, 2 hours, 4 hours, 8 hours, 24 hours`.
Each item's value is the schedule spec string `now + N minutes|hours`.

**Time branch — Stage B(time)** → **Stage C(time)**:

- Hour picker: items `00`–`23`.
- Minute picker: items `00, 05, 10, …, 55` (12 entries).
- Resolution rule: interpreted as the **next future occurrence** of HH:MM in local
  time — if HH:MM today is still ahead, use today; otherwise tomorrow. Implemented
  using `datetime.now().astimezone()` + `timedelta(days=1)` if needed, then
  formatted as an ISO string and passed through the backend as-is.

All three stages use the existing `open_picker` and closure-based form passing
already used by the new-job flow — no new overlay kind.

Result handoff: each branch produces a schedule spec string accepted by
`resolve_schedule_input`. No typed input is possible, so `at`-parse errors from
user typos can't occur.

## 2. Submit-to-atq confirm (default Yes)

After schedule selection and prompt capture, the new-job flow shows a `confirm`
overlay: `"Submit to at queue now? [Y/n]"` with `default=True`.

Two small changes to the existing confirm machinery:

- `overlay_confirm_key` already takes y/n/Esc; add handling for **Enter** to
  accept `overlay.default`.
- Footer render: when `overlay.default` is true, print `[Y/n]`; otherwise
  `[y/N]` (current behaviour). One-line change in `footer_fragments`.

Dispatch:

- Yes → `create_job(..., submit=True)` (today's behaviour).
- No  → `create_job(..., submit=False)` — job persists but isn't queued. The
  user can press `s` later to submit.

## 3. 30-second auto-refresh

Register a single asyncio task via `app.create_background_task`:

```
async def _auto_refresh():
    while True:
        await asyncio.sleep(30)
        if state.overlay.kind is None and not state.quit:
            state.refresh_jobs()
            app.invalidate()
```

- Skips while an overlay is open (so the user's in-progress picker/confirm/input
  never blinks or loses state).
- Silent — no `state.message` mutation (the `g` manual refresh keeps its
  "Refreshed." message).
- Cancelled when the app exits: `app.create_background_task` ties the task to
  the app's lifecycle, no manual cleanup needed.

## 4. Highlighted action keys in footer

`_help_hints()` currently returns a plain string; change the call site in
`footer_fragments` to return a **list of fragments** instead, with a new
`class:key` style applied to the first letter of each action:

```
(class:footer, " ")(class:key, "N")(class:footer, "ew  ")(class:key, "E")(class:footer, "dit  ")…
```

Keys covered: `N` new, `E` edit, `T` reschedule, `C` session, `U` unschedule,
`S` submit, `R` retry, `D` delete, `F` filter, `G` refresh, `V` detail,
`?` help, `Q` quit.

New style entry: `"key": "reverse bold"` (on top of the footer's reverse
background so it stands out against the bar). Same Window, only the fragment
list changes.

## 5. Help shown in the detail pane

**No new overlay kind.** Add `state.show_help: bool` to `JobsScreenState`. When
true, the `detail_fragments()` renderer returns the help text instead of the
job detail. The existing `detail_window` and its two layout containers
(VSplit pane in medium/wide, HSplit overlay in narrow) are reused as-is.

Key bindings:

- `?` (no overlay): toggle `state.show_help`.
- Any **other** top-level key while `show_help` is true: set
  `state.show_help = False` **before** running the key's action, so `?` acts
  as a dismiss-anything shortcut.

Help content (static string, rendered via existing `FormattedTextControl`):

```
Statuses
  Queued     Created, not yet submitted to at(1)
  Scheduled  Submitted to at(1), waiting to run
  Running    Currently executing
  Waiting    Waiting on a dependency job
  Blocked    A blocking condition prevents run
  Completed  Finished with exit code 0
  Failed     Finished with non-zero exit code
  Removed    Cancelled / removed from queue
  Invalid    On-disk metadata is broken

Actions
  N new         Create a job (pick agent → session → schedule → prompt)
  E edit        Edit the selected job's prompt in $EDITOR
  T reschedule  Change when the selected job runs
  C session     Change the selected job's session id
  U unschedule  Remove from at(1) but keep metadata
  S submit      Submit or repair the selected job
  R retry       Reschedule a completed/failed job
  D delete      Permanently delete the selected job
  F filter      Cycle: all / active / completed
  G refresh     Reload from disk now (auto every 30s)
  V detail      Toggle detail pane (narrow mode)
  ? help        Toggle this help screen
  Q quit        Exit the jobs screen
```

## File-level scope

All changes live in `schedule_agent/cli.py`. No changes to `operations.py`,
`scheduler_backend.py`, `time_utils.py`, or persistence.

- `OverlayState`: no structural change (reusing `default`, `items`, `on_pick`, `on_confirm`).
- `JobsScreenState`: add `show_help: bool = False`.
- `Style.from_dict`: add `"key": "reverse bold"`.
- New helpers:
  - `_schedule_picker_start(form, on_spec)` — Stage A, routes to offset or time branch, then calls `on_spec(spec_string)`.
  - `_resolve_time_pick(hour: int, minute: int) -> str` — returns "YYYY-MM-DD HH:MM" for next occurrence, usable by `resolve_schedule_input`.
  - `_auto_refresh_task` — the asyncio coroutine.
  - `_render_help()` — static help fragments.
- New-job flow: `_nj_input_schedule` replaced by `_nj_pick_schedule` (calls `_schedule_picker_start`).
- `_reschedule` / `_retry` key handlers: use `_schedule_picker_start` instead of `open_input`.
- `footer_fragments`: fragment-list rendering + `[Y/n]` for `default=True` confirms.
- `_help_hints`: returns fragment list, not string.
- New key binding `?` and the dismiss-on-any-key logic.
- Auto-refresh task registration: pass `pre_run=lambda: app.create_background_task(_auto_refresh())` to `app.run()`. This keeps the synchronous `app.run()` entry point; `pre_run` fires once the event loop is up, which is where `create_background_task` must be called.

## Testing

- Existing 73 pytest tests must still pass unchanged.
- New unit-level targets:
  - `_resolve_time_pick(10, 0)` when now is 17:30 → date is tomorrow.
  - `_resolve_time_pick(23, 55)` when now is 23:50 → date is today.
  - Offset preset labels map to valid `resolve_schedule_input` outputs.
  - Help toggle: pressing `?` flips `state.show_help`; any other key flips it off.
  - Auto-refresh skips when `state.overlay.kind != None`.

Headless TUI drive (following existing pattern):

- New-job happy path: mode=Offset → 15 min → prompt → confirm Yes → `cached_jobs` grows by 1, submission=scheduled.
- New-job "don't submit" path: mode=Time → 10 → 00 → prompt → confirm No → job exists, submission=queued, `at_job_id` empty.

Manual verification (the headless simulator can't cover):

- Visual: key highlight in footer reads cleanly, help pane renders in both narrow and wide modes, picker stages feel responsive.
- Real `$EDITOR` suspend/resume for new-job prompt capture still works.

## Out of scope

- Changing the help/hint content beyond what's listed above.
- Any non-TUI surface (`list_jobs_noninteractive`, argparse commands).
- Any change to the scheduler backend or persistence.
- Custom minute granularity (locked at 5-min).
- Date-picker (not just HH:MM) — only "next occurrence" semantics.
