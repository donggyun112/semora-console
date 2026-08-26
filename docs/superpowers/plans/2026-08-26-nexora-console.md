# Nexora Control Plane Console — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone FastAPI + vanilla-JS demo where an operator composes nexora control-plane units across lifecycle hooks and watches the same locked task transform by policy alone.

**Architecture:** A `Unit` registry maps names → (control point, composer, verdict, fn). `compose_controls(names)` groups selected units by hook and wraps each with its composer (`Permissions`/`Journal`/`FinishPolicy`) into one `ControlPlane`, passed to `AgentRuntime.run/.resume`. The server streams ndjson frames; the client accumulates text deltas into one bubble per turn and renders verdict-colored unit actions. A `policy_summary` frame reports which toggled-on units fired vs stayed dormant (with reasons).

**Tech Stack:** Python 3.12, nexora (path dep), FastAPI, uvicorn, langchain-openai @ OpenRouter (`deepseek/deepseek-v4-flash-0731`), vanilla JS (no build), pytest, node (for headless JS test).

**Spec:** `/Users/dongkseo/.claude/plans/partitioned-foraging-pumpkin.md`

## Global Constraints

- Project root: `/Users/dongkseo/project/nexora-console`. Package: `console` under `src/console/`.
- **Seam discipline (north star):** every unit's name/description must match what its hook actually reaches. `pii_mask` (after_tool_call) masks the model view + UI stream, NOT the durable ledger — never claim otherwise.
- **Two boundaries never conflated:** `pii_mask` = ingest; `dlp_block` = egress.
- **No silent no-op:** every toggled-on unit either fires visibly or is reported dormant with a reason.
- Locked scenario prompts only — no free-text input (prevents public-link LLM-proxy abuse).
- Model default `deepseek/deepseek-v4-flash-0731`. Turn cap 8.
- Control-plane composer facts (from `packages/nexora/src/nexora/controls.py`): `Permissions` → **Deny wins over Suspend**, Continue never short-circuits; `Journal` runs after_tool_call writers in order (in-place mutation of the result dict flows to model + emitted event); `FinishPolicy` `Proceed(steers)` vetoes → another round, else `Halt` finishes. `ctx.calls_made` lists every tool requested this run (this round's siblings included).
- Each task ends with: tests green + a commit. Never mark a task done on an untested claim.

---

## Current state (starting point)

The backend files already exist as a first scaffold and MUST be treated as unverified until a task's tests pass against them: `pyproject.toml`, `README.md`, `src/console/{__init__,units,tools,scenarios,dormancy,provider,store,server}.py`. `static/` and `tests/` do NOT exist yet. Each task below either verifies-and-hardens an existing file (write its test, run, fix if red, commit) or creates a new one.

---

### Task 1: Project baseline — sync, import, git

**Files:**
- Verify: `pyproject.toml`, `src/console/__init__.py`, `README.md`, `.env` (copied)
- Create: `.env.example`, `.gitignore`

**Interfaces:**
- Produces: an installed `console` package importable via `uv run python -c "import console"`; `uv run` available for all later tasks.

- [ ] **Step 1: Create `.gitignore`**

```
.venv/
__pycache__/
*.pyc
.env
.pytest_cache/
```

- [ ] **Step 2: Create `.env.example`**

```
# Required: OpenRouter key that drives the live agent runs.
OPENROUTER_API_KEY=
# Optional: model id (default deepseek/deepseek-v4-flash-0731).
MODEL=deepseek/deepseek-v4-flash-0731
# Required only for the durable-across-restart proof.
# DATABASE_URL=postgresql://user:pass@host:5432/console
```

- [ ] **Step 3: Sync**

Run: `cd /Users/dongkseo/project/nexora-console && uv sync`
Expected: resolves (nexora path deps), no build error. If README/pyproject issue, fix and re-run.

- [ ] **Step 4: Import smoke test**

Run: `uv run python -c "import console; from console import units, tools, scenarios, dormancy, provider, store, server; print('import ok')"`
Expected: `import ok` (server import may require nothing beyond deps; no key needed to import).

- [ ] **Step 5: Commit**

```bash
git init && git add -A && git commit -m "chore: scaffold nexora-console project baseline"
```

---

### Task 2: Control-plane units + composition (the substance)

**Files:**
- Verify/finalize: `src/console/units.py`
- Test: `tests/test_units.py`

**Interfaces:**
- Consumes: `nexora` (`Continue/Deny/Suspend/Proceed/Halt`, `ControlPlane`, composers, `Ctx`, `ToolCall`).
- Produces: `UNITS: list[Unit]`, `UNITS_BY_NAME: dict[str, Unit]`, `compose_controls(names: list[str]) -> ControlPlane | None`. `Unit` fields: `name, point, composer, verdict, title, desc, fn`. Unit names: `approval, dlp_block, rate_cap, pii_mask, log_gate`. Verdict dicts carry `{"unit": <name>, ...}`; deny/suspend results include `type` and `message`/`reason`; pii_mask sets `result["redacted_by"]="pii_mask"`.

- [ ] **Step 1: Write the failing test** — `tests/test_units.py`

```python
import pytest
from nexora import Continue, Deny, Suspend, Proceed, Halt
from nexora.controls import Ctx
from console.units import compose_controls

def _call(name, **args):
    return {"id": "c1", "name": name, "args": args, "type": "tool_call"}

def _ctx(*names):
    return Ctx(turn=0, calls_made=[{"name": n, "input": {}} for n in names])

@pytest.mark.asyncio
async def test_permissions_deny_wins_over_suspend():
    plane = compose_controls(["approval", "dlp_block"])
    d = await plane.pre_tool_use(_ctx("read_customer", "send_email"), _call("send_email"))
    assert isinstance(d, Deny)

@pytest.mark.asyncio
async def test_approval_suspends_every_effect_but_passes_reads():
    plane = compose_controls(["approval"])
    assert isinstance(await plane.pre_tool_use(_ctx("charge_card"), _call("charge_card")), Suspend)
    assert isinstance(await plane.pre_tool_use(_ctx("remember_note"), _call("remember_note")), Suspend)
    assert isinstance(await plane.pre_tool_use(_ctx("read_customer"), _call("read_customer")), Continue)

@pytest.mark.asyncio
async def test_pii_mask_rewrites_result_in_place():
    plane = compose_controls(["pii_mask"])
    res = {"type": "text", "text": "email=jane@doe.io ssn=123-45-6789"}
    await plane.after_tool_call(_ctx(), _call("read_customer"), res)
    assert "jane@doe.io" not in res["text"] and "123-45-6789" not in res["text"]
    assert res["redacted_by"] == "pii_mask"

@pytest.mark.asyncio
async def test_log_gate_vetoes_until_recorded():
    plane = compose_controls(["log_gate"])
    assert isinstance(await plane.before_finish(_ctx("charge_card"), "completed"), Proceed)
    assert isinstance(await plane.before_finish(_ctx("remember_note"), "completed"), Halt)

@pytest.mark.asyncio
async def test_rate_cap_denies_past_budget():
    plane = compose_controls(["rate_cap"])
    assert isinstance(await plane.pre_tool_use(_ctx("charge_card"), _call("charge_card")), Continue)
    assert isinstance(await plane.pre_tool_use(_ctx("charge_card", "charge_card", "charge_card"), _call("charge_card")), Deny)

@pytest.mark.asyncio
async def test_compose_empty_is_bare_loop_and_multihook_builds_one_plane():
    assert compose_controls([]) is None
    assert compose_controls(["approval", "pii_mask", "log_gate"]) is not None
```

- [ ] **Step 2: Add pytest-asyncio config** — append to `pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
asyncio_mode = "auto"
```
and add `"pytest-asyncio>=0.24"` to the `dev` dependency group; run `uv sync`.

- [ ] **Step 3: Run to verify** — `uv run pytest tests/test_units.py -v`. If red, fix `units.py` (not the test) until green. Also run the self-check: `uv run python -m console.units` → `units self-check ok`.

- [ ] **Step 4: Commit**

```bash
git add src/console/units.py tests/test_units.py pyproject.toml && git commit -m "feat(units): 5 control-plane units + composition, tested"
```

---

### Task 3: Demo tools

**Files:**
- Verify/finalize: `src/console/tools.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces: `DemoTools` implementing `execute(name, call_id, args)->dict`, `get`, `list`. Tools: `remember_note, read_customer, charge_card, send_email`. Results carry `execution_count`; `read_customer` text contains an email and an SSN.

- [ ] **Step 1: Write the failing test** — `tests/test_tools.py`

```python
import pytest
from console.tools import DemoTools

@pytest.mark.asyncio
async def test_read_customer_carries_pii_and_counts():
    t = DemoTools()
    r = await t.execute("read_customer", "id1", {"customer_id": "c-001"})
    assert "@" in r["text"] and "-" in r["text"]
    assert r["execution_count"] == 1

@pytest.mark.asyncio
async def test_execution_count_increments_per_call_id():
    t = DemoTools()
    await t.execute("charge_card", "x", {"amount": "10"})
    r2 = await t.execute("charge_card", "x", {"amount": "10"})
    assert r2["execution_count"] == 2

@pytest.mark.asyncio
async def test_list_defines_four_tools():
    assert {d["name"] for d in DemoTools().list()} == {"remember_note", "read_customer", "charge_card", "send_email"}
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_tools.py -v`. Fix `tools.py` if red.
- [ ] **Step 3: Commit** — `git add src/console/tools.py tests/test_tools.py && git commit -m "feat(tools): demo effects with exec-count, tested"`

---

### Task 4: Scenarios + dormancy reasons

**Files:**
- Verify/finalize: `src/console/scenarios.py`, `src/console/dormancy.py`
- Test: `tests/test_scenarios.py`

**Interfaces:**
- Produces: `SCENARIOS` (list of `{id,title,does,risk,prompt}`, ids `note/customer/charge/batch`), `SYSTEM_PROMPT`, `dormant_reason(unit, scenario_id) -> str`.

- [ ] **Step 1: Write the failing test** — `tests/test_scenarios.py`

```python
from console.scenarios import SCENARIOS
from console.dormancy import dormant_reason

def test_four_scenarios_well_formed():
    ids = [s["id"] for s in SCENARIOS]
    assert ids == ["note", "customer", "charge", "batch"]
    for s in SCENARIOS:
        assert s["prompt"] and s["title"] and s["risk"]

def test_dormant_reason_specific_then_default():
    assert "remember_note" in dormant_reason("log_gate", "note")   # scenario-specific
    assert dormant_reason("pii_mask", "charge")                     # per-unit default
    assert dormant_reason("unknown_unit", "charge")                 # generic fallback, non-empty
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_scenarios.py -v`. Fix if red.
- [ ] **Step 3: Commit** — `git add src/console/scenarios.py src/console/dormancy.py tests/test_scenarios.py && git commit -m "feat(scenarios): 4 locked scenarios + dormancy reasons, tested"`

---

### Task 5: Provider + store

**Files:**
- Verify/finalize: `src/console/provider.py`, `src/console/store.py`
- Test: `tests/test_provider_store.py`

**Interfaces:**
- Produces: `openrouter_model(name=None)->ChatOpenAI` (raises if no key), `DEFAULT_MODEL`; `make_store()->(store, closer)` returning `MemorySteps` when `DATABASE_URL` unset.

- [ ] **Step 1: Write the failing test** — `tests/test_provider_store.py`

```python
import pytest
from console.provider import openrouter_model, DEFAULT_MODEL
from console.store import make_store

def test_provider_raises_without_key(monkeypatch):
    for k in ("OPENROUTER_API_KEY", "OPENROUTER_KEY", "OPEN_ROTURE"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        openrouter_model()

def test_provider_builds_with_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    m = openrouter_model()
    assert DEFAULT_MODEL.startswith("deepseek/")
    assert m.model_name == DEFAULT_MODEL or m.model_name  # constructed

@pytest.mark.asyncio
async def test_make_store_memory_without_db(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store, closer = await make_store()
    assert store is not None and closer is None
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_provider_store.py -v`. Fix if red (note: `ChatOpenAI` attr may be `model_name`; adjust assert to whatever the lib exposes without changing provider behavior).
- [ ] **Step 3: Commit** — `git add src/console/provider.py src/console/store.py tests/test_provider_store.py && git commit -m "feat(provider,store): openrouter model + memory/postgres store, tested"`

---

### Task 6: Server endpoints + policy summary wiring

**Files:**
- Verify/finalize: `src/console/server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: units, tools, scenarios, dormancy, provider, store.
- Produces: FastAPI `app` with `GET /api/scenarios`, `GET /api/units` (`{model, units:[{name,point,composer,verdict,title,desc}]}`), `POST /api/run` (ndjson), `POST /api/resume` (ndjson). Emits frames: `meta, agent, unit, lifecycle, suspended, outcome, policy_summary, error`. `policy_summary` = `{kind, units:[{name, fired, count, reason}]}`.

- [ ] **Step 1: Write the failing test** — `tests/test_server.py` (uses FastAPI TestClient; the read-only endpoints need no key or LLM)

```python
from fastapi.testclient import TestClient
from console.server import app

def test_scenarios_endpoint():
    with TestClient(app) as c:
        r = c.get("/api/scenarios")
        assert r.status_code == 200
        assert [s["id"] for s in r.json()] == ["note", "customer", "charge", "batch"]

def test_units_endpoint_shape():
    with TestClient(app) as c:
        r = c.get("/api/units")
        assert r.status_code == 200
        body = r.json()
        assert body["model"].startswith("deepseek/")
        names = {u["name"] for u in body["units"]}
        assert names == {"approval", "dlp_block", "rate_cap", "pii_mask", "log_gate"}
        points = {u["point"] for u in body["units"]}
        assert {"pre_tool_use", "after_tool_call", "before_finish"} <= points
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_server.py -v`. Note: `TestClient(app)` triggers lifespan (opens MemorySteps) — must succeed without DATABASE_URL. Fix `server.py` if red.
- [ ] **Step 3: Commit** — `git add src/console/server.py tests/test_server.py && git commit -m "feat(server): run/resume streaming + units/scenarios endpoints + policy summary, tested"`

---

### Task 7: Frontend shell — dark cockpit (index.html + styles.css)

**Files:**
- Create: `src/console/static/index.html`, `src/console/static/styles.css`

**Interfaces:**
- Produces: a 3-column shell served at `/`. Required element ids app.js depends on (Task 8): `#scenarios`, `#units`, `#compose-summary`, `#run`, `#status`, `#stream`, `#approval` (+ `#approve`, `#deny`), `#policy-strip`, `#model`. Verdict CSS classes: `.v-suspend, .v-deny, .v-rewrite, .v-steer, .v-allow`. Dark palette tokens per spec (allow=green, suspend=amber, deny=red, rewrite=cyan, steer=violet).

- [ ] **Step 1: Write `index.html`** — 3 columns (좌 작업 / 중 컨트롤 플레인 / 우 실행), Pretendard via jsdelivr CDN + fallback stack, `<link href="/styles.css?v=1">`, `<script src="/app.js?v=1">`. Include every id above. Empty-state line in `#stream`. Suspend card `#approval.hidden` with Approve/Deny. Policy strip `#policy-strip`.

- [ ] **Step 2: Write `styles.css`** — dark tokens (`--bg,--panel,--line,--muted,--text` + verdict colors `--suspend/--deny/--rewrite/--steer/--allow`), 3-column grid (collapse < 860px), scenario cards, unit chips grouped by hook with a lit state, run button, stream rows, unit-action rows colored per verdict, amber suspend card, policy-strip chips (fired vs dormant).

- [ ] **Step 3: Verify served** — `uv run uvicorn console.server:app --port 8877 &` then:
```bash
sleep 4
curl -s -o /dev/null -w "index:%{http_code} css:%{http_code}\n" http://127.0.0.1:8877/ ; curl -s -o /dev/null -w "" http://127.0.0.1:8877/styles.css
curl -s http://127.0.0.1:8877/ | grep -c 'id="stream"'   # expect 1
pkill -f "uvicorn console.server"
```
Expected: 200s; grep prints `1` (and repeat-check the other required ids exist).

- [ ] **Step 4: Commit** — `git add src/console/static/index.html src/console/static/styles.css && git commit -m "feat(ui): dark cockpit shell + verdict palette"`

---

### Task 8: Frontend logic — stream assembly + composition view (app.js)

**Files:**
- Create: `src/console/static/app.js`
- Test: `tests/test_stream.mjs` (node headless) + a runner note

**Interfaces:**
- Consumes: `/api/scenarios`, `/api/units`, `/api/run`, `/api/resume`; element ids from Task 7.
- Produces: `renderFrame(frame)`, delta accumulation (`appendDelta`/`endDeltas`), `renderUnits` grouped by control point with a live `#compose-summary` reading `ControlPlane(pre_tool_use=Permissions(...), ...)`, `#policy-strip` populated from `policy_summary`, suspend card wired to `/api/resume`.

- [ ] **Step 1: Write the failing headless test** — `tests/test_stream.mjs` (pure JS assembly reducer, no DOM): the reducer under test is a small exported function `reduce(frames)->rows`. Extract the assembly logic into a pure function in `app.js` guarded so node can import it (e.g. `export`-like via `globalThis.__reducer` when not in browser, or a tiny `reducer.js` imported by both). Test asserts: contiguous `text` deltas collapse to ONE row with concatenated text; a `tool_call`/`unit`/`suspended` frame closes the current text row; `unit` frames map verdict→class.

```js
import assert from "node:assert";
import { reduceFrames } from "../src/console/static/reducer.mjs";

const rows = reduceFrames([
  { kind: "agent", event: { type: "tool_call", name: "read_customer", input: {} } },
  { kind: "unit", unit: "pii_mask", verdict: "rewrite", message: "masked" },
  { kind: "agent", event: { type: "text", text: "고객 " } },
  { kind: "agent", event: { type: "text", text: "요약" } },
  { kind: "outcome", outcome: { stop_reason: "completed" } },
]);
const textRows = rows.filter(r => r.cls === "text");
assert.equal(textRows.length, 1, "deltas accumulate to one bubble");
assert.equal(textRows[0].text, "고객 요약");
assert.ok(rows.some(r => r.cls === "unit v-rewrite"));
console.log("stream reducer ok");
```

- [ ] **Step 2: Run to verify it fails** — `node tests/test_stream.mjs` → fails (module missing).

- [ ] **Step 3: Implement** — create `src/console/static/reducer.mjs` exporting `reduceFrames(frames)` (the pure assembly), and have `app.js` import/use it for live rendering (browsers support ES module `<script type="module">`, or inline the reducer and duplicate minimal logic — prefer a shared `reducer.mjs`). Then build the rest of `app.js`: fetch + render scenarios/units, grouped hook view, compose summary, run/stream (ndjson carry-buffer), suspend card, policy strip.

- [ ] **Step 4: Run to verify it passes** — `node tests/test_stream.mjs` → `stream reducer ok`. Also re-serve and hard-check the page loads app.js (200).

- [ ] **Step 5: Commit** — `git add src/console/static/app.js src/console/static/reducer.mjs tests/test_stream.mjs && git commit -m "feat(ui): stream assembly + composition view + policy strip, headless-tested"`

---

### Task 9: Live acceptance gate (real LLM matrix)

**Files:**
- Create: `scripts/acceptance.py`

**Interfaces:**
- Consumes: a running server + `OPENROUTER_API_KEY`. Asserts each unit visibly acts in its home scenario.

- [ ] **Step 1: Write `scripts/acceptance.py`** — starts nothing; posts to a already-running `http://127.0.0.1:8850`. For each case, stream ndjson and assert the expected frame:
  - approval + `charge` → a `suspended` frame; then `POST /api/resume approved` → a `tool_result` with `result.execution_count == 1`.
  - dlp_block + `customer` → a `unit` frame `verdict=deny` on send.
  - rate_cap + `batch` → a `unit` frame `verdict=deny` (3rd charge); exactly 2 `charged` results.
  - pii_mask + `customer` → `read_customer` result text has no raw email/SSN and a `unit verdict=rewrite` frame appears.
  - log_gate + `charge` → a `unit verdict=steer` frame + an extra `remember_note` tool_call, then `outcome`.
  - headline: customer + [approval, dlp_block, pii_mask] → a `rewrite` on read AND a `deny` (not suspend) on send.
  - every run ends with a `policy_summary`; assert non-home selected units are `fired=false` with a non-null `reason`.

- [ ] **Step 2: Run** — `uv run uvicorn console.server:app --port 8850 &` (with `.env` key); `sleep 5`; `uv run python scripts/acceptance.py`; expect `ALL LIVE CHECKS PASS`; `pkill -f "uvicorn console.server"`.

- [ ] **Step 3: Commit** — `git add scripts/acceptance.py && git commit -m "test: live acceptance gate for per-unit visibility"`

---

### Task 10: Durable proof + docs

**Files:**
- Verify: `README.md` durable section (exists)
- Create: `tests/test_durable.py` (skipped without `DATABASE_URL`)

**Interfaces:**
- Produces: a headless test asserting suspend→(restart-simulating fresh runtime over the same store)→resume runs the effect once. Without Postgres, skip.

- [ ] **Step 1: Write `tests/test_durable.py`** — `@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs Postgres")`; run approval+charge to suspend against a `PostgresSteps` store, build a NEW `AgentRuntime` over the same store, resume by `run_id`, assert the charge result `execution_count == 1`.

- [ ] **Step 2: Run** — `uv run pytest tests/test_durable.py -v` (skips locally; documents the path). If a Postgres URL is available, run it green.

- [ ] **Step 3: Full suite + commit** — `uv run pytest -q` (all green/skipped) then `git add tests/test_durable.py README.md && git commit -m "test,docs: durable exactly-once proof + README"`

---

## Self-Review

**Spec coverage:** narrative (README + UI Task 7/8) ✓; 5 units across 3 hooks (Task 2) ✓; scenario×unit matrix incl. headline (Task 9) ✓; semantic integrity / seam discipline (Global Constraints + Task 2 desc) ✓; no-silent-no-op via policy_summary (Task 4/6/9) ✓; streaming assembly fix (Task 8) ✓; durable suspend/resume in UI + deferred crash → deploy/test (Task 9/10) ✓; dark cockpit UI (Task 7/8) ✓; verification A/B/C/D → Tasks 2/8/9/10 ✓.

**Placeholder scan:** test code is concrete; implementation for large UI files (Task 7/8) is described by required ids/classes + a headless-tested pure reducer rather than pasted verbatim — acceptable because the deliverable is verified by served-page checks and the reducer test.

**Type consistency:** unit names, frame `kind`s (`meta/agent/unit/lifecycle/suspended/outcome/policy_summary/error`), `policy_summary` row shape (`name,fired,count,reason`), and `/api/units` shape (`{model,units:[…]}`) are used identically in server (Task 6), acceptance (Task 9), and UI (Task 8).
