# Run Inspector UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the current two-card demo/composer presentation and replace it with an idle launch surface that becomes a trace-first run inspector.

**Architecture:** Introduce an immutable run-lifecycle module so parked runs cannot be confused with idle ones, make the NDJSON boundary reject malformed or incomplete streams, and reshape reducer output into concise trace rows with contextual details. Replace the HTML and application bindings together so no committed state serves a broken shell, then rebuild the CSS around one continuous inspector surface and drawer-based secondary controls.

**Tech Stack:** FastAPI static files, vanilla HTML/CSS/ES modules, Node `assert` tests with a dependency-free fake DOM, pytest/TestClient, headless Chrome for rendered acceptance.

**Spec:** `docs/superpowers/specs/2026-08-27-run-inspector-ui-design.md`

## Global Constraints

- The old two-card and permanent demo/composer mode-tab layout must be absent.
- Idle state contains no timeline, fake output, or empty log panel.
- Default scenario is `leak` with `approval` and `dlp_block`.
- The application must not start a run automatically.
- One run snapshots its scenario and units; draft selections cannot drift while it is nonterminal.
- Only `idle`, `terminal`, and `error` allow a new run.
- Suspended and recoverable runs retain their run ID and must be continued, recovered, or aborted before replacement.
- Malformed JSON, a truncated final fragment, and nonterminal EOF are errors rather than successful completion.
- Preserve every existing backend endpoint and JSON payload shape.
- Preserve all scenarios, policy units, abort, steering, approval, denial, recovery, fired/dormant state, and execution counts.
- Raw payloads and model content remain out of the primary trace and appear only in contextual details.
- Verdicts always retain text labels; color is supplemental.
- Keep a dark graphite palette without gradients, glow, or bordered cards around every group.
- Use monospace only for identifiers, hooks, verdicts, and payloads.
- At widths below `820px`, details become a bottom sheet and the page has no horizontal scroll.
- All controls remain keyboard reachable, focus-visible, live-region aware, and reduced-motion safe.
- Do not add dependencies, backend semantics, auth, quotas, deployment configuration, or replay storage.
- Python and Node suites must remain green.

---

## File structure

- Create `src/console/static/run-state.mjs`: immutable draft snapshot and run-phase transitions.
- Modify `src/console/static/ndjson.mjs`: strict line parsing with named parse errors.
- Modify `src/console/static/reducer.mjs`: structured trace rows and contextual detail payloads.
- Modify `src/console/static/index.html`: new header, launch surface, inspector, details drawer, and policy drawer.
- Modify `src/console/static/app.js`: boot, draft editing, streaming, lifecycle, drawers, and endpoint bindings.
- Modify `src/console/static/styles.css`: replace the previous visual system with the Run Inspector layout.
- Delete `src/console/static/view-state.mjs`: obsolete demo/composer mode state.
- Delete `src/console/static/run-guard.mjs`: superseded by explicit lifecycle transitions.
- Modify `tests/test_stream.mjs`: pure lifecycle, parser, reducer, and real app-listener coverage.
- Modify `tests/test_server.py`: served-shell and stylesheet contracts.

### Task 1: Explicit run lifecycle and frozen configuration

**Files:**
- Create: `src/console/static/run-state.mjs`
- Modify: `tests/test_stream.mjs`

**Interfaces:**
- Produces: `DEFAULT_DRAFT`, `createRunState()`, `updateDraft()`, `startRun()`, `attachRunId()`, `suspendRun()`, `markRecoverable()`, `beginContinuation()`, `finishRun()`, `failRun()`, `returnToDraft()`, `canStartRun()`, `canEditDraft()`, `canSteer()`, and `acceptsStreamEnd()`.
- Consumes: no DOM or browser globals.

- [ ] **Step 1: Add failing lifecycle assertions**

Replace the old `view-state.mjs` and `startWhenIdle` assertion block in `tests/test_stream.mjs` with imports and literal expectations:

```javascript
import {
  DEFAULT_DRAFT,
  acceptsStreamEnd,
  attachRunId,
  beginContinuation,
  canEditDraft,
  canStartRun,
  canSteer,
  createRunState,
  failRun,
  finishRun,
  markRecoverable,
  returnToDraft,
  startRun,
  suspendRun,
  updateDraft,
} from "../src/console/static/run-state.mjs";

const idle = createRunState();
assert.deepEqual(idle, {
  phase: "idle",
  draft: { scenarioId: "leak", unitNames: ["approval", "dlp_block"] },
  active: null,
  runId: null,
  pendingId: null,
  continuationBusy: false,
  stopReason: null,
  error: null,
});
assert.deepEqual(DEFAULT_DRAFT, {
  scenarioId: "leak",
  unitNames: ["approval", "dlp_block"],
});

const edited = updateDraft(idle, { scenarioId: "baseline", unitNames: [] });
assert.equal(idle.draft.scenarioId, "leak");
assert.deepEqual(edited.draft, { scenarioId: "baseline", unitNames: [] });

const streaming = attachRunId(startRun(idle), "run-1");
assert.deepEqual(streaming.active, {
  scenarioId: "leak",
  unitNames: ["approval", "dlp_block"],
});
assert.equal(canStartRun(streaming), false);
assert.equal(canEditDraft(streaming), false);
assert.equal(canSteer(streaming), true);

const suspended = suspendRun(streaming, "pending-1");
assert.equal(suspended.phase, "suspended");
assert.equal(canStartRun(suspended), false);
assert.equal(canSteer(suspended), false);
assert.equal(acceptsStreamEnd(suspended), true);

const resuming = beginContinuation(suspended);
assert.equal(resuming.phase, "streaming");
assert.equal(resuming.continuationBusy, true);
assert.throws(() => beginContinuation(resuming), /continuation already active/);

const recoverable = markRecoverable(streaming);
assert.equal(recoverable.phase, "recoverable");
assert.equal(canStartRun(recoverable), false);
assert.equal(acceptsStreamEnd(recoverable), true);

const terminal = finishRun(streaming, "completed");
assert.equal(terminal.phase, "terminal");
assert.equal(terminal.stopReason, "completed");
assert.equal(canStartRun(terminal), true);

const failed = failRun(streaming, "truncated stream");
assert.equal(failed.phase, "error");
assert.equal(failed.error, "truncated stream");
assert.equal(acceptsStreamEnd(failed), true);

const retryDraft = returnToDraft(failed, { source: "active", withoutPolicies: true });
assert.deepEqual(retryDraft.draft, { scenarioId: "leak", unitNames: [] });
assert.equal(retryDraft.phase, "idle");
assert.throws(
  () => updateDraft(suspended, { scenarioId: "other" }),
  /draft locked/,
);
```

- [ ] **Step 2: Run RED**

Run:

```bash
node --test tests/test_stream.mjs
```

Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `run-state.mjs`.

- [ ] **Step 3: Implement the pure lifecycle module**

Use immutable object/array copies. Validate phases through one internal `transition(state, phase, patch)` helper. `acceptsStreamEnd()` returns true only for `suspended`, `recoverable`, `terminal`, and `error`. `beginContinuation()` accepts only suspended/recoverable state, rejects `continuationBusy`, and returns streaming state without changing `active`, `runId`, or `pendingId`.

```javascript
export const DEFAULT_DRAFT = Object.freeze({
  scenarioId: "leak",
  unitNames: Object.freeze(["approval", "dlp_block"]),
});

const STARTABLE = new Set(["idle", "terminal", "error"]);
const EDITABLE = new Set(["idle", "terminal", "error"]);
const ENDABLE = new Set(["suspended", "recoverable", "terminal", "error"]);

const copyConfig = (config) => ({
  scenarioId: config.scenarioId,
  unitNames: [...config.unitNames],
});

export function createRunState() {
  return {
    phase: "idle",
    draft: copyConfig(DEFAULT_DRAFT),
    active: null,
    runId: null,
    pendingId: null,
    continuationBusy: false,
    stopReason: null,
    error: null,
  };
}

export const canStartRun = (state) => STARTABLE.has(state.phase);
export const canEditDraft = (state) => EDITABLE.has(state.phase);
export const canSteer = (state) =>
  state.phase === "streaming" && Boolean(state.runId);
export const acceptsStreamEnd = (state) => ENDABLE.has(state.phase);

export function updateDraft(state, patch) {
  if (!canEditDraft(state)) throw new Error("draft locked");
  return {
    ...state,
    draft: {
      scenarioId: patch.scenarioId ?? state.draft.scenarioId,
      unitNames: patch.unitNames
        ? [...patch.unitNames]
        : [...state.draft.unitNames],
    },
  };
}

export function startRun(state) {
  if (!canStartRun(state)) throw new Error("run already active");
  return {
    ...state,
    phase: "streaming",
    active: copyConfig(state.draft),
    runId: null,
    pendingId: null,
    continuationBusy: false,
    stopReason: null,
    error: null,
  };
}

export function attachRunId(state, runId) {
  if (state.phase !== "streaming") throw new Error("run id outside stream");
  return { ...state, runId };
}

export function suspendRun(state, pendingId) {
  if (state.phase !== "streaming") throw new Error("cannot suspend run");
  return {
    ...state,
    phase: "suspended",
    pendingId,
    continuationBusy: false,
  };
}

export function markRecoverable(state) {
  if (state.phase !== "streaming") throw new Error("cannot park recovery");
  return {
    ...state,
    phase: "recoverable",
    continuationBusy: false,
  };
}

export function beginContinuation(state) {
  if (state.continuationBusy || state.phase === "streaming") {
    throw new Error("continuation already active");
  }
  if (!["suspended", "recoverable"].includes(state.phase) || !state.runId) {
    throw new Error("no parked run");
  }
  return { ...state, phase: "streaming", continuationBusy: true };
}

export function finishRun(state, stopReason) {
  if (!state.active) throw new Error("no active run");
  return {
    ...state,
    phase: "terminal",
    pendingId: null,
    continuationBusy: false,
    stopReason,
    error: null,
  };
}

export function failRun(state, message) {
  if (!state.active) throw new Error("no active run");
  return {
    ...state,
    phase: "error",
    continuationBusy: false,
    error: message,
  };
}

export function returnToDraft(
  state,
  { source = "draft", withoutPolicies = false } = {},
) {
  if (!canStartRun(state)) throw new Error("run still active");
  const selected = source === "active" ? state.active : state.draft;
  if (!selected) throw new Error("missing source config");
  const draft = copyConfig(selected);
  if (withoutPolicies) draft.unitNames = [];
  return {
    ...createRunState(),
    draft,
  };
}
```

- [ ] **Step 4: Run GREEN and regression tests**

```bash
node --test tests/test_stream.mjs
/Users/dongkseo/project/nexora-console/.venv/bin/pytest -q
```

Expected: Node passes; Python passes with only the existing Postgres skip.

- [ ] **Step 5: Commit**

```bash
git add src/console/static/run-state.mjs tests/test_stream.mjs
git commit -m "feat(ui): model run lifecycle explicitly"
```

### Task 2: Strict NDJSON stream boundary

**Files:**
- Modify: `src/console/static/ndjson.mjs`
- Modify: `tests/test_stream.mjs`

**Interfaces:**
- Produces: `NdjsonParseError extends Error`.
- Preserves: `createNdjsonReader(onFrame)` with `push(bytes)` and `end()`.
- Consumes: `acceptsStreamEnd()` in the later application task, not inside the parser.

- [ ] **Step 1: Add failing malformed-stream tests**

```javascript
import {
  NdjsonParseError,
  createNdjsonReader,
} from "../src/console/static/ndjson.mjs";

const malformed = createNdjsonReader(() => {});
assert.throws(
  () => malformed.push(new TextEncoder().encode('{"kind":}\n')),
  (error) =>
    error instanceof NdjsonParseError &&
    error.line === '{"kind":}',
);

const truncated = createNdjsonReader(() => {});
truncated.push(new TextEncoder().encode('{"kind":"agent"'));
assert.throws(
  () => truncated.end(),
  (error) =>
    error instanceof NdjsonParseError &&
    error.message.includes("trailing"),
);
```

- [ ] **Step 2: Run RED**

```bash
node --test tests/test_stream.mjs
```

Expected: FAIL because malformed input is currently swallowed and `NdjsonParseError` is absent.

- [ ] **Step 3: Replace silent catches**

```javascript
export class NdjsonParseError extends Error {
  constructor(message, line, options = {}) {
    super(message, options);
    this.name = "NdjsonParseError";
    this.line = line;
  }
}

function parseLine(line, location) {
  try {
    return JSON.parse(line);
  } catch (cause) {
    throw new NdjsonParseError(`invalid NDJSON ${location}`, line, {
      cause,
    });
  }
}
```

Use `parseLine(line, "line")` for newline-delimited frames and `parseLine(tail, "trailing fragment")` in `end()`. Keep the existing split-Hangul and final-valid-line behavior.

- [ ] **Step 4: Run GREEN**

```bash
node --test tests/test_stream.mjs
```

Expected: all parser, reducer, and lifecycle assertions pass with pristine output.

- [ ] **Step 5: Commit**

```bash
git add src/console/static/ndjson.mjs tests/test_stream.mjs
git commit -m "fix(ui): reject malformed run streams"
```

### Task 3: Structured trace rows with hidden raw details

**Files:**
- Modify: `src/console/static/reducer.mjs`
- Modify: `tests/test_stream.mjs`

**Interfaces:**
- Produces: `reduceFrames(frames)` rows shaped as `{ id, kind, label, summary, verdict, tone, details }`.
- Produces: `summarizeOutcome(frames)` returning `{ verdict, tool, result }` for the outcome strip.
- `details` contains raw input/output/frame content used by the contextual drawer.
- Tool-call rows keep stable ID `tool:<call_id>` so a later blocked event replaces the pending row.

- [ ] **Step 1: Replace presentation-string assertions with structured expectations**

Add literal cases:

```javascript
const traceFrames = [
  { kind: "meta", run_id: "run-1" },
  { kind: "lifecycle", type: "pre_tool_use", payload: { name: "send_email" } },
  {
    kind: "agent",
    event: {
      type: "tool_call",
      id: "call-1",
      name: "send_email",
      input: { to: "leaker@personal-mail.com", body: "secret" },
    },
  },
  {
    kind: "unit",
    unit: "dlp_block",
    verdict: "deny",
    message: "주민번호가 외부 주소로 나가는 요청",
  },
  {
    kind: "agent",
    event: {
      type: "tool_call",
      id: "call-1",
      name: "send_email",
      input: { to: "leaker@personal-mail.com", body: "secret" },
      blocked: true,
    },
  },
];
const trace = reduceFrames(traceFrames);

assert.deepEqual(
  trace.map(({ kind, label, summary, verdict }) => ({
    kind, label, summary, verdict,
  })),
  [
    { kind: "lifecycle", label: "pre_tool_use", summary: "send_email", verdict: null },
    { kind: "tool", label: "send_email", summary: "실행 전 거부", verdict: "DENY" },
    {
      kind: "policy",
      label: "dlp_block",
      summary: "주민번호가 외부 주소로 나가는 요청",
      verdict: "DENY",
    },
  ],
);
assert.deepEqual(trace[1].details.input, {
  to: "leaker@personal-mail.com",
  body: "secret",
});
assert.ok(
  trace.every((row) => !row.summary.includes("leaker@personal-mail.com")),
  "raw tool arguments stay out of trace summaries",
);
assert.deepEqual(summarizeOutcome([
  ...traceFrames,
  { kind: "outcome", outcome: { stop_reason: "completed" } },
]), {
  verdict: "DENY",
  tool: "send_email",
  result: "실행 안 됨",
});
```

Retain independently-derived tests for text aggregation, lifecycle ordering, tool execution count, steering queued/admitted rows, recoverable rows, and outcome rows.

- [ ] **Step 2: Run RED**

```bash
node --test tests/test_stream.mjs
```

Expected: FAIL because current rows expose formatted tool arguments and lack structured details.

- [ ] **Step 3: Refactor reducer output**

Use a monotonically increasing sequence for rows without protocol IDs and stable protocol IDs for tool calls. Store original frames under `details.raw`. Keep text aggregation internally, but expose the primary row as:

```javascript
{
  id: "agent:0",
  kind: "agent",
  label: "agent",
  summary: "응답 생성",
  verdict: null,
  tone: "neutral",
  details: { output: accumulatedText, raw: sourceFrames },
}
```

Tool results expose `summary: "실행 완료"` and `details.output`; policy rows expose uppercase verdict text and a matching semantic tone. `summarizeOutcome()` derives the last blocking verdict and blocked tool from frames, falling back to the terminal stop reason when no policy blocked a tool. Do not put JSON strings in `summary`.

- [ ] **Step 4: Run GREEN**

```bash
node --test tests/test_stream.mjs
```

Expected: all structured-row and ordering assertions pass.

- [ ] **Step 5: Commit**

```bash
git add src/console/static/reducer.mjs tests/test_stream.mjs
git commit -m "refactor(ui): derive structured trace rows"
```

### Task 4: Replace the browser shell and bind the Run Inspector

**Files:**
- Modify: `src/console/static/index.html`
- Modify: `src/console/static/app.js`
- Delete: `src/console/static/view-state.mjs`
- Delete: `src/console/static/run-guard.mjs`
- Modify: `tests/test_server.py`
- Modify: `tests/test_stream.mjs`

**Interfaces:**
- Consumes: lifecycle transitions from `run-state.mjs`, strict `createNdjsonReader()`, and structured `reduceFrames()`.
- Produces DOM IDs: `scenario-trigger`, `scenario-menu`, `launch-title`, `launch-prompt`, `launch-policies`, `run`, `policy-open`, `launch-policy-open`, `run-shell`, `run-title`, `run-status`, `run-policies`, `abort`, `outcome-strip`, `rerun-plain`, `rerun-same`, `retry-run`, `return-draft`, `trace`, `details-drawer`, `details-close`, `details-copy`, `details-title`, `details-body`, `steer-form`, `steer-text`, `policy-drawer`, `policy-close`, `scenarios`, `units`, `compose-summary`, `approval`, `approve`, `deny`, `recovery`, `recover`, `run-error`, `boot-error`, and `boot-retry`.
- Preserves endpoint bodies exactly:
  - `/api/run`: `{ scenario_id, units }`
  - `/api/steer`: `{ run_id, text }`
  - `/api/abort`: `{ run_id }`
  - `/api/recover`: `{ run_id }`
  - `/api/resume`: `{ run_id, pending_id, approved }`

- [ ] **Step 1: Write failing served-shell assertions**

Replace the obsolete mode-panel test with a parser-based contract that proves:

```python
def test_static_shell_is_run_inspector_with_contextual_drawers():
    with TestClient(app) as c:
        html = c.get("/").text

    class ShellParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids: list[str] = []
            self.elements: dict[str, dict[str, str | None]] = {}

        def handle_starttag(self, _tag, attrs):
            attributes = dict(attrs)
            if element_id := attributes.get("id"):
                self.ids.append(element_id)
                self.elements[element_id] = attributes

    shell = ShellParser()
    shell.feed(html)
    assert len(shell.ids) == len(set(shell.ids))

    required = {
        "scenario-trigger", "scenario-menu", "launch-title", "launch-prompt",
        "launch-policies", "run", "policy-open", "launch-policy-open",
        "run-shell", "run-status",
        "outcome-strip", "trace", "details-drawer", "details-close",
        "details-copy",
        "details-title", "details-body", "steer-form", "steer-text",
        "policy-drawer", "policy-close", "scenarios", "units",
        "compose-summary", "approval", "approve", "deny", "recovery",
        "recover", "abort", "rerun-plain", "rerun-same", "retry-run",
        "return-draft", "run-error", "boot-error", "boot-retry",
    }
    assert required <= set(shell.ids)
    assert {"mode-demo", "mode-composer", "demo-panel", "composer-panel"}.isdisjoint(
        shell.ids
    )
    assert "hidden" in shell.elements["run-shell"]["class"].split()
    assert "hidden" in shell.elements["details-drawer"]["class"].split()
    assert "hidden" in shell.elements["policy-drawer"]["class"].split()
    assert shell.elements["trace"]["role"] == "log"
    assert shell.elements["run-error"]["aria-live"] == "assertive"
```

- [ ] **Step 2: Run RED**

```bash
/Users/dongkseo/project/nexora-console/.venv/bin/pytest -q tests/test_server.py::test_static_shell_is_run_inspector_with_contextual_drawers
```

Expected: FAIL because the old mode tabs and panels are still served.

- [ ] **Step 3: Replace `index.html`**

Create one `<main id="app">` containing:

```html
<section id="launch" class="launch" aria-labelledby="launch-title">
  <button id="scenario-trigger" type="button" aria-expanded="false"></button>
  <div id="scenario-menu" class="scenario-menu hidden"></div>
  <h1 id="launch-title"></h1>
  <p id="launch-prompt"></p>
  <div id="launch-policies" aria-label="선택된 정책"></div>
  <button id="run" type="button">실행</button>
  <button id="launch-policy-open" data-policy-open type="button">정책 편집</button>
</section>

<section id="run-shell" class="run-shell hidden" aria-labelledby="run-title">
  <header class="run-summary">
    <div><h1 id="run-title"></h1><div id="run-policies"></div></div>
    <span id="run-status" aria-live="polite">대기</span>
    <button id="abort" class="hidden" type="button">중단</button>
  </header>
  <div id="outcome-strip" class="outcome-strip hidden"></div>
  <div class="terminal-actions hidden">
    <button id="rerun-plain" type="button">정책 없이 다시 실행</button>
    <button id="rerun-same" type="button">같은 설정으로 다시 실행</button>
  </div>
  <div class="error-actions hidden">
    <button id="retry-run" type="button">다시 실행</button>
    <button id="return-draft" type="button">설정으로 돌아가기</button>
  </div>
  <div class="inspector">
    <div id="trace" class="trace" role="log" aria-live="polite"></div>
    <aside id="details-drawer" class="details-drawer hidden" aria-labelledby="details-title">
      <button id="details-close" type="button" aria-label="상세 닫기">×</button>
      <button id="details-copy" type="button">복사</button>
      <h2 id="details-title"></h2>
      <div id="details-body"></div>
    </aside>
  </div>
  <!-- steer-form, approval, and recovery remain inside run-shell -->
</section>
```

Add `<button id="policy-open" data-policy-open>` and `LOCAL DEMO` to the global header. Use a native `<dialog id="policy-drawer">` containing the existing `scenarios`, `units`, and `compose-summary` containers. Bind both `[data-policy-open]` buttons to `showModal()`; close on the close button or Escape and restore focus to the opener. Render each lifecycle-hook policy group as a `<details>` accordion. Do not include an initial trace placeholder or model label.

- [ ] **Step 4: Replace application state and boot rendering**

```javascript
const state = {
  run: createRunState(),
  scenarios: [],
  unitsMeta: new Map(),
  frames: [],
  rows: [],
  selectedRowId: null,
  abortCtl: null,
  steers: [],
};
```

Boot fetches scenarios/units, populates metadata, renders the idle draft, and binds listeners. It never calls `start()`. Scenario and policy handlers call `updateDraft()`; if `canEditDraft(state.run)` is false, they return before any DOM or state mutation.

- [ ] **Step 5: Implement lifecycle-aware streaming**

Before `/api/run`, set `state.run = startRun(state.run)`, clear prior frames/details, show `run-shell`, hide `launch`, and render the frozen `state.run.active`.

In `handleFrame(frame)`:

- `meta` → `attachRunId`
- `suspended` → `suspendRun` and expand/focus approval within its trace row
- `recoverable` → `markRecoverable` and expand/focus recovery within its trace row
- `outcome` → `finishRun` and render outcome strip
- `error` → `failRun` and announce `run-error`
- every render uses `reduceFrames(state.frames)`

After `ndjson.end()`, enforce:

```javascript
if (!acceptsStreamEnd(state.run)) {
  throw new Error("실행 스트림이 완료 상태 없이 종료되었습니다.");
}
```

The catch path preserves partial rows and calls `failRun`; it never appends a synthetic outcome or labels the run completed.

- [ ] **Step 6: Bind active-run controls**

- Abort remains available while streaming, suspended, or recoverable and transitions the existing run to terminal aborted.
- Steering requires `canSteer(state.run)`.
- Resume/recover first call `beginContinuation(state.run)`; a second call throws/returns before UI mutation or fetch.
- Policy drawer shows the active snapshot read-only during streaming/suspended/recoverable.
- Terminal rerun actions call `returnToDraft(state.run, { source: "active", withoutPolicies })` and then start a new run.
- Error retry uses the same frozen active snapshot; `return-draft` restores that snapshot as an editable draft without starting.
- `policy_summary` updates fired/dormant state and execution counts inside the policy drawer without changing the active snapshot.
- Suspended approval controls are appended inside the last relevant tool trace row; recovery controls are appended inside the recoverable trace row.
- Clicking a trace row sets `selectedRowId` and renders escaped `details` using `textContent`, never `innerHTML`.
- `details-copy` serializes only the selected row's `details` and writes it through `navigator.clipboard.writeText`; disable it when no row is selected.

- [ ] **Step 7: Replace fake-DOM integration assertions**

Update the element fixture to the new IDs and exercise the registered listeners:

- boot performs zero `/api/run` requests
- click Run sends exactly `{ scenario_id: "leak", units: ["approval", "dlp_block"] }`
- while the first stream is pending, scenario/policy listeners cannot change the active snapshot
- suspended stream end keeps Run disabled and preserves `runId`
- double Approve creates one `/api/resume` request
- recoverable stream end keeps Run disabled; double Recover creates one `/api/recover` request
- malformed or nonterminal EOF results in phase `error` and an assertive error message
- a terminal policy-free rerun creates a new `/api/run` request with the same scenario and `units: []`
- selecting a trace row opens details without raw content in the row summary; Copy sends the selected `details` JSON to a fake clipboard
- opening/closing the policy dialog restores focus and every scenario/unit remains represented

Use literal fake responses and real app-registered event listeners; do not assert source strings.

- [ ] **Step 8: Run GREEN**

```bash
node --check src/console/static/app.js
node --test tests/test_stream.mjs
/Users/dongkseo/project/nexora-console/.venv/bin/pytest -q tests/test_server.py
/Users/dongkseo/project/nexora-console/.venv/bin/pytest -q
```

Expected: syntax passes; Node passes; Python passes with only the existing Postgres skip.

- [ ] **Step 9: Delete obsolete modules and commit**

```bash
git rm src/console/static/view-state.mjs src/console/static/run-guard.mjs
git add src/console/static/index.html src/console/static/app.js tests/test_server.py tests/test_stream.mjs
git commit -m "feat(ui): replace console with run inspector"
```

### Task 5: Rebuild the visual system and responsive drawers

**Files:**
- Modify: `src/console/static/styles.css`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: classes and IDs introduced in Task 4.
- Produces: centered idle launch, continuous trace plane, 380px contextual drawer, policy drawer, 820px bottom-sheet behavior, visible focus, and reduced-motion overrides.

- [ ] **Step 1: Replace the old stylesheet contract with failing Run Inspector checks**

```python
def test_stylesheet_has_run_inspector_drawers_and_accessibility_contracts():
    with TestClient(app) as c:
        css = c.get("/styles.css").text

    for selector in [
        ".launch",
        ".run-shell",
        ".trace-row",
        ".details-drawer",
        ".policy-drawer",
        ".outcome-strip",
        ":focus-visible",
        "@media (max-width: 820px)",
        "@media (prefers-reduced-motion: reduce)",
    ]:
        assert selector in css
    assert ".workspace.mode-demo" not in css
    assert ".composer-panel" not in css
```

- [ ] **Step 2: Run RED**

```bash
/Users/dongkseo/project/nexora-console/.venv/bin/pytest -q tests/test_server.py::test_stylesheet_has_run_inspector_drawers_and_accessibility_contracts
```

Expected: FAIL because old workspace/composer selectors remain.

- [ ] **Step 3: Replace the stylesheet**

Use these exact layout contracts:

```css
:root {
  --canvas: #0b0d10;
  --surface: #11151a;
  --surface-2: #171c23;
  --line: #27303b;
  --text: #eef2f7;
  --muted: #8d98a8;
  --blue: #6f9cff;
  --amber: #f4b942;
  --red: #ff5d68;
  --green: #4fc68a;
}

.launch {
  width: min(760px, calc(100% - 40px));
  margin: clamp(72px, 14vh, 150px) auto 48px;
}

.inspector {
  display: grid;
  grid-template-columns: minmax(640px, 1fr);
  min-width: 0;
}

.inspector.has-details {
  grid-template-columns: minmax(640px, 1fr) 380px;
}

.trace {
  min-width: 0;
  border-top: 1px solid var(--line);
}

.trace-row {
  display: grid;
  grid-template-columns: 28px minmax(120px, 180px) minmax(0, 1fr) auto;
  gap: 12px;
  border-bottom: 1px solid var(--line);
}

.details-drawer,
.policy-drawer {
  background: var(--surface);
  border-left: 1px solid var(--line);
  min-width: 0;
  overflow: auto;
}
```

Use separators rather than outer card borders. Add `min-width: 0` and `overflow-wrap: anywhere` to trace summaries, detail values, steering body/input, and policy labels.

At `max-width: 820px`, make `.details-drawer` a fixed bottom sheet with `max-height: 72dvh`; make the policy drawer full width; collapse trace rows to `28px minmax(0, 1fr)` and move metadata beneath the label.

- [ ] **Step 4: Add interaction-state styling**

- `:focus-visible` uses a 2px blue outline with 2px offset.
- Selected trace rows use `surface-2` plus an `aria-selected` border marker.
- Verdict colors always sit beside visible uppercase verdict text.
- Disabled draft controls use opacity and cursor without hiding their selected state.
- Reduced motion sets animation and transition durations to `0.01ms`.

- [ ] **Step 5: Run GREEN**

```bash
/Users/dongkseo/project/nexora-console/.venv/bin/pytest -q tests/test_server.py
node --test tests/test_stream.mjs
git diff --check
```

Expected: server tests and Node pass; whitespace check is silent.

- [ ] **Step 6: Render at required boundaries**

Serve the worktree:

```bash
PYTHONPATH=src /Users/dongkseo/project/nexora-console/.venv/bin/uvicorn console.server:app --port 8850
```

Inspect with headless Chrome:

- idle at 1440×1000: no empty trace, one centered launch column
- streaming at 1440×1000: trace owns the canvas; details absent until row click
- selected row at 1440×1000: 380px details drawer, no page overflow
- policy drawer: all scenarios/units visible within one drawer scroll surface
- 820×900 and 390×844: details bottom sheet, stacked controls, no horizontal overflow
- suspended/recoverable: inline action row receives keyboard focus
- reduced motion: no visible pulse/entry animation
- 1,400-character prompt, steering text, trace summary, and detail payload: no overflow

Capture desktop idle, desktop trace-with-details, and mobile screenshots under `/private/tmp`; inspect each image rather than relying only on DOM dimensions.

- [ ] **Step 7: Commit**

```bash
git add src/console/static/styles.css tests/test_server.py
git commit -m "feat(ui): style the run inspector"
```

### Task 6: Final lifecycle, visual, and scope verification

**Files:**
- Verify: all files changed in Tasks 1-5
- Verify: `docs/superpowers/specs/2026-08-27-run-inspector-ui-design.md`
- Do not modify backend endpoint/policy files unless a failing pre-existing test proves an unrelated defect.

**Interfaces:**
- Consumes: complete Run Inspector implementation.
- Produces: clean test, browser, diff, and review evidence.

- [ ] **Step 1: Run the full suite from a fresh process**

```bash
/Users/dongkseo/project/nexora-console/.venv/bin/pytest -q
node --test tests/test_stream.mjs
node --check src/console/static/app.js
git diff --check
```

Expected: Python passes with only the existing Postgres skip; Node and syntax pass; whitespace check prints nothing.

- [ ] **Step 2: Audit the branch scope**

```bash
git diff --stat 045f3bc..HEAD
git diff --name-only 045f3bc..HEAD
git ls-files .superpowers
git status --short
```

Expected: only planned static/test files changed; no backend, auth, deploy, or `.superpowers` artifacts are tracked or staged.

- [ ] **Step 3: Verify every acceptance criterion**

Record direct evidence for:

- old mode tabs/cards absent
- idle state has no log placeholder
- default leak + two policies
- run swaps launch for inspector
- details hidden until row selection
- active snapshot cannot drift
- suspended/recoverable cannot start a new run
- abort/steer/approval/denial/recovery work
- malformed/truncated/nonterminal EOF become errors
- terminal outcome names verdict, tool, and execution result
- policy-free rerun uses the same scenario with empty units
- full composer drawer preserves all scenarios and units
- desktop/mobile/focus/live-region/reduced-motion/no-overflow behavior

- [ ] **Step 4: Request whole-branch review**

Use `superpowers:requesting-code-review` with base `045f3bc`, current HEAD, the approved spec, this plan, complete diff, test output, and screenshot evidence. Fix every Critical/Important finding through the task fix loop.

- [ ] **Step 5: Re-run verification after review fixes**

```bash
/Users/dongkseo/project/nexora-console/.venv/bin/pytest -q
node --test tests/test_stream.mjs
node --check src/console/static/app.js
git diff --check
git status --short
```

Expected: the same clean result as Step 1 and no unexpected worktree changes.
