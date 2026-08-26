import { reduceFrames } from "./reducer.mjs?v=15";
import { createNdjsonReader } from "./ndjson.mjs?v=15";
import {
  createViewState,
  rerunWithoutPolicies,
  switchMode as nextMode,
} from "./view-state.mjs?v=17";
import { startWhenIdle } from "./run-guard.mjs?v=17";

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
const meta = {}; // name -> {point, composer, verdict}

const POINT_ORDER = ["on_inputs", "before_model", "pre_tool_use", "after_tool_call", "before_finish", "on_suspend"];

function mk(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

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
  $("run-error").textContent = "";
  $("policy-strip").classList.add("hidden");
  $("approval").classList.add("hidden");
  $("recovery").classList.add("hidden");
}

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

function renderScenarios(scenarios) {
  const box = $("scenarios");
  box.replaceChildren();
  scenarios.forEach((s) => {
    const el = mk("button", "scenario" + (s.risk.includes("없음") ? " baseline" : ""));
    el.dataset.scenario = s.id;
    el.type = "button";
    el.setAttribute("aria-pressed", "false");
    const prompt = mk("div", "s-prompt");
    prompt.append(mk("span", "s-lock", "고정된 지시"), document.createTextNode(s.prompt));
    const risk = s.risk.includes("없음") ? s.risk : "위험 · " + s.risk;
    el.append(mk("div", "s-title", s.title), mk("div", "s-does", s.does), mk("div", "s-risk", risk), prompt);
    el.addEventListener("click", () => {
      state.scenario = s.id;
      syncSelectionUi();
    });
    box.appendChild(el);
  });
}

function renderUnits(units) {
  const box = $("units");
  box.replaceChildren();
  const byPoint = {};
  units.forEach((u) => (byPoint[u.point] ||= []).push(u));
  POINT_ORDER.filter((p) => byPoint[p]).forEach((point) => {
    const group = mk("div", "point");
    const head = mk("div", "point-head");
    head.append(
      mk("span", "point-hook", point),
      mk("span", "point-arrow", "→"),
      mk("span", "point-composer", byPoint[point][0].composer),
      mk("span", "point-rule"),
    );
    group.appendChild(head);
    byPoint[point].forEach((u) => {
      const el = mk("button", "unit");
      el.dataset.unit = u.name;
      el.type = "button";
      el.setAttribute("aria-pressed", "false");
      el.append(
        mk("span", "chk", "✓"),
        mk("span", "u-name", u.name),
        mk("span", "u-verdict v-" + u.verdict.toLowerCase(), u.verdict),
        mk("span", "u-desc", u.desc),
      );
      el.addEventListener("click", () => {
        if (state.units.has(u.name)) state.units.delete(u.name);
        else state.units.add(u.name);
        syncSelectionUi();
      });
      group.appendChild(el);
    });
    box.appendChild(group);
  });
}

function updateCompose() {
  const selected = [...state.units];
  if (!selected.length) {
    $("compose-summary").textContent = "ControlPlane()  ·  컨트롤 없음";
    return;
  }
  const slots = [];
  for (const point of POINT_ORDER) {
    const names = selected.filter((n) => meta[n]?.point === point);
    if (names.length) slots.push(`${point}=${meta[names[0]].composer}(${names.join(", ")})`);
  }
  $("compose-summary").textContent = `ControlPlane(${slots.join(", ")})`;
}

function syncRun() {
  document.querySelectorAll("[data-run]").forEach((button) => {
    button.disabled = !state.scenario || state.busy;
  });
  $("abort").classList.toggle("hidden", !state.busy);
  $("steer-box").classList.toggle("hidden", !state.busy);
}

function setStatus(kind, label) {
  $("status").className = "status " + kind;
  $("status").textContent = label;
}

// ── row reconcile from the pure reducer ─────────────────────────────────
function rowNode(row) {
  const el = mk("div", "row " + row.cls);
  if (row.cls.startsWith("unit")) {
    el.append(mk("span", "tag", row.tag), mk("span", "msg", row.text));
  } else if (row.cls === "result") {
    el.append(document.createTextNode(row.text));
    if (row.exec != null) el.append(mk("span", "exec", `· exec ×${row.exec}`));
  } else {
    el.textContent = row.text;
  }
  return el;
}

function renderRows() {
  const rows = reduceFrames(state.frames);
  const stream = $("stream");
  const empty = stream.querySelector(".empty");
  if (rows.length && empty) empty.remove();
  for (let i = 0; i < rows.length; i++) {
    const existing = state.rowEls[i];
    if (!existing) {
      const el = rowNode(rows[i]);
      state.rowEls[i] = el;
      stream.appendChild(el);
      el.scrollIntoView({ block: "nearest" });
    } else if (rows[i].cls === "text" || rows[i].cls === "thinking") {
      if (existing.textContent !== rows[i].text) {
        existing.textContent = rows[i].text;
        existing.scrollIntoView({ block: "nearest" });
      }
    } else if (existing.className !== "row " + rows[i].cls) {
      const el = rowNode(rows[i]);
      existing.replaceWith(el);
      state.rowEls[i] = el;
    }
  }
  while (state.rowEls.length > rows.length) {
    state.rowEls.pop().remove();
  }
}

function renderSteerQueue() {
  const box = $("steer-queue");
  box.replaceChildren();
  state.steers.forEach((s) => {
    const el = mk("div", "steer-item " + (s.phase || ""));
    el.append(mk("span", "src", s.source), mk("span", "phase", s.phase === "queued" ? "대기" : "반영"), mk("span", "body", s.text));
    box.appendChild(el);
  });
}

function renderPolicyStrip(units) {
  const strip = $("policy-strip");
  strip.replaceChildren();
  strip.classList.remove("hidden");
  units.forEach((u) => {
    const chip = mk("div", "pchip " + (u.fired ? "fired" : "dormant"));
    chip.append(mk("span", "dot"));
    chip.append(mk("span", "name", u.name + (u.fired ? ` ×${u.count}` : "")));
    if (!u.fired && u.reason) chip.append(mk("span", "why", "· " + u.reason));
    strip.appendChild(chip);
  });
}

function handleFrame(f) {
  if (f.kind === "steer") {
    const src = f.source === "user_steer" ? "운영자" : f.source === "control" ? "정책" : f.source;
    const i = state.steers.findIndex((s) => s.phase === "queued" && s.text === f.text);
    if (i >= 0) state.steers[i] = { source: src, text: f.text, phase: f.phase || "admitted" };
    else state.steers.push({ source: src, text: f.text, phase: f.phase || "admitted" });
    renderSteerQueue();
    if (f.phase === "queued") return;
  }
  state.frames.push(f);
  if (f.kind === "meta") state.runId = f.run_id;
  else if (f.kind === "suspended") {
    setStatus("suspended", "일시중지");
    $("approval").dataset.pending = f.pending_id;
    $("approval-meta").textContent = `${state.runId} · ${f.pending_id}`;
    $("approval").classList.remove("hidden");
    $("approval").focus();
  } else if (f.kind === "outcome") {
    const reason = f.outcome && f.outcome.stop_reason;
    if (reason === "aborted") setStatus("aborted", "중단됨");
    else if ($("status").textContent === "실행 중") setStatus("done", "완료");
  } else if (f.kind === "recoverable") {
    setStatus("error", "장애");
    $("recovery-meta").textContent = f.step ? `스텝 ${f.step}` : "";
    $("recovery").classList.remove("hidden");
    $("recovery").focus();
  } else if (f.kind === "error") {
    setStatus("error", "오류");
    $("run-error").textContent =
      f.message || "실행 중 오류가 발생했습니다.";
  } else if (f.kind === "policy_summary") renderPolicyStrip(f.units);
  renderRows();
}

async function stream(url, body) {
  state.busy = true;
  $("abort").disabled = false;
  syncRun();
  setStatus("running", "실행 중");
  state.abortCtl = new AbortController();
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: state.abortCtl.signal,
    });
    if (!res.ok || !res.body) {
      throw new Error("실행 요청 실패 · HTTP " + res.status);
    }
    const reader = res.body.getReader();
    const ndjson = createNdjsonReader(handleFrame);
    while (true) {
      const { value, done } = await reader.read();
      if (value) ndjson.push(value);
      if (done) break;
    }
    ndjson.end();
  } catch (err) {
    if (err && err.name === "AbortError") {
      if ($("status").textContent === "실행 중") {
        handleFrame({ kind: "outcome", outcome: { stop_reason: "aborted" } });
        setStatus("aborted", "중단됨");
      }
    } else {
      handleFrame({ kind: "error", message: String(err) });
    }
  }
  state.abortCtl = null;
  state.busy = false;
  $("abort").disabled = false;
  if ($("status").textContent === "실행 중") setStatus("done", "완료");
  syncRun();
}

async function enqueueSteer(ev) {
  ev.preventDefault();
  const text = $("steer-text").value.trim();
  if (!text || !state.busy || !state.runId) return;
  $("steer-text").value = "";
  handleFrame({ kind: "steer", source: "user_steer", text, phase: "queued" });
  try {
    const res = await fetch("/api/steer", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ run_id: state.runId, text }),
    });
    if (!res.ok) handleFrame({ kind: "error", message: "지시를 넣지 못했습니다 · " + res.status });
  } catch (err) {
    handleFrame({ kind: "error", message: String(err) });
  }
}

async function abortRun() {
  if (!state.busy) return;
  $("abort").disabled = true;
  if (state.runId) {
    try {
      await fetch("/api/abort", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ run_id: state.runId }),
      });
    } catch (_) {
      state.abortCtl?.abort();
    }
  } else {
    state.abortCtl?.abort();
  }
}

async function run() {
  if (state.busy) return;
  resetRunView();
  const usedPolicies = state.units.size > 0;
  await stream("/api/run", {
    scenario_id: state.scenario,
    units: [...state.units],
  });
  $("rerun-plain").classList.toggle("hidden", !usedPolicies);
}

async function runWithoutPolicies() {
  if (state.busy) return;
  const next = rerunWithoutPolicies(currentViewState());
  state.units = new Set(next.unitNames);
  syncSelectionUi();
  await run();
}

async function recover() {
  await startWhenIdle(state, async () => {
    $("recovery").classList.add("hidden");
    handleFrame({ kind: "unit", unit: "recover", verdict: "steer", message: "복원" });
    await stream("/api/recover", { run_id: state.runId });
  });
}

async function resume(approved) {
  await startWhenIdle(state, async () => {
    $("approval").classList.add("hidden");
    handleFrame({ kind: "unit", unit: "operator", verdict: approved ? "allow" : "deny", message: approved ? "승인" : "거부" });
    await stream("/api/resume", { run_id: state.runId, pending_id: $("approval").dataset.pending, approved });
  });
}

boot();
