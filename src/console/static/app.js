import { createNdjsonReader } from "./ndjson.mjs";
import { reduceFrames, summarizeOutcome } from "./reducer.mjs";
import {
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
} from "./run-state.mjs";

const PHASE_LABELS = {
  idle: "대기",
  streaming: "실행 중",
  suspended: "승인 대기",
  recoverable: "복구 대기",
  terminal: "완료",
  error: "오류",
};

const ACTIVE_PHASES = new Set(["streaming", "suspended", "recoverable"]);

function must(documentRef, id) {
  const element = documentRef.getElementById(id);
  if (!element) throw new Error(`missing #${id}`);
  return element;
}

function setHidden(element, hidden) {
  element.classList.toggle("hidden", hidden);
}

function setText(element, value) {
  element.textContent = value ?? "";
}

function policyChip(documentRef, name) {
  const chip = documentRef.createElement("span");
  chip.className = "policy-chip";
  chip.textContent = name;
  return chip;
}

function checkedNames(container) {
  return [...container.querySelectorAll('input[type="checkbox"]:checked')].map(
    (input) => input.value,
  );
}

export function getLaunchCopy(scenario) {
  return {
    title: scenario.title,
    prompt: scenario.prompt,
  };
}

function lastPendingTool(tools) {
  return [...tools].reverse().find((tool) => tool.status === "running");
}

export function deriveChatView(prompt, frames) {
  const tools = [];
  const toolById = new Map();
  let assistantText = "";
  let lastDenial = null;

  for (const frame of frames) {
    const event = frame?.kind === "agent" ? frame.event : null;
    if (event?.type === "tool_call") {
      const id = event.id ?? `tool-${tools.length}`;
      let tool = toolById.get(id);
      if (!tool) {
        tool = {
          id,
          name: event.name ?? "tool",
          status: "running",
          summary: "실행 중",
          reason: null,
        };
        tools.push(tool);
        toolById.set(id, tool);
      }
      if (event.blocked) {
        tool.status = "blocked";
        tool.summary = "정책으로 차단";
        tool.reason = lastDenial?.message ?? null;
      }
      continue;
    }

    if (event?.type === "tool_result") {
      const id = event.id ?? `tool-${tools.length}`;
      let tool = toolById.get(id);
      if (!tool) {
        tool = {
          id,
          name: event.name ?? "tool",
          status: "running",
          summary: "실행 중",
          reason: null,
        };
        tools.push(tool);
        toolById.set(id, tool);
      }
      tool.status = event.executed === false ? "blocked" : "completed";
      tool.summary = event.executed === false ? "실행 안 됨" : "실행 완료";
      tool.reason = event.executed === false
        ? (event.result?.message ?? lastDenial?.message ?? null)
        : null;
      continue;
    }

    if (event?.type === "text") {
      assistantText += event.text ?? "";
      continue;
    }

    if (frame?.kind === "unit" && String(frame.verdict).toLowerCase() === "deny") {
      lastDenial = frame;
      continue;
    }

    if (frame?.kind === "suspended") {
      const tool = lastPendingTool(tools);
      if (tool) {
        tool.status = "approval";
        tool.summary = "승인 대기";
      }
      continue;
    }

    if (frame?.kind === "recoverable") {
      const tool = lastPendingTool(tools);
      if (tool) {
        tool.status = "recoverable";
        tool.summary = "복구 대기";
      }
    }
  }

  return {
    user: { role: "user", text: prompt },
    assistant: { role: "assistant", text: assistantText, tools },
  };
}

export function createConsole({
  document: documentRef,
  fetch: fetchRef,
  clipboard = globalThis.navigator?.clipboard,
} = {}) {
  const ids = [
    "launch", "scenario-trigger", "scenario-menu", "launch-title", "launch-prompt",
    "launch-policies", "run", "policy-open", "launch-policy-open", "run-shell",
    "run-title", "run-status", "run-policies", "abort", "chat-thread",
    "event-count", "outcome-strip",
    "rerun-plain", "rerun-same", "retry-run", "return-draft", "trace",
    "details-drawer", "details-close", "details-copy", "details-title",
    "details-body", "steer-form", "steer-text", "policy-drawer", "policy-close",
    "scenarios", "units", "compose-summary", "approval", "approve", "deny",
    "recovery", "recover", "run-error", "boot-error", "boot-retry",
  ];
  const dom = Object.fromEntries(ids.map((id) => [id, must(documentRef, id)]));
  dom.terminalActions = documentRef.querySelector(".terminal-actions");
  dom.errorActions = documentRef.querySelector(".error-actions");
  dom.inspector = documentRef.querySelector(".inspector");

  const state = {
    run: createRunState(),
    scenarios: [],
    unitsMeta: new Map(),
    frames: [],
    rows: [],
    selectedRowId: null,
    abortCtl: null,
    steers: [],
    policyActivity: new Map(),
    booted: false,
    policyOpener: null,
  };

  const scenarioById = (id) => state.scenarios.find((item) => item.id === id);
  const visibleConfig = () =>
    ACTIVE_PHASES.has(state.run.phase) ? state.run.active : state.run.draft;

  function renderChips(container, names) {
    container.replaceChildren();
    if (!names.length) {
      const empty = documentRef.createElement("span");
      empty.className = "policy-chip muted";
      empty.textContent = "정책 없음";
      container.append(empty);
      return;
    }
    container.append(...names.map((name) => policyChip(documentRef, name)));
  }

  function chooseScenario(id) {
    if (!canEditDraft(state.run)) return;
    state.run = updateDraft(state.run, { scenarioId: id });
    setHidden(dom["scenario-menu"], true);
    dom["scenario-trigger"].setAttribute("aria-expanded", "false");
    render();
  }

  function renderScenarioMenu() {
    dom["scenario-menu"].replaceChildren();
    for (const scenario of state.scenarios) {
      const button = documentRef.createElement("button");
      button.type = "button";
      button.className = "scenario-option";
      button.dataset.scenarioId = scenario.id;
      button.setAttribute(
        "aria-current",
        scenario.id === state.run.draft.scenarioId ? "true" : "false",
      );
      const title = documentRef.createElement("strong");
      title.textContent = scenario.title;
      const risk = documentRef.createElement("span");
      risk.textContent = scenario.risk;
      button.append(title, risk);
      button.addEventListener("click", () => chooseScenario(scenario.id));
      dom["scenario-menu"].append(button);
    }
  }

  function renderLaunch() {
    const scenario = scenarioById(state.run.draft.scenarioId);
    if (!scenario) return;
    const copy = getLaunchCopy(scenario);
    setText(dom["scenario-trigger"], scenario.title);
    setText(dom["launch-title"], copy.title);
    setText(dom["launch-prompt"], copy.prompt);
    renderChips(dom["launch-policies"], state.run.draft.unitNames);
    dom.run.disabled = !canStartRun(state.run);
    renderScenarioMenu();
  }

  function renderDetails() {
    const row = state.rows.find((item) => item.id === state.selectedRowId);
    if (!row) {
      state.selectedRowId = null;
      setHidden(dom["details-drawer"], true);
      dom["details-copy"].disabled = true;
      dom.inspector.classList.remove("has-details");
      return;
    }
    dom.inspector.classList.add("has-details");
    setHidden(dom["details-drawer"], false);
    setText(dom["details-title"], row.label);
    setText(dom["details-body"], JSON.stringify(row.details, null, 2));
    dom["details-copy"].disabled = false;
  }

  function selectRow(id) {
    state.selectedRowId = id;
    renderRows();
  }

  function makeTraceRow(row, index) {
    const button = documentRef.createElement("button");
    button.type = "button";
    button.className = `trace-row tone-${row.tone}`;
    button.dataset.rowId = row.id;
    button.setAttribute("aria-expanded", String(row.id === state.selectedRowId));
    button.setAttribute("aria-selected", String(row.id === state.selectedRowId));

    const order = documentRef.createElement("span");
    order.className = "trace-order";
    order.textContent = String(index + 1).padStart(2, "0");
    const label = documentRef.createElement("strong");
    label.className = "trace-label";
    label.textContent = row.label;
    const summary = documentRef.createElement("span");
    summary.className = "trace-summary";
    summary.textContent = row.summary;
    button.append(order, label, summary);
    if (row.verdict) {
      const verdict = documentRef.createElement("span");
      verdict.className = "trace-verdict";
      verdict.textContent = row.verdict;
      button.append(verdict);
    }
    button.addEventListener("click", () => selectRow(row.id));
    return button;
  }

  function attachInlineAction(section, rowKind) {
    const candidates = [...dom.trace.querySelectorAll(".trace-entry")];
    const host = [...candidates].reverse().find((entry) => entry.dataset.kind === rowKind);
    (host ?? dom.trace).append(section);
  }

  function renderRows() {
    state.rows = reduceFrames(state.frames);
    setText(dom["event-count"], `${state.rows.length} EVENTS`);
    dom.trace.replaceChildren();
    state.rows.forEach((row, index) => {
      const entry = documentRef.createElement("article");
      entry.className = "trace-entry";
      entry.dataset.kind = row.kind;
      entry.append(makeTraceRow(row, index));
      dom.trace.append(entry);
    });

    setHidden(dom.approval, state.run.phase !== "suspended");
    if (state.run.phase === "suspended") attachInlineAction(dom.approval, "tool");
    setHidden(dom.recovery, state.run.phase !== "recoverable");
    if (state.run.phase === "recoverable") attachInlineAction(dom.recovery, "recovery");
    renderDetails();
  }

  function makeChatMessage(role, label) {
    const message = documentRef.createElement("article");
    message.className = `chat-message ${role}`;
    const avatar = documentRef.createElement("span");
    avatar.className = "chat-avatar";
    avatar.textContent = label;
    const content = documentRef.createElement("div");
    content.className = "chat-content";
    message.append(avatar, content);
    return { message, content };
  }

  function renderChat(scenario) {
    const view = deriveChatView(scenario?.prompt ?? "", state.frames);
    dom["chat-thread"].replaceChildren();

    const user = makeChatMessage("user", "YOU");
    const userText = documentRef.createElement("p");
    userText.textContent = view.user.text;
    user.content.append(userText);

    const assistant = makeChatMessage("assistant", "NX");
    if (view.assistant.tools.length) {
      const toolList = documentRef.createElement("div");
      toolList.className = "chat-tools";
      for (const tool of view.assistant.tools) {
        const item = documentRef.createElement("div");
        item.className = `chat-tool status-${tool.status}`;
        const mark = documentRef.createElement("span");
        mark.className = "chat-tool-mark";
        mark.textContent = tool.status === "completed"
          ? "✓"
          : tool.status === "blocked"
            ? "×"
            : "·";
        const copy = documentRef.createElement("span");
        const name = documentRef.createElement("strong");
        name.textContent = tool.name;
        const status = documentRef.createElement("small");
        status.textContent = tool.reason
          ? `${tool.summary} · ${tool.reason}`
          : tool.summary;
        copy.append(name, status);
        item.append(mark, copy);
        toolList.append(item);
      }
      assistant.content.append(toolList);
    }
    const assistantText = documentRef.createElement("p");
    assistantText.className = view.assistant.text ? "" : "chat-pending";
    assistantText.textContent = view.assistant.text || "응답을 기다리는 중";
    assistant.content.append(assistantText);

    dom["chat-thread"].append(user.message, assistant.message);
  }

  function renderOutcome() {
    const terminal = state.run.phase === "terminal";
    setHidden(dom["outcome-strip"], !terminal);
    setHidden(dom.terminalActions, !terminal);
    if (terminal) {
      const outcome = summarizeOutcome(state.frames);
      setText(
        dom["outcome-strip"],
        [outcome.verdict, outcome.tool, outcome.result].filter(Boolean).join(" · "),
      );
    }
  }

  function renderRun() {
    const config = state.run.active;
    if (!config) return;
    const scenario = scenarioById(config.scenarioId);
    setText(dom["run-title"], scenario?.title ?? config.scenarioId);
    setText(dom["run-status"], PHASE_LABELS[state.run.phase]);
    renderChips(dom["run-policies"], config.unitNames);
    setHidden(dom.abort, !ACTIVE_PHASES.has(state.run.phase));
    setHidden(dom["steer-form"], !canSteer(state.run));
    setHidden(dom["run-error"], state.run.phase !== "error");
    setText(dom["run-error"], state.run.error);
    setHidden(dom.errorActions, state.run.phase !== "error");
    renderChat(scenario);
    renderOutcome();
    renderRows();
  }

  function renderPolicyComposer() {
    const config = visibleConfig();
    const readOnly = ACTIVE_PHASES.has(state.run.phase);
    dom.scenarios.replaceChildren();
    for (const scenario of state.scenarios) {
      const label = documentRef.createElement("label");
      label.className = "scenario-radio";
      const input = documentRef.createElement("input");
      input.type = "radio";
      input.name = "scenario";
      input.value = scenario.id;
      input.checked = config?.scenarioId === scenario.id;
      input.disabled = readOnly;
      input.addEventListener("change", () => chooseScenario(scenario.id));
      const text = documentRef.createElement("span");
      text.textContent = scenario.title;
      label.append(input, text);
      dom.scenarios.append(label);
    }

    dom.units.replaceChildren();
    const groups = new Map();
    for (const unit of state.unitsMeta.values()) {
      const items = groups.get(unit.point) ?? [];
      items.push(unit);
      groups.set(unit.point, items);
    }
    for (const [point, units] of groups) {
      const group = documentRef.createElement("details");
      group.open = true;
      const summary = documentRef.createElement("summary");
      summary.textContent = point;
      group.append(summary);
      for (const unit of units) {
        const label = documentRef.createElement("label");
        label.className = "unit-toggle";
        const input = documentRef.createElement("input");
        input.type = "checkbox";
        input.value = unit.name;
        input.checked = config?.unitNames.includes(unit.name) ?? false;
        input.disabled = readOnly;
        input.addEventListener("change", () => {
          if (!canEditDraft(state.run)) return;
          state.run = updateDraft(state.run, { unitNames: checkedNames(dom.units) });
          render();
        });
        const copy = documentRef.createElement("span");
        const title = documentRef.createElement("strong");
        title.textContent = unit.title ?? unit.name;
        const desc = documentRef.createElement("small");
        const activity = state.policyActivity.get(unit.name);
        desc.textContent = activity
          ? activity.fired
            ? `${unit.desc} · ${activity.count}회 동작`
            : `${unit.desc} · ${activity.reason}`
          : unit.desc;
        copy.append(title, desc);
        label.append(input, copy);
        group.append(label);
      }
      dom.units.append(group);
    }
    const selectedCount = config?.unitNames.length ?? 0;
    setText(
      dom["compose-summary"],
      readOnly ? `실행 중인 설정 · 정책 ${selectedCount}개` : `정책 ${selectedCount}개 선택`,
    );
  }

  function render() {
    const idle = state.run.phase === "idle";
    setHidden(dom.launch, !idle);
    setHidden(dom["run-shell"], idle);
    if (idle) renderLaunch();
    else renderRun();
    renderPolicyComposer();
  }

  function handleFrame(frame) {
    state.frames.push(frame);
    if (frame.kind === "meta") {
      state.run = attachRunId(state.run, frame.run_id);
    } else if (frame.kind === "suspended") {
      state.run = suspendRun(state.run, frame.pending_id);
    } else if (frame.kind === "recoverable") {
      state.run = markRecoverable(state.run);
    } else if (frame.kind === "outcome") {
      state.run = finishRun(
        state.run,
        frame.outcome?.stop_reason ?? frame.stop_reason ?? "completed",
      );
    } else if (frame.kind === "error") {
      state.run = failRun(state.run, frame.message ?? "실행에 실패했습니다.");
    } else if (frame.kind === "policy_summary") {
      for (const unit of frame.units ?? []) state.policyActivity.set(unit.name, unit);
    }
    render();
  }

  async function post(path, body) {
    const response = await fetchRef(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      let reason = `${response.status}`;
      try {
        reason = (await response.json()).detail ?? reason;
      } catch {}
      throw new Error(reason);
    }
    return response;
  }

  async function consume(response) {
    if (!response.body) throw new Error("실행 스트림을 열지 못했습니다.");
    const parser = createNdjsonReader(handleFrame);
    const reader = response.body.getReader();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.push(value);
    }
    parser.end();
    if (!acceptsStreamEnd(state.run)) {
      throw new Error("실행 스트림이 완료 상태 없이 종료되었습니다.");
    }
  }

  function failCurrent(error) {
    if (state.run.active && state.run.phase !== "error") {
      state.run = failRun(state.run, error?.message ?? String(error));
    }
    render();
  }

  async function stream(path, body) {
    try {
      await consume(await post(path, body));
    } catch (error) {
      failCurrent(error);
    }
  }

  async function runActive() {
    if (!canStartRun(state.run)) return;
    state.run = startRun(state.run);
    state.frames = [];
    state.rows = [];
    state.selectedRowId = null;
    state.policyActivity.clear();
    render();
    await stream("/api/run", {
      scenario_id: state.run.active.scenarioId,
      units: [...state.run.active.unitNames],
    });
  }

  async function continueRun(path, body) {
    try {
      state.run = beginContinuation(state.run);
    } catch {
      return;
    }
    render();
    await stream(path, body);
  }

  async function decide(approved) {
    if (state.run.phase !== "suspended") return;
    const body = {
      run_id: state.run.runId,
      pending_id: state.run.pendingId,
      approved,
    };
    await continueRun("/api/resume", body);
  }

  async function recover() {
    if (state.run.phase !== "recoverable") return;
    await continueRun("/api/recover", { run_id: state.run.runId });
  }

  async function abort() {
    if (!ACTIVE_PHASES.has(state.run.phase) || !state.run.runId) return;
    try {
      await post("/api/abort", { run_id: state.run.runId });
      if (state.run.phase !== "terminal") {
        state.frames.push({ kind: "outcome", outcome: { stop_reason: "aborted" } });
        state.run = finishRun(state.run, "aborted");
        render();
      }
    } catch (error) {
      failCurrent(error);
    }
  }

  async function sendSteer(event) {
    event.preventDefault();
    const text = dom["steer-text"].value.trim();
    if (!text || !canSteer(state.run)) return;
    dom["steer-text"].value = "";
    state.frames.push({ kind: "steer", status: "queued", source: "operator", text });
    render();
    try {
      await post("/api/steer", { run_id: state.run.runId, text });
    } catch (error) {
      failCurrent(error);
    }
  }

  function rerun(withoutPolicies) {
    if (!canStartRun(state.run)) return;
    state.run = returnToDraft(state.run, { source: "active", withoutPolicies });
    void runActive();
  }

  function returnDraft() {
    if (!canStartRun(state.run)) return;
    state.run = returnToDraft(state.run, { source: "active" });
    state.frames = [];
    state.selectedRowId = null;
    render();
  }

  function openPolicy(event) {
    state.policyOpener = event.currentTarget;
    renderPolicyComposer();
    dom["policy-drawer"].classList.remove("hidden");
    dom["policy-drawer"].showModal();
  }

  function closePolicy() {
    dom["policy-drawer"].close();
    dom["policy-drawer"].classList.add("hidden");
    state.policyOpener?.focus();
  }

  function bind() {
    dom["scenario-trigger"].addEventListener("click", () => {
      const willOpen = dom["scenario-menu"].classList.contains("hidden");
      setHidden(dom["scenario-menu"], !willOpen);
      dom["scenario-trigger"].setAttribute("aria-expanded", String(willOpen));
    });
    dom.run.addEventListener("click", () => void runActive());
    dom.abort.addEventListener("click", () => void abort());
    dom.approve.addEventListener("click", () => void decide(true));
    dom.deny.addEventListener("click", () => void decide(false));
    dom.recover.addEventListener("click", () => void recover());
    dom["steer-form"].addEventListener("submit", sendSteer);
    dom["details-close"].addEventListener("click", () => {
      state.selectedRowId = null;
      renderRows();
    });
    dom["details-copy"].addEventListener("click", async () => {
      const row = state.rows.find((item) => item.id === state.selectedRowId);
      if (row && clipboard) await clipboard.writeText(JSON.stringify(row.details, null, 2));
    });
    dom["policy-open"].addEventListener("click", openPolicy);
    dom["launch-policy-open"].addEventListener("click", openPolicy);
    dom["policy-close"].addEventListener("click", closePolicy);
    dom["policy-drawer"].addEventListener("cancel", (event) => {
      event.preventDefault();
      closePolicy();
    });
    dom["rerun-plain"].addEventListener("click", () => rerun(true));
    dom["rerun-same"].addEventListener("click", () => rerun(false));
    dom["retry-run"].addEventListener("click", () => rerun(false));
    dom["return-draft"].addEventListener("click", returnDraft);
    dom["boot-retry"].addEventListener("click", () => void boot());
  }

  async function boot() {
    setHidden(dom["boot-error"], true);
    try {
      const [scenarioResponse, unitResponse] = await Promise.all([
        fetchRef("/api/scenarios"),
        fetchRef("/api/units"),
      ]);
      if (!scenarioResponse.ok || !unitResponse.ok) throw new Error("boot failed");
      state.scenarios = await scenarioResponse.json();
      const unitPayload = await unitResponse.json();
      state.unitsMeta = new Map(unitPayload.units.map((unit) => [unit.name, unit]));
      if (!state.booted) bind();
      state.booted = true;
      render();
    } catch {
      setHidden(dom["boot-error"], false);
      setHidden(dom.launch, true);
    }
  }

  return { state, boot, render, runActive, handleFrame };
}

if (typeof document !== "undefined" && typeof fetch !== "undefined") {
  const consoleApp = createConsole({ document, fetch: globalThis.fetch.bind(globalThis) });
  globalThis.__NEXORA_CONSOLE__ = consoleApp;
  void consoleApp.boot();
}
