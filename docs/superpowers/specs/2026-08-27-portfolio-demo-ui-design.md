# Nexora Portfolio Demo UI Redesign

**Date:** 2026-08-27  
**Status:** Approved direction; implementation pending

## Context

The control-plane behavior is already the strongest part of the project. The current UI exposes that behavior as a dense three-column operator console, but asks a first-time visitor to understand eight scenarios, seven policy units, lifecycle hooks, and a blank run stream before seeing the point.

The redesign keeps the console and its behavior. It changes the first-use path so a technical reviewer can inspect one concrete scenario immediately, then move into the full composer when they want more control.

Primary audience: software engineers, technical hiring managers, and reviewers evaluating the project as an engineering portfolio piece.

## Goals

- Put a real scenario, its prompt, selected policies, and its run result on one screen.
- Let a visitor reach the representative DLP behavior with one click.
- Describe only inputs, policy decisions, and observed outcomes.
- Preserve every existing scenario, policy unit, streaming state, approval flow, steering flow, abort flow, and recovery flow.
- Make the first screen readable without prior Nexora knowledge.
- Keep the advanced composer available without making it the entry point.
- Improve keyboard, screen-reader, mobile, loading, and error behavior.

## Non-goals

- No marketing landing page or hero section.
- No new control-plane units, scenarios, tools, or backend execution semantics.
- No Supabase authentication, quotas, deployment configuration, or recorded replay in this change.
- No architecture essay inside the main interaction.
- No fabricated run output before the user starts a run.

## Copy rule

The interface does not make value claims. It labels concrete state.

Avoid:

- “위험한 AI 에이전트를 정책으로 통제합니다.”
- “정책을 바꾸면 행동이 바뀝니다.”
- “안전하고 강력한 control plane.”

Use:

- “기밀 외부 유출”
- “메일 본문에서 SSN 감지.”
- “send_email 실행 안 됨”
- “승인 대기”
- “정책 끄고 다시 실행”

English remains for code-level identifiers and runtime verdicts: approval, dlp_block, SUSPEND, DENY. Korean explains the observed event in one short line.

## Information architecture

The application has two modes that share one run state.

### Representative demo

This is the default mode.

- Header: Nexora identity and model name. A repository link is omitted until a public repository URL exists.
- Scenario panel: selected scenario, locked prompt, active policy chips, run action.
- Run panel: streamed lifecycle rows, current status, approval/recovery controls, final result.
- Secondary actions: rerun without policy, choose another scenario, open policy composer.

The initial selection is:

- Scenario: leak
- Units: approval, dlp_block

The application does not start automatically. The run panel begins in a ready state with the neutral message “실행 로그가 여기에 표시됩니다.” Clicking **실행** starts the existing live NDJSON flow.

After completion, **정책 끄고 다시 실행** runs the same scenario with no selected units. The new run replaces the visible timeline; the interface does not imply a side-by-side comparison it does not retain.

### Policy composer

This contains the full functionality of the current console.

- All eight scenarios remain available.
- Units remain grouped by lifecycle hook and composer.
- The live ControlPlane(...) summary remains visible.
- Abort, steer, approval, recovery, fired/dormant summaries, and execution counts remain available.
- Returning to the representative demo does not mutate the active selection or current run.

A simple mode switch is sufficient. This is not separate routing or a second application.

## Desktop layout

The representative demo uses a two-column workspace.

- Left: fixed-width scenario panel, approximately 38–42%.
- Right: flexible run panel, approximately 58–62%.
- Maximum content width: about 1180–1240 px.
- The run panel is visually stronger because execution evidence is the main artifact.
- No empty decorative hero area.
- No dashboard metric cards.

The policy composer can retain a three-column structure, but its widths and typography must be relaxed. Scenario and policy controls should not compete visually with the stream.

## Mobile layout

Below approximately 820 px:

- Header metadata wraps or hides nonessential model detail.
- Scenario panel appears before the run panel.
- The primary run button remains in normal document flow; it is not sticky over streamed content.
- Policy groups become a single column.
- Long prompts and code identifiers wrap without horizontal scrolling.
- Approval and recovery actions stack vertically when needed.

## Visual system

Keep the existing dark control-room palette and verdict colors. They already encode runtime meaning.

- Blue: selection and neutral interaction.
- Green: allow/completed.
- Amber: suspend/approval.
- Red: deny/error.
- Teal: rewrite.
- Purple: steer.

Changes:

- Increase body and explanatory copy size.
- Reduce decorative uppercase labels.
- Reserve monospace for identifiers, hooks, verdicts, and payloads.
- Use spacing and borders before adding more color.
- Add clear focus-visible states.
- Respect reduced-motion preferences.
- Do not rely on color alone; every verdict keeps a text label.

## State and data flow

1. boot() fetches /api/scenarios and /api/units.
2. It initializes representative mode with leak, approval, and dlp_block.
3. The user starts a run through the existing /api/run endpoint.
4. The existing NDJSON reader feeds frames into the reducer.
5. The run panel renders the reducer output as the current timeline.
6. Suspended and recoverable frames reveal the existing action controls.
7. Abort and steer continue to address the active run_id.
8. Completed runs render the final outcome and fired/dormant policy summary.
9. Compare-without-policy starts a new run using the same scenario and an empty unit list.

Only one run can be active in the browser. Mode switching during an active run does not create a second run.

## Component boundaries

### HTML shell

index.html owns semantic landmarks and stable containers:

- application header
- mode navigation
- representative scenario panel
- run panel
- advanced composer panel
- live regions for status and errors

### Presentation

styles.css owns layout, responsive behavior, focus states, verdict styling, and reduced-motion behavior. It does not encode runtime state beyond CSS classes supplied by JavaScript.

### Interaction

app.js owns API loading, current mode, selection state, active run state, control bindings, and DOM rendering.

Preset selection should be represented as data, not repeated DOM-manipulation branches. If extracting a small presets.mjs makes this independently testable, do so; otherwise keep a single constant near application state.

### Stream reduction

reducer.mjs and ndjson.mjs retain their current responsibilities. The redesign consumes their output and does not duplicate stream assembly in view code.

## Error handling

- Failure to load scenarios or units replaces the workspace with a short inline error and retry action.
- A run failure remains in the run panel and preserves the selected scenario and policies.
- Provider errors shown to the visitor should be concise; raw stack traces are never rendered.
- Disconnect or truncated NDJSON ends the busy state and exposes retry.
- Buttons that would create conflicting actions are disabled while a request is active.
- Denied approval, abort, and recovery conflict keep their existing distinct statuses.

## Accessibility

- Scenario and unit toggle buttons expose aria-pressed.
- Mode navigation exposes the active mode.
- Run status uses a polite live region.
- Errors use an assertive live region.
- Approval and recovery controls receive focus when they appear.
- Every input has a programmatic label.
- All interactive elements are keyboard reachable.
- Focus indication is visible against the dark background.
- The DOM order matches the mobile reading order.

## Testing

Automated checks:

- Existing Python suite remains green.
- Existing NDJSON and reducer Node tests remain green.
- Add tests for the default representative preset.
- Add tests that rerun-without-policy preserves the scenario and clears only units.
- Add tests for mode switching without resetting active run state.
- Add static assertions for semantic landmarks and accessibility attributes where practical.
- Preserve tests for suspended, denied, aborted, recoverable, and completed streams.

Manual checks:

- First load at desktop and mobile widths.
- Keyboard-only scenario selection, mode switch, run, approval, and recovery.
- Long Korean prompt wrapping.
- Loading, provider error, stream disconnect, and retry.
- Verdicts remain understandable without color.
- No run output is shown before an actual run.

## Expected file changes

- src/console/static/index.html
- src/console/static/styles.css
- src/console/static/app.js
- src/console/static/reducer.mjs only if view-facing state needs a small extension
- tests/test_stream.mjs
- tests/test_server.py
- Optional: src/console/static/presets.mjs when extracted for testability

Backend execution endpoints and policy implementations remain unchanged.

## Acceptance criteria

- A new visitor sees the leak scenario, its locked prompt, and approval + dlp_block selected without making choices.
- One click starts that scenario through the real streaming endpoint.
- The UI reports SUSPEND and DENY as emitted and shows that send_email was not executed.
- The visitor can rerun the same scenario without policies.
- The visitor can open the full composer and access every current scenario and control.
- No marketing claim or fabricated result appears on the first screen.
- Desktop, mobile, keyboard, loading, and error states remain usable.
- All automated tests pass.
