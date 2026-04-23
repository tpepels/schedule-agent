# Jobs Screen Refactor Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan phase-by-phase. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

Make the jobs screen the single authoritative interactive surface for job management.

The fullscreen jobs screen must support all common actions directly inside the screen, without launching separate modal mini-apps that call `.run()` against prompt_toolkit dialogs from inside the running TUI.

The jobs screen must become structurally simple, width-aware, and comfortable in a normal terminal.

## Required outcome

After this refactor:

* [ ] The fullscreen jobs screen is the primary interactive UI.
* [ ] All job actions are initiated and completed from inside the jobs screen.
* [ ] The jobs screen no longer mixes fullscreen TUI control flow with prompt_toolkit dialog `.run()` calls from helper functions.
* [ ] The jobs screen no longer uses `asyncio.run()` inside the active TUI event loop.
* [ ] The jobs list is compact and width-aware.
* [ ] The left pane is a summary list, not a pipe-delimited fake table.
* [ ] Secondary metadata moves to the detail pane.
* [ ] The UI works at normal terminal widths without requiring an unusually wide screen.
* [ ] Message handling is consistent and not overwritten by generic success text.
* [ ] Legacy mixed-interaction behavior is removed rather than preserved.

## Product decision

The jobs screen is a real application surface, not a wrapper around a chain of separate prompt dialogs.

That means the following patterns are no longer valid inside the jobs screen:

* calling `input_dialog(...).run()` from a keybinding
* calling `yes_no_dialog(...).run()` from a keybinding
* calling `radiolist_dialog(...).run()` from a keybinding
* returning either a bool or a coroutine depending on loop state
* calling `asyncio.run()` from a TUI action
* using ad hoc terminal handoff logic to patch over the above

These patterns may remain available for non-TUI CLI flows if needed, but **must not be used by the fullscreen jobs screen**.

## Root problems to fix

### 1. Mixed interaction models

The current file combines:

* noninteractive command CLI
* modal prompt/dialog helpers
* fullscreen TUI

The fullscreen TUI currently calls into helpers designed for standalone dialog workflows. This creates conflicting control flow and brittle behavior.

### 2. Broken event-loop model

The current jobs screen defines async action plumbing but then calls it from synchronous key handlers without awaiting or scheduling it consistently. The delete flow then tries to force execution with `asyncio.run()` inside the running application loop.

This must be removed.

### 3. Table rendering is fake and too wide

The left pane currently renders pipe-delimited strings as if they were a table. This causes:

* header/row misalignment
* unstable widths
* poor truncation behavior
* excessive minimum terminal width

The left pane should instead be a compact summary list with fixed-width rendering rules.

### 4. Status/message handling is muddy

Actions set detailed messages, then a generic wrapper may overwrite them with `Updated.` or similar. User feedback must come from one authority.

### 5. Legacy behavior should not be preserved

This is pre-alpha. There is no need to preserve mixed legacy UI patterns. Remove obsolete behavior instead of layering compatibility code over it.

## Design rules

### Jobs screen ownership

The fullscreen jobs screen owns:

* selection
* filtering
* confirmation
* temporary input prompts
* session selection
* status messages
* refresh behavior
* post-action redraw

### Single interaction model inside the TUI

Inside the jobs screen, there must be one consistent model:

* synchronous action dispatch from keybindings
* app-owned overlays / inline prompts / inline confirm UI
* explicit refresh/invalidate after state changes

Do not mix in separate prompt_toolkit mini-apps.

### Compact information hierarchy

The left pane is for quick scanning and navigation only.

The right pane is for detailed metadata and full summary.

Do not try to display every field in the left pane.

### Width-aware UI

The jobs screen must adapt to narrower terminals.

Minimum expectation:

* narrow width: summary pane only, detail view toggleable
* medium width: summary + detail split
* wide width: richer summary columns

### Remove legacy, do not phase-preserve it

If old helper patterns or layout behaviors conflict with the above, remove them.

## Target screen behavior

## Main layout

The jobs screen should have 3 regions:

1. Header bar
2. Main body
3. Footer/status bar

### Header bar

Shows concise global state:

* app name or screen name
* current filter
* local timezone
* optional count summary

Example shape:

`Jobs  Filter: Active  TZ: WEST  8 items`

### Main body

The main body should render one of these modes depending on available width:

#### Mode A: split view

* left: job summary list
* right: selected job detail

#### Mode B: list-only view

* full width summary list
* enter/tab toggles detail overlay or detail mode for selected job

### Footer/status bar

Footer shows:

* key hints
* current contextual status message

Keep hints compact and stable.

## Left pane redesign

Replace the current pipe-delimited fake table with a structured summary row renderer.

### Left pane columns

Default summary fields should be limited to:

* title
* state/status
  n- scheduled time
* agent/session marker

Optional fifth compact field if width allows:

* queue/submission marker

### Fields that must move out of the left pane

Move these to detail only:

* created time
* updated time
* dependency id
* full scheduler label
* full prompt path
* full cwd
* verbose lifecycle fields

### Row behavior

Each row must:

* truncate predictably
* use fixed column widths per active layout mode
* visually distinguish selected row
* visually soften completed/removed/invalid items
* not rely on pipe separators for alignment

### Recommended row rendering approach

Implement a width-aware formatter that returns one formatted row string from a defined column spec.

For example, define summary column policies such as:

* title: flex, min 18, ideal 28, max 40
* status: fixed 10
* run_at: fixed 16
* agent/session: fixed 12

Then derive actual widths from terminal width.

## Detail pane redesign

The detail pane becomes the authoritative full job inspector.

It should show:

* job id
* title
* current display state
* scheduler/submission state
* scheduled time
* created time
* updated time
* agent
* session mode / session id
* cwd
* dependency info
* prompt file path
* queue id / at job id when relevant
* full formatted summary text if useful

The detail pane can be verbose because it only serves the selected item.

## Actions that must remain inside the jobs screen

The jobs screen must support these actions directly:

* [ ] new job
* [ ] edit prompt
* [ ] reschedule
* [ ] change session
* [ ] unschedule
* [ ] submit / repair
* [ ] retry
* [ ] delete
* [ ] refresh
* [ ] cycle filter
* [ ] toggle detail / responsive mode behavior

## Input model for actions

For actions that require extra input, use app-owned overlays or inline prompt components.

### Required overlays / inline prompts

* [ ] confirm delete
* [ ] input schedule spec
* [ ] session picker
* [ ] transient informational message area

Do not use standalone dialog helpers from the fullscreen app.

## Action architecture

## Replace async action wrapper with synchronous dispatch

The jobs screen should use a single synchronous action dispatcher.

Recommended shape:

* keybinding handler calls a local action function
* action function performs synchronous operation
* action function returns a user-facing message string or raises `OperationError`
* dispatcher stores message, refreshes view, invalidates app

### Rules

* no `asyncio.run()`
* no return type that changes between bool/coroutine
* no un-awaited coroutine creation in key handlers
* no action wrapper that overwrites specific messages with generic text

### Message model

Every action should either:

* return a success message
* raise `OperationError`
* raise `KeyboardInterrupt` / explicit cancellation signal

The dispatcher alone should convert those into footer/status text.

## Confirmation model

Delete confirmation must happen inside the jobs screen.

Implement one of these patterns:

* a centered confirm overlay with Yes/No keybindings
* an inline footer prompt like `Delete job abc123? [y/N]`

Do not call standalone confirm dialogs from the active TUI.

## Session selection model

Session choice must also happen inside the jobs screen.

Do not call a separate radiolist dialog from the TUI.

Recommended behavior:

* open a session picker overlay
* show `New session` plus discovered sessions
* filterable or scrollable if needed
* confirm selection and return to jobs screen

## Prompt editing model

Editing in `$EDITOR` is allowed, but must be treated as a controlled suspend/resume operation.

Required behavior:

* [ ] suspend/leave the visual screen cleanly
* [ ] launch editor on the prompt file
* [ ] resume the jobs screen
* [ ] refresh prompt metadata/state
* [ ] show result message

This must be implemented as a dedicated TUI-safe path, not by reusing legacy modal helper logic.

## New job flow inside the jobs screen

The new job flow must stay within the jobs screen UX.

At minimum it should support:

* agent selection
* session selection (new/resume)
* schedule input
* prompt editing / prompt capture
* submit on creation

This can be a staged overlay flow inside the same application.

It does not need to be perfect on the first pass, but it must no longer rely on nested standalone dialogs launched from inside the fullscreen app.

## Concrete refactor requirements

## Phase 1: stabilize the event model

* [ ] Remove the async `run_action` pattern from the jobs screen.
* [ ] Remove any `asyncio.run()` usage from jobs screen action paths.
* [ ] Make all jobs screen actions synchronous from the TUI’s perspective.
* [ ] Remove any helper that returns a coroutine sometimes and a plain value other times for TUI use.
* [ ] Ensure every keybinding action fully executes or cleanly cancels without loop errors.

### Phase 1 acceptance criteria

* [ ] Delete no longer crashes with event-loop errors.
* [ ] No jobs screen action relies on `asyncio.run()`.
* [ ] No TUI keybinding silently drops an un-awaited coroutine.

## Phase 2: stop using standalone dialogs inside the fullscreen app

* [ ] Identify all jobs screen flows that call prompt_toolkit `.run()` dialogs indirectly or directly.
* [ ] Replace them with jobs-screen-owned overlays or inline prompts.
* [ ] Ensure confirm, input, and list selection are handled inside the TUI.

### Phase 2 acceptance criteria

* [ ] Delete confirmation is TUI-native.
* [ ] Reschedule input is TUI-native.
* [ ] Session selection is TUI-native.
* [ ] Filter control is TUI-native.
* [ ] No jobs screen action depends on `input_dialog`, `yes_no_dialog`, `radiolist_dialog`, or `message_dialog`.

## Phase 3: redesign the left pane

* [ ] Remove the current pipe-delimited header/body rendering model for the left pane.
* [ ] Implement a compact summary row renderer with explicit column widths.
* [ ] Reduce the summary fields to a small scan-friendly set.
* [ ] Move secondary metadata to the detail pane.
* [ ] Add truncation/ellipsis behavior where needed.

### Phase 3 acceptance criteria

* [ ] Header and rows visually align.
* [ ] The screen is usable in a normal terminal width.
* [ ] The left pane remains readable without horizontal sprawl.

## Phase 4: responsive layout behavior

* [ ] Add width-aware layout selection.
* [ ] Support at least list-only mode and split mode.
* [ ] Add a keybinding to toggle detail when in narrow mode, if automatic split is not possible.
* [ ] Ensure the layout does not assume an excessively wide terminal.

### Phase 4 acceptance criteria

* [ ] Narrow terminals remain usable.
* [ ] The user can still access detail information without widening the screen excessively.

## Phase 5: unify messaging and action feedback

* [ ] Centralize success/error/cancel message handling.
* [ ] Remove generic action wrapper text that overwrites specific messages.
* [ ] Ensure each action returns a precise, user-facing result.

### Phase 5 acceptance criteria

* [ ] Footer/status text consistently reflects the last action.
* [ ] Specific messages are preserved.

## Phase 6: remove conflicting legacy TUI behavior

* [ ] Remove TUI code paths that depend on mixed dialog/TUI assumptions.
* [ ] Remove obsolete helper usage from the fullscreen jobs screen.
* [ ] Prefer deleting dead or conflicting code over preserving compatibility.

### Phase 6 acceptance criteria

* [ ] The jobs screen code path is internally consistent.
* [ ] No legacy mixed-surface behavior remains in the fullscreen TUI.

## Non-goals

These are not required for this refactor unless directly needed by the implementation:

* redesigning argparse command structure
* splitting the file into modules right now
* changing backend job semantics
* changing persistence format
* building a mouse-driven UI
* preserving pre-alpha legacy UI quirks

## Implementation guidance

## Keep one file for now

Implement this in the current file if that is the fastest path.

Do not block the refactor on code organization purity. The priority is to make the jobs screen correct and coherent first.

## But structure the code for later extraction

Even in one file, create clearly separated sections for:

* TUI state/model
* summary row formatting
* detail formatting
* overlay/input state
* synchronous action dispatch
* keybindings
* layout selection

This prepares the later split without delaying the refactor.

## Suggested internal architecture in one file

Recommended logical sections:

1. `JobsScreenState`

   * selected index
   * filter
   * message
   * current mode
   * overlay state
   * cached jobs if needed

2. `SummaryRowRenderer`

   * width policy
   * row formatting
   * header formatting

3. `DetailRenderer`

   * selected job detail formatting

4. `OverlayState`

   * none / confirm / text input / picker / new-job flow

5. `dispatch_action(...)`

   * execute action
   * catch exceptions
   * set message
   * refresh invalidate

6. `jobs_menu()`

   * build layout
   * bind keys
   * run app

## Visual design guidance

The user wants terminal menus with a black background and a more modern feel.

Within prompt_toolkit styling constraints:

* use dark background as the baseline
* use restrained contrast
* highlight selection cleanly
* avoid noisy separators
* keep the footer compact
* keep the header calm and informative

Do not solve visual modernity with more text density.

## Behavior guidance for list density

When listing jobs, sessions, or similar items, always show enough information to scan meaningfully.

For jobs in the main list, that means the user should be able to see at a glance:

* what the job is
* whether it is active/completed/failed/invalid
* when it will run or ran
* which agent/session context it belongs to

But this must be done compactly.

## Removal policy

Because this project is pre-alpha, prefer the following policy:

* [ ] if an old TUI behavior conflicts with the new jobs-screen-first design, remove it
* [ ] if a helper exists only to bridge incompatible interaction models, remove or stop using it from the TUI
* [ ] do not keep compatibility code just because it existed first

## Final acceptance test checklist

* [ ] Start the jobs screen in a normal-width terminal and verify it is usable without excessive width.
* [ ] Navigate up/down through jobs.
* [ ] Cycle filters.
* [ ] Open selected job details.
* [ ] Reschedule a job entirely from the jobs screen.
* [ ] Change session entirely from the jobs screen.
* [ ] Unschedule from the jobs screen.
* [ ] Submit/repair from the jobs screen.
* [ ] Retry from the jobs screen.
* [ ] Delete from the jobs screen without event-loop errors.
* [ ] Edit prompt and return cleanly to the jobs screen.
* [ ] Confirm specific status messages appear after actions.
* [ ] Confirm there are no dialog `.run()` calls used by the fullscreen jobs screen path.
* [ ] Confirm there is no `asyncio.run()` in the fullscreen jobs screen path.
* [ ] Confirm obsolete mixed-interaction TUI code has been removed.

## Definition of done

This refactor is done when the jobs screen is a coherent standalone interface that manages jobs directly, works in a normal terminal, and no longer depends on mixed modal-dialog behavior or broken async bridging.

At that point, the code can be split into separate modules later without changing the interaction model again.
