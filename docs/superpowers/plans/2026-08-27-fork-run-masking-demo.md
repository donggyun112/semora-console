# Fork Run Masking Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a real two-step masking-incident demo that creates a masked source branch, invokes `nexora_fork.fork_run` once without the input masker, and shows the preserved source branch beside the new active branch.

**Architecture:** Keep general run streaming in `server.py`, isolate source/fork orchestration and branch snapshot projection in a new `fork_demo.py`, and represent branch snapshots as lifecycle frames consumed by pure frontend reducers. Add one `on_inputs` control unit for source masking; the fork endpoint reuses the source session while removing only that unit.

**Tech Stack:** Python 3.12+, FastAPI, Nexora `AgentRuntime`, `nexora-fork`, Nexora memory/Postgres stores, vanilla ES modules, Node test runner, pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-fork-run-masking-demo-design.md`

## Global Constraints

- Work directly on `main`; the user explicitly requested integration on `main`.
- Preserve all pre-existing unstaged changes in `README.md`, `src/console/server.py`, `src/console/store.py`, `tests/test_durable.py`, `tests/test_provider_store.py`, and `tests/test_server.py`.
- Stage only task-owned files or task-owned hunks.
- Scenario prompts remain locked; no free-text proxy is introduced.
- Source branch metadata must contain the masked value, never the source-ledger original.
- Fork branch transcript and `context_injected` may contain the original by design; the UI must display the durable-data warning.
- Trace summaries never copy raw lifecycle payload content.
- Every production behavior follows a witnessed RED → GREEN cycle.

---

### Task 1: Project policy denial at the decision boundary

**Files:**
- Modify: `src/console/server.py:167-268`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: Nexora `permission_denied` payload `{call_id, name, reason, source}`.
- Produces: `_project_event(event, pending, projected_denials=None)` and `_stream` order `unit → lifecycle → blocked tool`.

- [x] **Step 1: Write the failing stream-order test**

```python
@pytest.mark.asyncio
async def test_denial_projects_policy_before_permission_lifecycle_once():
    async def attempt(runtime, on_event):
        call = {"type": "tool_call", "id": "call-send", "name": "send_email", "input": {}}
        await on_event(call)
        denied = {"type": "error", "unit": "dlp_block", "message": "거부"}
        await runtime.events.publish(
            EventType.PERMISSION_DENIED,
            call_id="call-send",
            name="send_email",
            reason=denied,
            source="pre_tool_use",
        )
        await on_event({**call, "type": "tool_result", "executed": False, "result": denied})
        return {"stop_reason": "completed"}

    # Collect the real `_stream` and assert one unit before one permission lifecycle frame.
```

- [x] **Step 2: Run RED**

Run: `.venv/bin/pytest -q tests/test_server.py::test_denial_projects_policy_before_permission_lifecycle_once`

Observed failure: actual order was `permission_denied`, then `dlp_block`.

- [x] **Step 3: Project and deduplicate at `publish`**

```python
projected_denials: set[str] = set()

if str(event_type).endswith("permission_denied"):
    reason = payload.get("reason") or {}
    call_id = str(payload.get("call_id") or "")
    unit = reason.get("unit", "control") if isinstance(reason, dict) else "control"
    projected_denials.add(call_id)
    await queue.put({"kind": "unit", "unit": unit, "verdict": "deny", "message": message})

# `_project_event` consumes `projected_denials` so the later tool_result emits only blocked tool.
```

- [x] **Step 4: Run GREEN and baseline suite**

Run: `.venv/bin/pytest -q tests/test_server.py::test_denial_projects_policy_before_permission_lifecycle_once`

Observed: `1 passed`; baseline after the change: `50 passed, 1 skipped` plus the Node stream test.

- [ ] **Step 5: Commit only the event-order hunks**

```bash
git add -p src/console/server.py tests/test_server.py
git diff --cached --check
git commit -m "fix(events): order policy before permission denial"
```

### Task 2: Add the input masking unit, scenario, and fork dependency

**Files:**
- Modify: `src/console/units.py:17-299`
- Modify: `src/console/scenarios.py:5-79`
- Modify: `tests/test_units.py`
- Modify: `tests/test_scenarios.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: `list[PendingInput]` at Nexora `on_inputs`.
- Produces: `input_mask(ctx, inputs) -> list[PendingInput]`, registry unit `input_mask`, scenario `fork_masking` with `default_units`.

- [ ] **Step 1: Write failing input-mask and scenario tests**

```python
@pytest.mark.asyncio
async def test_input_mask_rewrites_content_but_preserves_origin():
    plane = compose_controls(["input_mask"])
    incoming = [PendingInput("user_prompt", HumanMessage("ssn is 123-45"), "p2")]
    screened = await plane.on_inputs(Ctx(turn=0), incoming)
    assert screened[0].message.content == "ssn is ***"
    assert screened[0].origin_id == "p2"
    assert screened[0].kind == "user_prompt"


def test_fork_masking_scenario_starts_with_input_mask():
    scenario = next(item for item in SCENARIOS if item["id"] == "fork_masking")
    assert scenario["default_units"] == ["input_mask"]
    assert scenario["forkable"] is True
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/pytest -q tests/test_units.py::test_input_mask_rewrites_content_but_preserves_origin tests/test_scenarios.py::test_fork_masking_scenario_starts_with_input_mask`

Expected: fail because `input_mask` and `fork_masking` do not exist.

- [ ] **Step 3: Implement the masker and scenario**

```python
_INPUT_SSN = re.compile(r"\b\d{3}-\d{2}(?:-\d{4})?\b")

async def input_mask(_ctx: Ctx, inputs: list[PendingInput]) -> list[PendingInput]:
    masked: list[PendingInput] = []
    for item in inputs:
        content = str(item.message.content)
        masked.append(
            PendingInput(
                item.kind,
                HumanMessage(_INPUT_SSN.sub("***", content)),
                item.origin_id,
            )
        )
    return masked

Unit(
    "input_mask", "on_inputs", "Ingress", "Rewrite",
    "입력 개인정보 가리기", "모델에 넣기 전 주민번호를 가림.", input_mask,
)
```

Add the exact `fork_masking` metadata from the spec and append its id to the literal scenario-order assertion.

- [ ] **Step 4: Install the fork extra from the adjacent workspace**

```toml
"nexora[fork,openrouter]"

[tool.uv.sources]
nexora-fork = { path = "../nexora-python/packages/nexora-fork", editable = true }
```

Run: `uv lock --offline`

Run: `uv sync --offline`

Run: `.venv/bin/python -c "from nexora_fork import fork_run; print(fork_run.__name__)"`

Expected: `fork_run`.

- [ ] **Step 5: Run GREEN**

Run: `.venv/bin/pytest -q tests/test_units.py tests/test_scenarios.py`

Expected: all tests pass.

- [ ] **Step 6: Commit the unit, scenario, and dependency**

```bash
git add pyproject.toml uv.lock src/console/units.py src/console/scenarios.py tests/test_units.py tests/test_scenarios.py
git diff --cached --check
git commit -m "feat(fork): add masked input scenario"
```

### Task 3: Run the source conversation and real fork

**Files:**
- Create: `src/console/fork_demo.py`
- Create: `tests/test_fork_demo.py`
- Modify: `src/console/server.py:3-134,216-417`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: `AgentRuntime`, execution store, shared `Agent`, source/fork run ids, conversation id, origin id, controls, `on_event`.
- Produces: `run_masked_source(runtime, *, run_id, prefix_run_id, conversation_id, origin_id, prompt, agent, controls, on_event, should_stop_after_turn, aborted)`, `run_from_original(runtime, store, *, from_run_id, run_id, conversation_id, origin_id, agent, controls, on_event, should_stop_after_turn, aborted)`, `branch_snapshot(branch, run_id, conversation_id, origin_id, messages)`, and `POST /api/fork`.

- [ ] **Step 1: Write a failing real in-memory fork test**

```python
@pytest.mark.asyncio
async def test_fork_restores_original_and_preserves_masked_source():
    steps = MemorySteps()
    transcript = MemoryTranscript()
    snapshots: list[dict] = []

    async def emit(event_type, payload):
        if str(event_type) == "branch_snapshot":
            snapshots.append(payload)

    runtime = AgentRuntime(store=steps, transcript=transcript, emit=emit)
    agent = Agent(
        "fork-demo",
        "fork demo",
        FakeListChatModel(responses=["hello", "masked response", "original response"]),
        DemoTools(),
        SYSTEM_PROMPT,
    )
    await run_masked_source(
        runtime,
        run_id="run-b",
        prefix_run_id="run-a",
        conversation_id="conv",
        origin_id="p2",
        prompt="ssn is 123-45",
        agent=agent,
        controls=compose_controls(["input_mask"]),
    )
    await run_from_original(
        runtime,
        steps,
        from_run_id="run-b",
        run_id="run-c",
        conversation_id="conv",
        origin_id="p2",
        agent=agent,
        controls=None,
    )

    assert "ssn is ***" in [m["content"] for m in snapshots[0]["messages"]]
    assert "ssn is 123-45" in [m["content"] for m in snapshots[1]["messages"]]
    source = next(record for record in await steps.list_inputs("run-b") if record.input_id == "p2")
    assert "123-45" in str(source.value)
    assert [record.input_id for record in await steps.list_inputs("run-c")] == ["p2"]
```

This test fails if the implementation replays the masked transcript copy, mutates the source ledger, loses `origin_id`, or creates a fresh conversation instead of moving its head.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/pytest -q tests/test_fork_demo.py::test_fork_restores_original_and_preserves_masked_source`

Expected: import failure for the not-yet-created `console.fork_demo` module.

- [ ] **Step 3: Implement fork orchestration and safe snapshots**

```python
def branch_snapshot(branch, run_id, conversation_id, origin_id, messages):
    return {
        "branch": branch,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "origin_id": origin_id,
        "active": True,
        "messages": [
            {
                "id": str(message.id or ""),
                "role": "user" if message.type == "human" else "assistant",
                "content": str(message.content),
            }
            for message in messages
            if message.type in {"human", "ai"}
        ],
    }

async def run_masked_source(
    runtime,
    *,
    run_id,
    prefix_run_id,
    conversation_id,
    origin_id,
    prompt,
    agent,
    controls,
    on_event=None,
    should_stop_after_turn=None,
    aborted=None,
):
    await runtime.run(
        prefix_run_id,
        agent,
        prompt="대화를 시작하고 hello라고 답해줘.",
        conversation_id=conversation_id,
        should_stop_after_turn=should_stop_after_turn,
        aborted=aborted,
    )
    outcome = await runtime.run(
        run_id,
        agent,
        prompt=prompt,
        prompt_id=origin_id,
        controls=controls,
        conversation_id=conversation_id,
        on_event=on_event,
        should_stop_after_turn=should_stop_after_turn,
        aborted=aborted,
    )
    history = await runtime.committed_history(run_id, conversation_id)
    snapshot = branch_snapshot("source", run_id, conversation_id, origin_id, history)
    await runtime.events.publish("branch_snapshot", **snapshot)
    return outcome

async def run_from_original(
    runtime,
    store,
    *,
    from_run_id,
    run_id,
    conversation_id,
    origin_id,
    agent,
    controls,
    on_event=None,
    should_stop_after_turn=None,
    aborted=None,
):
    outcome = await fork_run(
        runtime,
        store,
        from_run_id=from_run_id,
        origin_id=origin_id,
        run_id=run_id,
        model=agent,
        controls=controls,
        conversation_id=conversation_id,
        on_event=on_event,
        should_stop_after_turn=should_stop_after_turn,
        aborted=aborted,
    )
    history = await runtime.committed_history(run_id, conversation_id)
    snapshot = branch_snapshot("fork", run_id, conversation_id, origin_id, history)
    await runtime.events.publish("branch_snapshot", **snapshot)
    return outcome
```

- [ ] **Step 4: Run the fork test GREEN**

Run: `.venv/bin/pytest -q tests/test_fork_demo.py`

Expected: all fork-demo tests pass using `MemorySteps` and `MemoryTranscript`.

- [ ] **Step 5: Write failing endpoint-state tests**

```python
def test_fork_rejects_unknown_source():
    with TestClient(app) as client:
        response = client.post("/api/fork", json={"run_id": "missing"})
    assert response.status_code == 404


def test_fork_rejects_nonforkable_and_repeated_source():
    # Seed real session records and assert 409 with no new session on each invalid state.
```

The repeated-source test records the session keys before the request and asserts they remain the same after the rejected request.

- [ ] **Step 6: Run endpoint RED**

Run: `.venv/bin/pytest -q tests/test_server.py -k fork`

Expected: 404/route failure because `/api/fork` is absent.

- [ ] **Step 7: Add `ForkRequest`, source-session fields, and `/api/fork`**

```python
class ForkRequest(BaseModel):
    run_id: str

@app.post("/api/fork")
async def fork(request: ForkRequest) -> StreamingResponse:
    source = _sessions.get(request.run_id)
    if source is None:
        raise HTTPException(status_code=404, detail="unknown run_id")
    if source.get("scenario_id") != "fork_masking" or not source.get("terminal"):
        raise HTTPException(status_code=409, detail="run is not forkable")
    if source.get("forked_to"):
        raise HTTPException(status_code=409, detail="run already forked")

    fork_run_id = f"run-{uuid.uuid4().hex[:12]}"
    fork_units = [name for name in source["units"] if name != "input_mask"]
    source["forked_to"] = fork_run_id
    _sessions[fork_run_id] = {
        "units": fork_units,
        "agent": source["agent"],
        "scenario_id": "fork_masking",
        "aborted": False,
        "crash": False,
        "crash_at": None,
        "conversation_id": source["conversation_id"],
        "origin_id": source["origin_id"],
        "source_run_id": source["source_run_id"],
        "fork_parent": request.run_id,
        "terminal": False,
    }
    return StreamingResponse(_stream(fork_run_id, attempt, ...), media_type="application/x-ndjson")
```

Special-case `fork_masking` in `/api/run` to call `run_masked_source`; all other scenarios retain the current dispatch path. Mark session `terminal=True` only after `_stream` obtains an outcome.

- [ ] **Step 8: Run backend GREEN**

Run: `.venv/bin/pytest -q tests/test_fork_demo.py tests/test_server.py`

Expected: all tests pass.

- [ ] **Step 9: Commit backend fork support**

```bash
git add src/console/fork_demo.py tests/test_fork_demo.py
git add -p src/console/server.py tests/test_server.py
git diff --cached --check
git commit -m "feat(fork): stream source and fork branches"
```

### Task 4: Add the two-step branch UI

**Files:**
- Modify: `src/console/static/run-state.mjs`
- Modify: `src/console/static/app.js`
- Modify: `src/console/static/reducer.mjs`
- Modify: `src/console/static/index.html`
- Modify: `src/console/static/styles.css`
- Modify: `tests/test_stream.mjs`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: `branch_snapshot` lifecycle frames and forkable scenario metadata.
- Produces: `beginFork(state)`, `deriveBranchView(frames)`, `#fork-run`, `#fork-warning`, and branch-group rendering.

- [ ] **Step 1: Write failing pure state and branch-view tests**

```javascript
const forkSourceTerminal = finishRun(
  attachRunId(
    startRun(updateDraft(createRunState(), {
      scenarioId: "fork_masking",
      unitNames: ["input_mask", "dlp_block"],
    })),
    "run-b",
  ),
  "completed",
);
assert.deepEqual(beginFork(forkSourceTerminal).active.unitNames, ["dlp_block"]);
assert.equal(beginFork(forkSourceTerminal).phase, "streaming");
assert.equal(beginFork(forkSourceTerminal).runId, null);

assert.deepEqual(
  deriveBranchView([
    {kind: "lifecycle", type: "branch_snapshot", payload: {
      branch: "source", run_id: "run-b", active: true,
      messages: [{role: "user", content: "ssn is ***"}],
    }},
    {kind: "lifecycle", type: "branch_snapshot", payload: {
      branch: "fork", run_id: "run-c", active: true,
      messages: [{role: "user", content: "ssn is 123-45"}],
    }},
  ]),
  [
    {branch: "source", runId: "run-b", active: false, messages: [{role: "user", content: "ssn is ***"}]},
    {branch: "fork", runId: "run-c", active: true, messages: [{role: "user", content: "ssn is 123-45"}]},
  ],
);
```

- [ ] **Step 2: Run frontend RED**

Run: `node --test tests/test_stream.mjs`

Expected: import failures for `beginFork` and `deriveBranchView`.

- [ ] **Step 3: Implement pure state and branch derivation**

```javascript
export function beginFork(state) {
  if (state.phase !== "terminal" || state.active?.scenarioId !== "fork_masking" || !state.runId) {
    throw new Error("no forkable source run");
  }
  return {
    ...state,
    phase: "streaming",
    active: {
      ...state.active,
      unitNames: state.active.unitNames.filter((name) => name !== "input_mask"),
    },
    runId: null,
    stopReason: null,
    error: null,
  };
}
```

`deriveBranchView` keeps the latest snapshot for each branch and marks only the last snapshot active.

- [ ] **Step 4: Run pure tests GREEN**

Run: `node --test tests/test_stream.mjs`

Expected: stream/state tests pass.

- [ ] **Step 5: Add failing shell-contract assertions**

Extend the real FastAPI shell parser test to require `fork-run` and `fork-warning`. The mutation caught is deleting the action or warning from the served HTML.

Run: `.venv/bin/pytest -q tests/test_server.py::test_static_shell_is_run_inspector_with_contextual_drawers`

Expected: fail because the ids are absent.

- [ ] **Step 6: Implement fork action and branch presentation**

```javascript
async function forkSource() {
  if (state.run.phase !== "terminal" || !state.run.runId) return;
  const sourceRunId = state.run.runId;
  state.run = beginFork(state.run);
  render();
  await stream("/api/fork", { run_id: sourceRunId });
}
```

- Scenario selection applies `scenario.default_units` when present.
- `renderOutcome` shows `#fork-run` only when a source snapshot exists and a fork snapshot does not.
- `renderChat` uses branch groups for `fork_masking`; other scenarios keep their current chat UI.
- The fork group displays `원문이 새 원장과 대화 기록에 남습니다.` before and after execution.
- `lifecycleSummary` summarizes `branch_snapshot` as `source branch` or `fork branch` without message content.
- CSS keeps branch labels and warning readable on desktop and mobile without adding another top-level panel.

- [ ] **Step 7: Run frontend and shell GREEN**

Run: `node --test tests/test_stream.mjs`

Run: `.venv/bin/pytest -q tests/test_server.py -k 'static_shell or stylesheet'`

Run: `node --check src/console/static/app.js`

Expected: all commands pass.

- [ ] **Step 8: Commit the two-step UI**

```bash
git add src/console/static/run-state.mjs src/console/static/app.js src/console/static/reducer.mjs src/console/static/index.html src/console/static/styles.css tests/test_stream.mjs
git add -p tests/test_server.py
git diff --cached --check
git commit -m "feat(ui): inspect source and fork branches"
```

### Task 5: Acceptance, documentation, and final verification

**Files:**
- Modify: `scripts/acceptance.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `/api/run` fork scenario response and `/api/fork` response.
- Produces: live acceptance case proving masked source and original fork; concise operator documentation.

- [ ] **Step 1: Add a live acceptance case**

```python
def c_fork():
    source = stream("/api/run", {"scenario_id": "fork_masking", "units": ["input_mask"]})
    source_meta = next(frame for frame in source if frame["kind"] == "meta")
    source_branch = next(
        frame for frame in source
        if frame["kind"] == "lifecycle" and frame.get("type") == "branch_snapshot"
    )
    fork = stream("/api/fork", {"run_id": source_meta["run_id"]})
    fork_branch = next(
        frame for frame in fork
        if frame["kind"] == "lifecycle" and frame.get("type") == "branch_snapshot"
    )
    source_text = str(source_branch["payload"]["messages"])
    fork_text = str(fork_branch["payload"]["messages"])
    return "***" in source_text and "123-45" not in source_text and "123-45" in fork_text, fork_text
```

- [ ] **Step 2: Document the two-step risk**

Add one README scenario row and one short paragraph: the source ledger keeps the original, the source transcript shows the masked value, and the fork makes the original durable under the fork's controls. Preserve all existing user edits in `README.md`.

- [ ] **Step 3: Run full verification**

Run: `.venv/bin/pytest -q`

Expected: all tests pass with only the existing optional-store skip.

Run: `node --test tests/test_stream.mjs`

Expected: one Node test file passes.

Run: `node --check src/console/static/app.js`

Run: `.venv/bin/python -m compileall -q src`

Run: `git diff --check`

Expected: all commands exit 0.

- [ ] **Step 4: Run offline packaging verification**

Run: `uv lock --check --offline`

Run: `.venv/bin/python -c "from console.server import app; from nexora_fork import fork_run; print(app.title, fork_run.__name__)"`

Expected: `Nexora Control Plane Console fork_run`.

- [ ] **Step 5: Commit acceptance and docs hunks**

```bash
git add scripts/acceptance.py
git add -p README.md
git diff --cached --check
git commit -m "docs: add fork masking acceptance path"
```

- [ ] **Step 6: Audit final scope**

Run: `git status --short`

Confirm only the user's pre-existing unstaged changes remain. Inspect `git log -6 --oneline` and verify each task commit is on `main`.
