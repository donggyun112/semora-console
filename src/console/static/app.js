import { reduceFrames } from "./reducer.mjs";

const $ = (id) => document.getElementById(id);
const state = { scenario: null, units: new Set(), frames: [], runId: null, busy: false, rowEls: [] };
const meta = {}; // name -> {point, composer, verdict}

const POINT_ORDER = ["on_inputs", "before_model", "pre_tool_use", "after_tool_call", "before_finish", "on_suspend"];

async function boot() {
  const [scenarios, unitsBody] = await Promise.all([
    fetch("/api/scenarios").then((r) => r.json()),
    fetch("/api/units").then((r) => r.json()),
  ]);
  $("model").textContent = unitsBody.model;
  unitsBody.units.forEach((u) => (meta[u.name] = u));
  renderScenarios(scenarios);
  renderUnits(unitsBody.units);
  updateCompose();
  $("run").addEventListener("click", run);
  $("approve").addEventListener("click", () => resume(true));
  $("deny").addEventListener("click", () => resume(false));
}

function mk(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

function renderScenarios(scenarios) {
  const box = $("scenarios");
  box.replaceChildren();
  scenarios.forEach((s) => {
    const el = mk("button", "scenario" + (s.risk.includes("없음") ? " baseline" : ""));
    const prompt = mk("div", "s-prompt");
    prompt.append(mk("span", "s-lock", "LOCKED PROMPT"), document.createTextNode(s.prompt));
    el.append(mk("div", "s-title", s.title), mk("div", "s-does", s.does), mk("div", "s-risk", "위험 · " + s.risk), prompt);
    el.addEventListener("click", () => {
      state.scenario = s.id;
      [...box.children].forEach((c) => c.classList.toggle("active", c === el));
      syncRun();
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
      el.append(
        mk("span", "chk", "✓"),
        mk("span", "u-name", u.name),
        mk("span", "u-verdict v-" + u.verdict.toLowerCase(), u.verdict),
        mk("span", "u-desc", u.desc),
      );
      el.addEventListener("click", () => {
        const on = el.classList.toggle("on");
        if (on) state.units.add(u.name);
        else state.units.delete(u.name);
        updateCompose();
      });
      group.appendChild(el);
    });
    box.appendChild(group);
  });
}

function updateCompose() {
  const selected = [...state.units];
  if (!selected.length) {
    $("compose-summary").textContent = "ControlPlane()  ·  bare loop — no controls";
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
  $("run").disabled = !state.scenario || state.busy;
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
    } else if ((rows[i].cls === "text" || rows[i].cls === "thinking") && existing.textContent !== rows[i].text) {
      // the only row that grows in place is the open text/thinking bubble
      existing.textContent = rows[i].text;
      existing.scrollIntoView({ block: "nearest" });
    }
  }
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
  state.frames.push(f);
  if (f.kind === "meta") state.runId = f.run_id;
  else if (f.kind === "suspended") {
    setStatus("suspended", "일시중지");
    $("approval").dataset.pending = f.pending_id;
    $("approval-meta").textContent = `run ${state.runId} · pending ${f.pending_id} — resumable by id`;
    $("approval").classList.remove("hidden");
  } else if (f.kind === "outcome") {
    if ($("status").textContent === "실행 중") setStatus("done", "완료");
  } else if (f.kind === "error") setStatus("error", "오류");
  else if (f.kind === "policy_summary") renderPolicyStrip(f.units);
  renderRows();
}

async function stream(url, body) {
  state.busy = true;
  syncRun();
  setStatus("running", "실행 중");
  let res;
  try {
    res = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  } catch (err) {
    handleFrame({ kind: "error", message: String(err) });
    state.busy = false; syncRun(); return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) { try { handleFrame(JSON.parse(line)); } catch (_) {} }
    }
  }
  state.busy = false;
  if ($("status").textContent === "실행 중") setStatus("done", "완료");
  syncRun();
}

async function run() {
  state.frames = [];
  state.rowEls = [];
  $("stream").replaceChildren();
  $("policy-strip").classList.add("hidden");
  $("approval").classList.add("hidden");
  await stream("/api/run", { scenario_id: state.scenario, units: [...state.units] });
}

async function resume(approved) {
  $("approval").classList.add("hidden");
  handleFrame({ kind: "unit", unit: "operator", verdict: approved ? "allow" : "deny", message: approved ? "사람이 승인함 — 재개" : "사람이 거부함" });
  await stream("/api/resume", { run_id: state.runId, pending_id: $("approval").dataset.pending, approved });
}

boot();
