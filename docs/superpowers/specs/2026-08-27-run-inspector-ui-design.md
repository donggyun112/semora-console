# Run Inspector UI Redesign

**Date:** 2026-08-27  
**Status:** Proposed  
**Supersedes:** `2026-08-27-portfolio-demo-ui-design.md`

## Goal

Replace the current two-card demo/composer screen with a run-centered inspector that makes one control-plane execution easy to start, follow, and explain.

The first screen should contain only the choices required to start a run. Once a run begins, the execution trace becomes the primary surface. Configuration, raw payloads, and the full policy composer stay available on demand instead of competing for attention.

## Audience

Software engineers and technical reviewers evaluating Nexora as an engineering portfolio project.

The page should answer three questions without explanatory marketing copy:

1. What task will run?
2. Which controls are active?
3. What did those controls change?

## Reference decisions

The design adapts patterns from:

- [Inngest Trace View](https://www.inngest.com/docs/platform/monitor/traces): execution timeline as the primary surface, with contextual details for the selected step.
- [Langfuse trace guidance](https://langfuse.com/docs/observability/best-practices): one agent run per trace, meaningful steps only, readable root input/output, and raw payloads moved out of the primary view.
- [Trigger.dev Runs](https://trigger.dev/docs/runs): explicit run lifecycle, run-level status, payload, cancellation, waiting states, and replay actions.

These are references, not layouts to clone. Nexora's distinctive content is the policy verdict at each lifecycle hook and the visible difference between a requested tool call and an executed tool call.

## What is removed

The existing screen structure is discarded:

- no permanent demo/composer mode tabs
- no two equal-weight bordered cards
- no large empty log panel before execution
- no scenario counter
- no model name in the global header
- no separate “risk” sentence competing with the task
- no always-visible locked-prompt box
- no full policy composer occupying the main canvas
- no nested card for every content group

Existing runtime capabilities remain; only their presentation and lifecycle discipline change.

## Information architecture

### Global header

A compact 48px header contains:

- Nexora wordmark
- current environment label: `LOCAL DEMO`
- `Policies` button, which opens the composer drawer
- GitHub/repository link if a public URL is configured

It does not contain model metadata, a tagline, mode switches, or run details.

### Idle state: launch surface

Before a run, the page uses one centered column with a maximum width of 760px.

Order:

1. small scenario selector
2. scenario title
3. the exact task prompt, readable as normal text
4. two selected policy tokens
5. primary `실행` button
6. quiet `정책 편집` action

For the default preset:

- scenario: `leak`
- title: `기밀 외부 유출 시도`
- policies: `approval`, `dlp_block`
- no run starts automatically
- no timeline, fake result, or oversized empty placeholder is shown

The prompt may wrap to multiple lines, but it is not displayed inside a code editor or bordered sub-card.

### Running state: inspector

Starting a run collapses the launch surface into a compact run header:

- scenario title
- active policy tokens
- run status
- elapsed time
- abort action

The remaining viewport becomes the inspector.

Desktop layout:

- trace column: flexible width, minimum 640px
- details drawer: 380px, hidden until a trace row is selected

The trace column is not a terminal window. It is a chronological list with a single vertical rail.

Each row contains only:

- sequence marker
- hook or step name
- short human-readable result
- verdict text when applicable
- duration/execution count when available

Examples:

- `on_inputs · 입력 검사 완료`
- `approval · SUSPEND · 사용자 승인을 기다리는 중`
- `send_email · 실행 전 거부`
- `dlp_block · DENY · 주민번호가 외부 주소로 나가는 요청`

Tool arguments, raw frames, model content, and metadata appear only in the details drawer after row selection.

While streaming, steering is a compact composer pinned below the trace. It shows queued steering messages as plain rows in the same timeline. Steering is disabled in suspended, recoverable, terminal, and error phases so it cannot imply a message will reach a parked or finished agent.

### Details drawer

The drawer is contextual, not permanent.

It contains:

- selected step name and status
- input
- output or error
- policy verdict details
- raw payload disclosure
- copy action

Closing the drawer returns the trace to full width. On mobile, the drawer becomes a bottom sheet.

### Suspended state

A suspended trace remains the active run.

The corresponding timeline row expands inline and contains:

- the requested action summary
- `승인`
- `거부`

While suspended:

- scenario and policy configuration cannot change
- new-run and policy-free-rerun actions remain unavailable
- the active run ID and configuration snapshot remain visible
- approval acts on the existing run rather than creating another run

### Recoverable state

A recoverable trace also remains active.

The failure row expands inline with:

- concise failure reason
- `복구` action
- attempt count when available

New runs remain unavailable until the existing run reaches a terminal state or is explicitly aborted.

### Terminal state

When the run ends, a compact outcome strip appears above the trace.

For the default leak demo, the outcome should read concretely:

- verdict: `DENY`
- affected tool: `send_email`
- observed result: `실행 안 됨`

Terminal actions:

- `정책 없이 다시 실행`
- `같은 설정으로 다시 실행`
- `정책 편집`

No action implies that the prior run is being resumed; reruns always create a new run.

## Policy composer drawer

The full composer moves into a drawer opened from the global header or idle launch surface.

It preserves:

- all existing scenarios
- all existing policy units
- lifecycle-hook grouping
- fired/dormant state
- execution counts
- compose summary

The drawer uses one scroll surface. Scenario selection is a compact list; policy units are grouped accordions. Applying changes updates the idle draft configuration.

During a nonterminal run, the drawer may show the active snapshot but cannot mutate it. The UI must not present draft configuration as if it were the configuration currently executing.

## Run lifecycle model

Browser state uses explicit phases:

- `idle`
- `streaming`
- `suspended`
- `recoverable`
- `terminal`
- `error`

`busy` alone is not the lifecycle model.

Rules:

- a run snapshots scenario and policy selection at start
- only `idle`, `terminal`, and `error` allow a new run
- `suspended` and `recoverable` keep the current run active
- scenario/policy controls are disabled for all nonterminal phases
- abort transitions the active run to a terminal state
- resume and recover operate on the existing run ID
- a second continuation request is ignored while one is in flight

## Stream completion and errors

NDJSON processing must distinguish a valid terminal stream from a dropped stream.

- malformed JSON is an error
- a truncated final JSON fragment is an error
- EOF without `outcome`, `suspended`, `recoverable`, or `error` is an error
- an error is announced in the inspector and exposes a retry action
- partial rows stay visible for diagnosis
- unexpected EOF is never labeled completed
- retry after a stream error starts a new run from the frozen configuration snapshot; it never resumes an unknown partial backend state
- partial-run actions are disabled until the user retries or returns to the editable idle draft

## Visual direction

The interface remains dark but no longer uses uniform black cards.

- page canvas: deep graphite
- inspector surface: slightly lighter continuous plane
- borders: used for separators and focus, not containers around every group
- primary action: blue
- `SUSPEND`: amber plus text
- `DENY`: red plus text
- `ALLOW`: green plus text
- body text: system sans
- identifiers, hooks, verdicts, and payloads: monospace
- maximum of three type sizes on the main surface
- no gradients, decorative glow, oversized empty areas, or marketing illustration

Hierarchy comes from spacing, typography, the trace rail, and state changes—not from adding more boxes.

## Responsive behavior

At widths below 820px:

- header actions collapse to icons or a menu
- launch surface remains one column
- trace uses the full viewport width
- details drawer becomes a bottom sheet
- expanded approval/recovery actions stack without overflow
- long prompts, identifiers, steering text, and payloads wrap
- no horizontal page scroll

## Accessibility

- every control is keyboard reachable
- scenario and policy selections expose selected/pressed state
- focus is visibly distinct from hover
- status updates use a polite live region
- errors use an assertive live region
- inline approval/recovery panels receive focus when they appear
- verdict meaning is always present in text
- reduced-motion users receive no pulsing or row-entry motion

## Copy rules

Use short, concrete Korean labels.

Prefer:

- `주민번호가 외부 주소로 나가는 요청`
- `사용자 승인을 기다리는 중`
- `send_email 실행 안 됨`

Avoid:

- product claims
- manifesto language
- “안전한 AI를 위한 혁신적인 제어 계층”
- invented results or personal experience

## Acceptance criteria

- the old two-card and mode-tab layout is absent
- idle state contains no empty log panel
- default leak + approval/dlp_block preset remains
- execution changes the page into the trace inspector
- raw details remain hidden until a row is selected
- active configuration is snapshotted and cannot drift during a run
- suspended/recoverable runs cannot be replaced by a new run
- approval, denial, recovery, abort, and steering remain functional
- malformed/truncated/nonterminal streams show an error, never success
- terminal outcome states the verdict, affected tool, and observed execution result
- full scenario/policy composer remains available in a drawer
- desktop and mobile render without horizontal overflow
- keyboard, live-region, non-color verdict, and reduced-motion behavior remains
- existing backend endpoint and payload semantics remain unchanged
- Python and Node suites pass
