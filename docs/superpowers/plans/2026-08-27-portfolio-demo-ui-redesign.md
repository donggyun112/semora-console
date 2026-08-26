# Portfolio Demo UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Replace the console-first entry screen with a concrete, one-click leak scenario while preserving the full policy composer and every existing run control.

**Architecture:** Add a small DOM-free view-state module for the default preset and mode transitions. Keep the current NDJSON reader and reducer unchanged, reorganize the HTML into representative-demo and composer panels around one shared run panel, and let app.js bind both views to the existing run state.

**Tech Stack:** FastAPI static files, vanilla HTML/CSS/ES modules, Node assert tests, pytest/TestClient.

**Spec:** docs/superpowers/specs/2026-08-27-portfolio-demo-ui-design.md

## Global Constraints

- Primary audience: software engineers, technical hiring managers, and reviewers evaluating the project as an engineering portfolio piece.
- No marketing landing page or hero section.
- No new control-plane units, scenarios, tools, or backend execution semantics.
- No Supabase authentication, quotas, deployment configuration, or recorded replay in this change.
- No fabricated run output before the user starts a run.
- Default scenario is leak with approval and dlp_block selected.
- The application must not start a run automatically.
- Keep the existing dark control-room palette and verdict colors.
- Reserve monospace for identifiers, hooks, verdicts, and payloads.
- Do not rely on color alone; every verdict keeps a text label.
- Only one run can be active in the browser.
- Existing Python and Node tests must remain green.

---

## File structure

- Create src/console/static/view-state.mjs: pure default-preset and mode-transition functions.
- Modify src/console/static/index.html: semantic two-mode shell around the shared run panel.
- Modify src/console/static/app.js: render the representative scenario, synchronize both views, and preserve current streaming actions.
- Modify src/console/static/styles.css: two-column demo layout, advanced composer layout, responsive states, focus states, and reduced motion.
- Modify tests/test_stream.mjs: unit coverage for view-state behavior alongside the current reducer and NDJSON checks.
- Modify tests/test_server.py: static-shell and stylesheet contract checks.

### Task 1: Pure representative-view state

**Files:**
- Create: src/console/static/view-state.mjs
- Modify: tests/test_stream.mjs:1-4 and append new assertions

**Interfaces:**
- Produces: DEFAULT_DEMO with scenarioId and unitNames.
- Produces: createViewState() returning a new { mode, scenarioId, unitNames } object.
- Produces: switchMode(viewState, mode) returning a copied state.
- Produces: rerunWithoutPolicies(viewState) returning a copied state with an empty unitNames array.
- Consumes: no browser or application globals.

- [ ] **Step 1: Write the failing view-state tests**

Add the import below near the existing imports:

~~~javascript
import {
  DEFAULT_DEMO,
  createViewState,
  rerunWithoutPolicies,
  switchMode,
} from "../src/console/static/view-state.mjs";
~~~

Append these assertions before the final console.log:

~~~javascript
const initialView = createViewState();
assert.deepEqual(initialView, {
  mode: "demo",
  scenarioId: "leak",
  unitNames: ["approval", "dlp_block"],
});
assert.notStrictEqual(
  createViewState().unitNames,
  createViewState().unitNames,
  "each UI state owns its unit array",
);
assert.deepEqual(DEFAULT_DEMO, {
  scenarioId: "leak",
  unitNames: ["approval", "dlp_block"],
});

const composerView = switchMode(initialView, "composer");
assert.equal(composerView.mode, "composer");
assert.equal(initialView.mode, "demo", "mode transition does not mutate the input");
assert.throws(() => switchMode(initialView, "unknown"), /unknown mode/);

const plainView = rerunWithoutPolicies(composerView);
assert.equal(plainView.scenarioId, "leak");
assert.deepEqual(plainView.unitNames, []);
assert.deepEqual(composerView.unitNames, ["approval", "dlp_block"]);
~~~

- [ ] **Step 2: Run the Node test and verify the missing module failure**

Run:

~~~bash
node --test tests/test_stream.mjs
~~~

Expected: FAIL with ERR_MODULE_NOT_FOUND for view-state.mjs.

- [ ] **Step 3: Create the pure state module**

Create src/console/static/view-state.mjs with:

~~~javascript
export const DEFAULT_DEMO = Object.freeze({
  scenarioId: "leak",
  unitNames: Object.freeze(["approval", "dlp_block"]),
});

const MODES = new Set(["demo", "composer"]);

export function createViewState() {
  return {
    mode: "demo",
    scenarioId: DEFAULT_DEMO.scenarioId,
    unitNames: [...DEFAULT_DEMO.unitNames],
  };
}

export function switchMode(viewState, mode) {
  if (!MODES.has(mode)) throw new Error("unknown mode: " + mode);
  return { ...viewState, mode, unitNames: [...viewState.unitNames] };
}

export function rerunWithoutPolicies(viewState) {
  return { ...viewState, unitNames: [] };
}
~~~

- [ ] **Step 4: Run the Node test and verify it passes**

Run:

~~~bash
node --test tests/test_stream.mjs
~~~

Expected: PASS and output includes stream reducer ok.

- [ ] **Step 5: Commit the pure state boundary**

~~~bash
git add src/console/static/view-state.mjs tests/test_stream.mjs
git commit -m "test(ui): define representative demo state"
~~~

### Task 2: Semantic two-mode HTML shell

**Files:**
- Modify: tests/test_server.py: after test_units_endpoint_shape
- Modify: src/console/static/index.html:1-94
- Modify: src/console/static/styles.css: add the global hidden utility only

**Interfaces:**
- Produces: mode-demo and mode-composer buttons with data-mode values.
- Produces: demo-panel and composer-panel view containers.
- Produces: demo-title, demo-does, demo-risk, demo-prompt, demo-policies, demo-run, rerun-plain, and open-composer elements.
- Preserves: scenarios, units, compose-summary, run, stream, status, abort, steer, recovery, and approval IDs used by the current app.js.
- Consumes: current API-rendered scenarios and units.

- [ ] **Step 1: Add a failing static-shell contract test**

Add this test after test_units_endpoint_shape:

~~~python
def test_static_shell_has_demo_composer_and_accessible_run_regions():
    with TestClient(app) as c:
        html = c.get("/").text

    for element_id in [
        "mode-demo",
        "mode-composer",
        "demo-panel",
        "composer-panel",
        "demo-title",
        "demo-prompt",
        "demo-policies",
        "demo-run",
        "rerun-plain",
        "open-composer",
        "stream",
        "status",
        "run-error",
    ]:
        assert f'id="{element_id}"' in html

    assert 'aria-live="polite"' in html
    assert 'role="log"' in html
    assert 'for="steer-text"' in html
~~~

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

~~~bash
uv run pytest -q tests/test_server.py::test_static_shell_has_demo_composer_and_accessible_run_regions
~~~

Expected: FAIL because mode-demo is absent.

- [ ] **Step 3: Replace index.html with the semantic shell**

Keep the existing head and font import, update asset versions to v=17, and replace the body with this exact structure:

~~~html
<body>
  <div class="grid-noise" aria-hidden="true"></div>

  <header class="topbar">
    <div class="brand">
      <span class="mark" aria-hidden="true">N</span>
      <b>NEXORA</b><span class="sep">/</span><span class="sub">CONTROL PLANE</span>
    </div>
    <div class="model" id="model">—</div>
  </header>

  <main class="app-shell">
    <nav class="mode-tabs" aria-label="화면 선택">
      <button id="mode-demo" class="mode-tab" data-mode="demo" aria-pressed="false">
        대표 데모
      </button>
      <button id="mode-composer" class="mode-tab active" data-mode="composer" aria-pressed="true">
        정책 직접 선택
      </button>
    </nav>

    <div id="boot-error" class="boot-error hidden" role="alert">
      <span id="boot-error-message">화면을 불러오지 못했습니다.</span>
      <button id="boot-retry" type="button">다시 시도</button>
    </div>

    <div id="workspace" class="workspace mode-composer">
      <section id="demo-panel" class="demo-panel hidden" aria-labelledby="demo-title">
        <div id="demo-index" class="scenario-index">SCENARIO 03 / 09</div>
        <h1 id="demo-title">기밀 외부 유출</h1>
        <p id="demo-does"></p>
        <p id="demo-risk" class="demo-risk"></p>

        <div class="field-label">PROMPT</div>
        <div class="locked-prompt">
          <span>고정된 지시</span>
          <code id="demo-prompt"></code>
        </div>

        <div class="field-label">POLICIES</div>
        <div id="demo-policies" class="demo-policies"></div>

        <button id="demo-run" class="run" data-run type="button">실행</button>
        <button id="rerun-plain" class="secondary-action hidden" type="button">
          정책 끄고 다시 실행
        </button>
        <button id="open-composer" class="text-action" type="button">
          정책 직접 선택
        </button>
      </section>

      <section id="composer-panel" class="composer-panel" aria-label="정책 직접 선택">
        <section class="col col-task" aria-labelledby="task-label">
          <h2 id="task-label" class="label"><span class="idx">01</span> 작업</h2>
          <div id="scenarios" class="scenarios"></div>
        </section>

        <section class="col col-policy" aria-labelledby="policy-label">
          <h2 id="policy-label" class="label"><span class="idx">02</span> 정책</h2>
          <div id="units" class="units"></div>
          <div class="compose">
            <div class="compose-label">조립</div>
            <code id="compose-summary">ControlPlane() · 컨트롤 없음</code>
          </div>
          <button id="run" class="run" data-run type="button" disabled>실행</button>
        </section>
      </section>

      <section class="col col-run" aria-labelledby="run-label">
        <div class="run-head">
          <h2 id="run-label" class="label"><span class="idx">03</span> 실행 로그</h2>
          <div class="run-actions">
            <button id="abort" class="abort hidden" type="button">중단</button>
            <span id="status" class="status idle" aria-live="polite">대기</span>
          </div>
        </div>

        <div id="policy-strip" class="policy-strip hidden"></div>
        <div id="run-error" class="sr-only" aria-live="assertive"></div>
        <div
          id="stream"
          class="stream"
          role="log"
          aria-live="polite"
          aria-relevant="additions text"
        >
          <div class="empty">
            <div class="empty-mark" aria-hidden="true">⌁</div>
            <p>실행 로그가 여기에 표시됩니다.</p>
          </div>
        </div>

        <div id="steer-box" class="steer-box hidden">
          <label class="steer-label" for="steer-text">지시 큐</label>
          <div id="steer-queue" class="steer-queue"></div>
          <form id="steer-form" class="steer-form">
            <input
              id="steer-text"
              maxlength="200"
              placeholder="실행 중인 에이전트에 지시"
              autocomplete="off"
            />
            <button type="submit">보내기</button>
          </form>
        </div>

        <div id="recovery" class="recovery hidden" tabindex="-1">
          <div class="recovery-body">
            <span class="pulse crash" aria-hidden="true"></span>
            <div>
              <strong>워커 장애</strong>
              <small><span id="recovery-meta"></span></small>
            </div>
          </div>
          <button id="recover" class="btn crash" type="button">복원</button>
        </div>

        <div id="approval" class="approval hidden" tabindex="-1">
          <div class="approval-body">
            <span class="pulse" aria-hidden="true"></span>
            <div>
              <strong>일시중지 — 승인 대기</strong>
              <small><span id="approval-meta"></span></small>
            </div>
          </div>
          <div class="approval-actions">
            <button id="deny" class="btn ghost" type="button">거부</button>
            <button id="approve" class="btn primary" type="button">승인하고 재개</button>
          </div>
        </div>
      </section>
    </div>
  </main>

  <script type="module" src="/app.js?v=17"></script>
</body>
~~~

The composer panel is intentionally the initial HTML fallback. Task 3 changes it to representative mode after successful API loading, so a JavaScript failure leaves the existing controls visible instead of an inert demo panel.

- [ ] **Step 4: Add one global hidden utility**

Add near the top of styles.css:

~~~css
.hidden { display: none !important; }
~~~

Leave all existing component-specific hidden rules in place during this task.

- [ ] **Step 5: Run the static-shell and full server tests**

Run:

~~~bash
uv run pytest -q tests/test_server.py
~~~

Expected: all server tests pass.

- [ ] **Step 6: Commit the semantic shell**

~~~bash
git add src/console/static/index.html src/console/static/styles.css tests/test_server.py
git commit -m "feat(ui): add representative demo shell"
~~~

### Task 3: Bind representative mode to the existing live run

**Files:**
- Modify: src/console/static/app.js:1-306
- Modify: tests/test_stream.mjs: view-state assertions when interface names change during implementation

**Interfaces:**
- Consumes: createViewState(), switchMode(), and rerunWithoutPolicies() from view-state.mjs.
- Consumes: existing reduceFrames() and createNdjsonReader().
- Produces: setMode(mode), renderDemo(), syncSelectionUi(), resetRunView(), runWithoutPolicies(), showBootError(error).
- Preserves: run(), stream(), enqueueSteer(), abortRun(), recover(), and resume() endpoint payloads.

- [ ] **Step 1: Update the imports and application state**

Replace the imports and state declaration at the top of app.js with:

~~~javascript
import { reduceFrames } from "./reducer.mjs?v=15";
import { createNdjsonReader } from "./ndjson.mjs?v=15";
import {
  createViewState,
  rerunWithoutPolicies,
  switchMode as nextMode,
} from "./view-state.mjs?v=17";

const $ = (id) => document.getElementById(id);
const initial = createViewState();
const state = {
  mode: initial.mode,
  scenario: initial.scenarioId,
  units: new Set(initial.unitNames),
  scenarios: [],
  frames: [],
  runId: null,
  busy: false,
  rowEls: [],
  abortCtl: null,
  steers: [],
};
const meta = {};
~~~

- [ ] **Step 2: Add exact view synchronization helpers after mk()**

~~~javascript
function currentViewState() {
  return {
    mode: state.mode,
    scenarioId: state.scenario,
    unitNames: [...state.units],
  };
}

function setMode(mode) {
  const next = nextMode(currentViewState(), mode);
  state.mode = next.mode;
  $("workspace").className = "workspace mode-" + mode;
  $("demo-panel").classList.toggle("hidden", mode !== "demo");
  $("composer-panel").classList.toggle("hidden", mode !== "composer");
  document.querySelectorAll("[data-mode]").forEach((button) => {
    const active = button.dataset.mode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function syncSelectionUi() {
  document.querySelectorAll("[data-scenario]").forEach((button) => {
    const active = button.dataset.scenario === state.scenario;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  document.querySelectorAll("[data-unit]").forEach((button) => {
    const active = state.units.has(button.dataset.unit);
    button.classList.toggle("on", active);
    button.setAttribute("aria-pressed", String(active));
  });
  updateCompose();
  renderDemo();
  syncRun();
}

function renderDemo() {
  const scenario = state.scenarios.find((item) => item.id === state.scenario);
  if (!scenario) return;
  const index = state.scenarios.indexOf(scenario) + 1;
  $("demo-index").textContent =
    "SCENARIO " + String(index).padStart(2, "0") +
    " / " + String(state.scenarios.length).padStart(2, "0");
  $("demo-title").textContent = scenario.title;
  $("demo-does").textContent = scenario.does;
  $("demo-risk").textContent = scenario.risk.includes("없음")
    ? scenario.risk
    : "위험 · " + scenario.risk;
  $("demo-prompt").textContent = scenario.prompt;

  const chips = [...state.units].map((name) => {
    const unit = meta[name];
    const chip = mk(
      "span",
      "demo-policy v-" + unit.verdict.toLowerCase(),
      name,
    );
    chip.append(mk("small", "", unit.verdict.toUpperCase()));
    return chip;
  });
  $("demo-policies").replaceChildren(...chips);
}

function showBootError(error) {
  $("workspace").classList.add("hidden");
  $("boot-error-message").textContent =
    "화면을 불러오지 못했습니다. " + String(error);
  $("boot-error").classList.remove("hidden");
  $("boot-retry").onclick = () => window.location.reload();
}

function resetRunView() {
  state.frames = [];
  state.rowEls = [];
  state.runId = null;
  state.steers = [];
  renderSteerQueue();
  const empty = mk("div", "empty");
  const mark = mk("div", "empty-mark", "⌁");
  mark.setAttribute("aria-hidden", "true");
  empty.append(mark, mk("p", "", "실행 로그가 여기에 표시됩니다."));
  $("stream").replaceChildren(empty);
  $("policy-strip").classList.add("hidden");
  $("approval").classList.add("hidden");
  $("recovery").classList.add("hidden");
}
~~~


- [ ] **Step 3: Replace boot() with guarded loading and event binding**

~~~javascript
async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error("HTTP " + response.status);
  return response.json();
}

async function boot() {
  try {
    const [scenarios, unitsBody] = await Promise.all([
      fetchJson("/api/scenarios"),
      fetchJson("/api/units"),
    ]);
    state.scenarios = scenarios;
    $("model").textContent = unitsBody.model;
    unitsBody.units.forEach((unit) => (meta[unit.name] = unit));

    renderScenarios(scenarios);
    renderUnits(unitsBody.units);
    syncSelectionUi();
    setMode("demo");

    document.querySelectorAll("[data-run]").forEach((button) => {
      button.addEventListener("click", run);
    });
    document.querySelectorAll("[data-mode]").forEach((button) => {
      button.addEventListener("click", () => setMode(button.dataset.mode));
    });
    $("open-composer").addEventListener("click", () => setMode("composer"));
    $("rerun-plain").addEventListener("click", runWithoutPolicies);
    $("abort").addEventListener("click", abortRun);
    $("steer-form").addEventListener("submit", enqueueSteer);
    $("recover").addEventListener("click", recover);
    $("approve").addEventListener("click", () => resume(true));
    $("deny").addEventListener("click", () => resume(false));
  } catch (error) {
    showBootError(error);
  }
}
~~~

- [ ] **Step 4: Make scenario and unit buttons accessible and synchronized**

In renderScenarios(), set data and aria attributes immediately after creating each button:

~~~javascript
el.dataset.scenario = s.id;
el.type = "button";
el.setAttribute("aria-pressed", "false");
~~~

Replace its click handler with:

~~~javascript
el.addEventListener("click", () => {
  state.scenario = s.id;
  syncSelectionUi();
});
~~~

In renderUnits(), set these attributes after creating each unit button:

~~~javascript
el.dataset.unit = u.name;
el.type = "button";
el.setAttribute("aria-pressed", "false");
~~~

Replace its click handler with:

~~~javascript
el.addEventListener("click", () => {
  if (state.units.has(u.name)) state.units.delete(u.name);
  else state.units.add(u.name);
  syncSelectionUi();
});
~~~

- [ ] **Step 5: Synchronize both run buttons and add the plain rerun**

Replace syncRun() with:

~~~javascript
function syncRun() {
  document.querySelectorAll("[data-run]").forEach((button) => {
    button.disabled = !state.scenario || state.busy;
  });
  $("abort").classList.toggle("hidden", !state.busy);
  $("steer-box").classList.toggle("hidden", !state.busy);
}
~~~

Add:

~~~javascript
async function runWithoutPolicies() {
  const next = rerunWithoutPolicies(currentViewState());
  state.units = new Set(next.unitNames);
  syncSelectionUi();
  await run();
}
~~~

Replace the state-reset block at the beginning of run() with resetRunView(). After stream() completes, reveal the rerun action only when at least one policy was used:

~~~javascript
const usedPolicies = state.units.size > 0;
await stream("/api/run", {
  scenario_id: state.scenario,
  units: [...state.units],
});
$("rerun-plain").classList.toggle("hidden", !usedPolicies);
~~~

- [ ] **Step 6: Harden streaming responses and focus transient controls**

Immediately after fetch() returns in stream(), add:

~~~javascript
if (!res.ok || !res.body) {
  throw new Error("실행 요청 실패 · HTTP " + res.status);
}
~~~

In handleFrame(), after revealing approval and recovery panels, move focus:

~~~javascript
$("approval").classList.remove("hidden");
$("approval").focus();
~~~

~~~javascript
$("recovery").classList.remove("hidden");
$("recovery").focus();
~~~

Replace the one-line error branch in handleFrame() with:

~~~javascript
} else if (f.kind === "error") {
  setStatus("error", "오류");
  $("run-error").textContent =
    f.message || "실행 중 오류가 발생했습니다.";
} else if (f.kind === "policy_summary") {
  renderPolicyStrip(f.units);
}
~~~

This keeps the visible reducer row and announces the same message through the assertive live region.

- [ ] **Step 7: Run all JavaScript and Python tests**

Run:

~~~bash
node --test tests/test_stream.mjs
uv run pytest -q
~~~

Expected: Node test passes; pytest passes with the existing Postgres-dependent skip allowed.

- [ ] **Step 8: Commit live demo integration**

~~~bash
git add src/console/static/app.js tests/test_stream.mjs
git commit -m "feat(ui): wire representative live demo"
~~~

### Task 4: Responsive visual hierarchy and interaction states

**Files:**
- Modify: tests/test_server.py: add stylesheet contract test
- Modify: src/console/static/styles.css:1-280
- Verify: src/console/static/index.html and src/console/static/app.js in a live browser

**Interfaces:**
- Consumes: workspace mode-demo/mode-composer classes and all classes created in Task 2.
- Produces: two-column demo layout, nested composer layout, mobile stacking, visible focus, reduced motion, and non-color verdict labels.
- Preserves: existing row, status, policy-strip, steer, approval, and recovery verdict classes.

- [ ] **Step 1: Add a failing stylesheet contract test**

Add after the static-shell test:

~~~python
def test_stylesheet_has_responsive_focus_and_reduced_motion_contracts():
    with TestClient(app) as c:
        css = c.get("/styles.css").text

    assert ".workspace.mode-demo" in css
    assert ".composer-panel" in css
    assert ":focus-visible" in css
    assert "@media (max-width: 820px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
~~~

- [ ] **Step 2: Run the focused stylesheet test and verify it fails**

Run:

~~~bash
uv run pytest -q tests/test_server.py::test_stylesheet_has_responsive_focus_and_reduced_motion_contracts
~~~

Expected: FAIL because workspace.mode-demo is absent.

- [ ] **Step 3: Replace the current layout/header rules with the new shell rules**

Retain the existing root variables, reset, verdict colors, stream rows, policy strip, steer, approval, and recovery rules. Replace the header and layout sections and add these selectors:

~~~css
.topbar {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  min-height: 58px;
  padding: 0 22px;
  border-bottom: 1px solid var(--line);
  background: rgba(10, 12, 17, 0.82);
  backdrop-filter: blur(8px);
}

.app-shell {
  position: relative;
  z-index: 1;
  max-width: 1240px;
  margin: 0 auto;
  padding: 18px 22px 24px;
}

.mode-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 14px;
}

.mode-tab {
  min-height: 34px;
  padding: 0 12px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--muted);
  font: 600 12px var(--kr);
  cursor: pointer;
}

.mode-tab.active {
  border-color: var(--line-2);
  background: var(--panel);
  color: var(--text);
}

.workspace {
  display: grid;
  gap: 16px;
  align-items: stretch;
}

.workspace.mode-demo {
  grid-template-columns: minmax(340px, 0.78fr) minmax(0, 1.22fr);
}

.workspace.mode-composer {
  grid-template-columns: minmax(560px, 1.08fr) minmax(420px, 0.92fr);
}

.demo-panel {
  min-width: 0;
  padding: 26px;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--bg-2);
}

.scenario-index,
.field-label {
  color: var(--dim);
  font: 10px var(--mono);
  letter-spacing: 0.12em;
}

.demo-panel h1 {
  margin: 10px 0 8px;
  font-size: clamp(26px, 3vw, 34px);
  line-height: 1.15;
}

#demo-does {
  margin: 0;
  color: var(--muted);
  font-size: 14px;
  line-height: 1.6;
}

.demo-risk {
  margin: 6px 0 24px;
  color: var(--deny);
  font: 11px var(--mono);
}

.locked-prompt {
  margin: 7px 0 20px;
  padding: 12px 13px;
  border: 1px solid var(--line);
  border-left: 2px solid var(--accent);
  border-radius: 9px;
  background: #0b0e14;
}

.locked-prompt span {
  display: block;
  margin-bottom: 6px;
  color: var(--dim);
  font: 9px var(--mono);
  letter-spacing: 0.1em;
}

.locked-prompt code {
  color: #c3ccdd;
  font: 12px/1.6 var(--kr);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.demo-policies {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  min-height: 28px;
  margin: 8px 0 22px;
}

.demo-policy {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 6px 9px;
  border: 1px solid currentColor;
  border-radius: 6px;
  font: 600 10px var(--mono);
}

.demo-policy small {
  opacity: 0.75;
  font-size: 8px;
}

.secondary-action,
.text-action {
  width: 100%;
  margin-top: 8px;
  cursor: pointer;
  font-family: var(--kr);
}

.secondary-action {
  min-height: 40px;
  border: 1px solid var(--line-2);
  border-radius: 9px;
  background: transparent;
  color: var(--text);
}

.text-action {
  min-height: 34px;
  border: 0;
  background: transparent;
  color: var(--muted);
  font-size: 12px;
}

.composer-panel {
  display: grid;
  grid-template-columns: minmax(240px, 0.9fr) minmax(280px, 1.1fr);
  gap: 16px;
  min-width: 0;
}

.col-run {
  min-height: 620px;
}

.boot-error {
  margin-bottom: 14px;
  padding: 13px 15px;
  border: 1px solid #6b2f34;
  border-radius: 10px;
  background: #1e1013;
  color: #ffb3b8;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.boot-error button {
  margin-left: 12px;
  border: 0;
  background: transparent;
  color: inherit;
  text-decoration: underline;
  cursor: pointer;
}
~~~

- [ ] **Step 4: Add interaction, mobile, and reduced-motion rules**

~~~css
button:focus-visible,
input:focus-visible,
[tabindex="-1"]:focus-visible {
  outline: 2px solid #9bbcff;
  outline-offset: 3px;
}

@media (max-width: 980px) {
  .workspace.mode-composer {
    grid-template-columns: 1fr;
  }

  .composer-panel {
    grid-template-columns: minmax(240px, 0.9fr) minmax(280px, 1.1fr);
  }
}

@media (max-width: 820px) {
  .topbar {
    min-height: 54px;
    padding: 0 15px;
  }

  .brand .sub,
  .model {
    display: none;
  }

  .app-shell {
    padding: 14px;
  }

  .workspace.mode-demo,
  .workspace.mode-composer,
  .composer-panel {
    grid-template-columns: 1fr;
  }

  .demo-panel {
    padding: 20px;
  }

  .col-run {
    min-height: 520px;
  }

  .approval,
  .recovery {
    align-items: stretch;
    flex-direction: column;
  }

  .approval-actions {
    width: 100%;
  }

  .approval-actions .btn,
  .recovery .btn {
    flex: 1;
  }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
~~~

Remove the old main three-column rule and its max-width 980 px single-column override so it cannot fight the new workspace rules.

- [ ] **Step 5: Run automated tests**

Run:

~~~bash
uv run pytest -q
node --test tests/test_stream.mjs
~~~

Expected: all Python tests pass with the existing Postgres-dependent skip allowed; Node test passes.

- [ ] **Step 6: Run the local server and perform the manual acceptance pass**

Run:

~~~bash
uv run uvicorn console.server:app --port 8850
~~~

Verify in the browser:

1. First load shows leak with approval and dlp_block selected.
2. The run panel says “실행 로그가 여기에 표시됩니다.” and contains no fabricated rows.
3. One click starts the live run.
4. The stream shows textual SUSPEND and DENY verdicts when emitted.
5. The post-run action says “정책 끄고 다시 실행.”
6. Composer mode exposes all scenarios and units.
7. Mode switching does not clear the current run.
8. Keyboard focus reaches both mode tabs, scenario buttons, unit buttons, run, abort, steer, approval, and recovery controls.
9. At 390 px width no prompt or code identifier causes horizontal page scrolling.
10. Reduced-motion mode removes row and pulse animation.

- [ ] **Step 7: Commit the visual redesign**

~~~bash
git add src/console/static/styles.css src/console/static/index.html src/console/static/app.js tests/test_server.py
git commit -m "feat(ui): focus console on representative run"
~~~

### Task 5: Final verification and scope audit

**Files:**
- Verify: docs/superpowers/specs/2026-08-27-portfolio-demo-ui-design.md
- Verify: all files changed in Tasks 1-4
- Do not modify backend policy or endpoint files unless a failing pre-existing test proves an unrelated defect.

**Interfaces:**
- Consumes: the complete implementation.
- Produces: test and diff evidence suitable for completion review.

- [ ] **Step 1: Run the complete automated suite from a clean process**

~~~bash
uv run pytest -q
node --test tests/test_stream.mjs
~~~

Expected: all Python tests pass with only the existing Postgres-dependent skip; Node test passes.

- [ ] **Step 2: Run source-level sanity checks**

~~~bash
git diff --check
git status --short
~~~

Expected: git diff --check prints nothing. Status contains no accidental .superpowers/ files staged for commit.

- [ ] **Step 3: Compare the implementation against every acceptance criterion**

Confirm explicitly:

- default leak preset
- approval and dlp_block selected
- no automatic run
- no fabricated output
- live stream preserved
- rerun without policies
- full composer preserved
- abort, steer, approval, and recovery preserved
- desktop and mobile layout
- keyboard and non-color verdict labels
- concise loading and error states

- [ ] **Step 4: Request code review**

Use superpowers:requesting-code-review with the design spec, this plan, the final diff, and test output. Address only findings that are in scope for this redesign.

- [ ] **Step 5: Run verification again after review fixes**

~~~bash
uv run pytest -q
node --test tests/test_stream.mjs
git diff --check
~~~

Expected: the same clean result as Step 1, plus no whitespace errors.
