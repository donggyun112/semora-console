import { createNdjsonReader } from "./ndjson.mjs";
import {
  reduceFrames,
  resultBadges,
  summarizeOutcome,
  toolResultOutput,
} from "./reducer.mjs";
import {
  acceptsStreamEnd,
  attachRunId,
  beginContinuation,
  beginFork,
  canEditDraft,
  canEditPolicy,
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

function makeResultBadge(documentRef, badge) {
  const chip = documentRef.createElement("span");
  chip.className = `result-badge kind-${badge.kind}`;
  chip.textContent = `${badge.label} · ${badge.detail}`;
  return chip;
}

export function makeResultBadges(documentRef, badges) {
  if (!badges?.length) return null;
  const group = documentRef.createElement("span");
  group.className = "result-badges";
  for (const badge of badges) {
    group.append(makeResultBadge(documentRef, badge));
  }
  return group;
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

// The demo offers eighty combinations and no order to read them in. These three are
// the order: what the agent does unguarded, what one gate changes, and what survives
// the worker dying. Everything else is worth exploring only after those.
export const GUIDE = Object.freeze([
  Object.freeze({
    scenarioId: "charge", unitNames: Object.freeze([]),
    label: "정책 없이", teaches: "청구가 그대로 나간다",
  }),
  Object.freeze({
    scenarioId: "charge", unitNames: Object.freeze(["approval"]),
    label: "승인 게이트", teaches: "루프가 멈추고 사람을 기다린다",
  }),
  Object.freeze({
    scenarioId: "crash", unitNames: Object.freeze(["approval"]),
    label: "대기 중 장애", teaches: "복구해도 청구는 한 번",
  }),
]);

// Both gates are durable tool boundaries you can branch from: the one before the call
// and the one after a person answered. They carry different labels so the trace does not
// read as the same row twice, so anything matching on the label has to accept both.
const TOOL_GATES = new Set(["pre_tool_use", "on_resume"]);
const isToolGate = (row) => TOOL_GATES.has(row?.label);

export function guideMatch(config) {
  // Which scene the current draft is sitting on, or -1 once the operator has wandered
  // off the path. Compared by value: the composer is free the moment it stops matching.
  const wanted = [...(config?.unitNames ?? [])].sort().join(",");
  return GUIDE.findIndex((scene) => (
    scene.scenarioId === config?.scenarioId
    && [...scene.unitNames].sort().join(",") === wanted
  ));
}

export function nextGuideStep(config, stopReason) {
  // A scene counts as taught once its run ends well. A failed or aborted attempt leaves
  // the operator where they were rather than marching them past something they did not see.
  const at = guideMatch(config);
  if (at < 0 || stopReason === "aborted" || stopReason === null) return at;
  return Math.min(at + 1, GUIDE.length - 1);
}

const JOURNAL_UNITS = new Set(["pii_mask", "context_firewall", "injection_guard"]);

function journalUnitsChanged(runState) {
  const selected = new Set(
    (runState?.draft?.unitNames ?? []).filter((name) => JOURNAL_UNITS.has(name)),
  );
  const source = new Set(
    (runState?.active?.unitNames ?? []).filter((name) => JOURNAL_UNITS.has(name)),
  );
  if (selected.size !== source.size) return true;
  for (const name of source) {
    if (!selected.has(name)) return true;
  }
  return false;
}

function applyToolOutput(tool, result, blocked = false) {
  if (blocked || result == null) {
    if (blocked) {
      delete tool.output;
      delete tool.redactedBy;
      tool.badges = [];
    }
    return;
  }
  const text = typeof result.text === "string" ? result.text : null;
  if (text) tool.output = text;
  else delete tool.output;
  if (result.redacted_by) tool.redactedBy = result.redacted_by;
  else delete tool.redactedBy;
  tool.badges = resultBadges({ name: tool.name, result });
}

export function pickInlineActionHost(chatToolNodes, callId, status) {
  // The decision belongs next to the call it gates. Fall back to the newest node in
  // the matching status, then to nothing — the caller parks it in the thread.
  const byId = callId ? chatToolNodes.get(callId) : null;
  if (byId) return byId;
  return [...chatToolNodes.values()].reverse()
    .find((node) => node.dataset?.status === status) ?? null;
}

function rowCallId(row) {
  return (
    row?.callId ??
    row?.details?.callId ??
    row?.details?.raw?.payload?.call_id ??
    row?.details?.raw?.event?.id ??
    null
  );
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
          badges: [],
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
          badges: [],
        };
        tools.push(tool);
        toolById.set(id, tool);
      }
      const blocked = event.executed === false;
      tool.status = blocked ? "blocked" : "completed";
      tool.summary = blocked ? "실행 안 됨" : "실행 완료";
      tool.reason = blocked
        ? (event.result?.message ?? lastDenial?.message ?? null)
        : null;
      applyToolOutput(tool, toolResultOutput(event), blocked);
      continue;
    }

    if (event?.type === "text") {
      assistantText += event.text ?? "";
      continue;
    }

    if (frame?.kind === "lifecycle" && frame.type === "post_tool_use") {
      const payload = frame.payload ?? {};
      const id = payload.call_id;
      let tool = toolById.get(id);
      if (!tool && id) {
        tool = {
          id,
          name: payload.name ?? "tool",
          status: "completed",
          summary: "실행 완료",
          reason: null,
          badges: [],
        };
        tools.push(tool);
        toolById.set(id, tool);
      }
      if (tool) {
        tool.status = "completed";
        tool.summary = "실행 완료";
        tool.reason = null;
        applyToolOutput(tool, payload.result);
      }
      continue;
    }

    if (frame?.kind === "unit" && String(frame.verdict).toLowerCase() === "deny") {
      lastDenial = frame;
      continue;
    }

    if (frame?.kind === "suspended") {
      // pending_id is the call id. Guessing the last running call put "승인 대기" on
      // the wrong charge in a parallel batch.
      const tool = toolById.get(frame.pending_id) ?? lastPendingTool(tools);
      if (tool) {
        tool.status = "approval";
        tool.summary = "승인 대기";
      }
      continue;
    }

    if (frame?.kind === "recoverable") {
      // step is the call id at the approval-gate seam; at the commit seam it is an
      // effect key that matches nothing, so fall back to the call still in flight.
      const tool = toolById.get(frame.step) ?? lastPendingTool(tools);
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

export function deriveBranchView(frames) {
  const branches = new Map();
  let activeBranch = null;
  for (const frame of frames) {
    if (frame?.kind !== "lifecycle" || frame.type !== "branch_snapshot") continue;
    const payload = frame.payload ?? {};
    if (!payload.branch) continue;
    branches.set(payload.branch, {
      branch: payload.branch,
      runId: payload.run_id,
      active: false,
      messages: (payload.messages ?? []).map((message) => ({ ...message })),
    });
    activeBranch = payload.branch;
  }
  return [...branches.values()].map((branch) => ({
    ...branch,
    active: branch.branch === activeBranch,
  }));
}

function versionPolicySuffix(units) {
  if (!Array.isArray(units)) return "";
  return units.length ? ` · ${units.join(", ")}` : " · 정책 없음";
}

export function deriveRunVersions(frames) {
  const seen = new Set();
  const runIds = [];
  const unitsByRun = new Map();
  for (const frame of frames) {
    if (frame?.kind !== "meta" || !frame.run_id || seen.has(frame.run_id)) continue;
    seen.add(frame.run_id);
    runIds.push(frame.run_id);
    if (Array.isArray(frame.units)) unitsByRun.set(frame.run_id, frame.units);
  }
  return runIds.map((runId, index) => ({
    runId,
    number: index + 1,
    label: `v${index + 1} · ${index === 0 ? "원본" : "분기"}${versionPolicySuffix(unitsByRun.get(runId))}`,
  }));
}

function selectVersionChatFrames(frames, runId, ancestors = new Set()) {
  const directFrames = frames.filter((frame) => frame?.run_id === runId);
  const meta = directFrames.find((frame) => frame?.kind === "meta");
  const parentRunId = meta?.fork_parent;
  if (!parentRunId || ancestors.has(runId)) return directFrames;

  const nextAncestors = new Set(ancestors);
  nextAncestors.add(runId);
  const parentFrames = selectVersionChatFrames(frames, parentRunId, nextAncestors);
  const forkIndex = parentFrames.findIndex(
    (frame) => frame?.event_id === meta.fork_event_id,
  );
  if (forkIndex < 0) return directFrames;

  const prefixEnd = forkIndex + (meta.fork_edge === "after" ? 1 : 0);
  const currentFrames = directFrames.filter(
    (frame) => frame?.kind !== "meta" && frame?.type !== "session_start",
  );
  return [...parentFrames.slice(0, prefixEnd), ...currentFrames];
}

export function selectRunFrames(frames, runId, options = {}) {
  if (!runId) return frames;
  if (options.inheritFork) return selectVersionChatFrames(frames, runId);
  return frames.filter((frame) => frame?.run_id === runId);
}

function rowFingerprint(row) {
  return [row.kind, row.label, row.summary].join("\u0000");
}

export function deriveVersionRows(frames, runId, ancestors = new Set()) {
  const directFrames = selectRunFrames(frames, runId);
  const directRows = reduceFrames(directFrames).map((row) => ({
    ...row,
    id: `${runId ?? "run"}:${row.id}`,
    versionOrigin: "current",
    forkStart: false,
  }));
  const meta = directFrames.find((frame) => frame?.kind === "meta");
  const parentRunId = meta?.fork_parent;
  if (!runId || !parentRunId || ancestors.has(runId)) return directRows;

  const nextAncestors = new Set(ancestors);
  nextAncestors.add(runId);
  const parentRows = deriveVersionRows(frames, parentRunId, nextAncestors);
  const forkIndex = parentRows.findIndex((row) => row.eventId === meta.fork_event_id);
  if (forkIndex < 0) return directRows;

  const after = meta.fork_edge === "after";
  const prefixEnd = forkIndex + (after ? 1 : 0);
  const selectedFingerprint = rowFingerprint(parentRows[forkIndex]);
  let childStart = directRows.findIndex(
    (row) => rowFingerprint(row) === selectedFingerprint,
  );
  if (childStart < 0) childStart = Math.min(prefixEnd, directRows.length);
  else if (after) childStart += 1;

  const inherited = parentRows.slice(0, prefixEnd).map((row) => ({
    ...row,
    versionOrigin: "inherited",
    forkStart: false,
  }));
  const current = meta.fork_mode === "leaf"
    ? directRows.filter((row) => row.label !== "session_start")
    : directRows.slice(childStart);
  if (current.length) current[0] = { ...current[0], forkStart: true };
  return [...inherited, ...current];
}

export function getForkActionLabel(row, policyCount) {
  if (row?.kind === "tool" || isToolGate(row)) {
    return `툴 실행 전 분기 · 정책 ${policyCount}개`;
  }
  if (row?.forkEdge === "after") {
    return `툴 결과에서 분기 · 정책 ${policyCount}개`;
  }
  return `이 입력에서 분기 · 정책 ${policyCount}개`;
}

function getForkActionDescription(row, request) {
  if (request && row?.eventId && request.event_id !== row.eventId) {
    return "선택한 마스킹 정책으로 툴 결과를 다시 만듭니다.";
  }
  if (row?.kind === "tool" || isToolGate(row)) {
    return "선택한 정책으로 툴 결과를 다시 만듭니다.";
  }
  if (row?.forkEdge === "after") {
    return "저장된 툴 결과 다음부터 이어서 실행합니다.";
  }
  return "선택한 정책으로 이 입력부터 다시 실행합니다.";
}

export function deriveVersionPhase(frames, fallback = "idle") {
  for (const frame of [...frames].reverse()) {
    if (frame?.kind === "outcome") return "terminal";
    if (frame?.kind === "error") return "error";
    if (frame?.kind === "suspended") return "suspended";
    if (frame?.kind === "recoverable") return "recoverable";
    if (frame?.kind === "meta") return "streaming";
  }
  return fallback;
}

export function getEventForkRequest(runState, row, rows = []) {
  if (
    runState?.phase !== "terminal" ||
    !runState?.runId ||
    !row?.eventId ||
    !row?.forkable
  ) {
    return null;
  }
  const units = [...runState.draft.unitNames];
  let eventId = row.eventId;
  let edge = row.forkEdge ?? "before";
  if (
    journalUnitsChanged(runState) &&
    row.forkEdge === "after" &&
    (row.label === "post_tool_use" || row.kind === "result")
  ) {
    const callId = rowCallId(row);
    const target = rows.indexOf(row);
    // The most recent boundary for this call, not the first one. An approved call has
    // two forkable pre_tool_use rows — the gate and the 승인 후 재검증 replay — and
    // forking from the first rewinds past the approval, discarding the operator's
    // decision without saying so.
    const pre = (target >= 0 ? rows.slice(0, target) : rows).findLast((item) => (
      item.forkable
      && isToolGate(item)
      && rowCallId(item) === callId
      && item.eventId
    ));
    if (pre) {
      eventId = pre.eventId;
      edge = pre.forkEdge ?? "before";
    }
  }
  return {
    run_id: row.runId ?? runState.runId,
    event_id: eventId,
    edge,
    units,
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
    "version-switcher",
    "event-count", "outcome-strip",
    "rerun", "retry-run", "return-draft", "trace", "guide", "recover-safe",
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
    forkEventIds: new Set(),
    selectedVersionRunId: null,
    chatToolNodes: new Map(),
  };

  const scenarioById = (id) => state.scenarios.find((item) => item.id === id);
  const visibleConfig = () =>
    canEditPolicy(state.run) ? state.run.draft : state.run.active;

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
    const scenario = scenarioById(id);
    state.run = updateDraft(state.run, {
      scenarioId: id,
      unitNames: scenario?.default_units ?? state.run.draft.unitNames,
    });
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

  function renderGuide() {
    const at = guideMatch(state.run.draft);
    dom.guide.replaceChildren();
    GUIDE.forEach((scene, index) => {
      const item = documentRef.createElement("li");
      item.className = `guide-step${index === at ? " is-current" : ""}`;
      const button = documentRef.createElement("button");
      button.type = "button";
      button.disabled = !canEditDraft(state.run);
      const label = documentRef.createElement("strong");
      label.textContent = `${index + 1}. ${scene.label}`;
      const teaches = documentRef.createElement("small");
      teaches.textContent = scene.teaches;
      button.append(label, teaches);
      button.addEventListener("click", () => {
        if (!canEditDraft(state.run)) return;
        state.run = updateDraft(state.run, {
          scenarioId: scene.scenarioId,
          unitNames: [...scene.unitNames],
        });
        render();
      });
      item.append(button);
      dom.guide.append(item);
    });
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
    renderGuide();
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
    const badges = makeResultBadges(documentRef, row.badges);
    if (badges) button.append(badges);
    if (state.forkEventIds.has(row.eventId)) {
      const marker = documentRef.createElement("span");
      marker.className = "trace-fork-origin";
      marker.textContent = "분기 기준";
      button.append(marker);
    }
    button.addEventListener("click", () => selectRow(row.id));
    return button;
  }

  function attachInlineAction(section, callId, status) {
    const host = pickInlineActionHost(state.chatToolNodes, callId, status)
      ?? dom["chat-thread"];
    host.append(section);
  }

  function renderRows(selectedPhase, selectedIsCurrent) {
    state.rows = deriveVersionRows(state.frames, state.selectedVersionRunId);
    setText(dom["event-count"], `${state.rows.length} EVENTS`);
    dom.trace.replaceChildren();
    state.rows.forEach((row, index) => {
      if (row.forkStart) {
        const cut = documentRef.createElement("div");
        cut.className = "trace-version-cut";
        cut.textContent = "분기 시작";
        dom.trace.append(cut);
      }
      const entry = documentRef.createElement("article");
      entry.className = `trace-entry version-${row.versionOrigin ?? "current"}`;
      entry.dataset.kind = row.kind;
      entry.append(makeTraceRow(row, index));
      const request = getEventForkRequest(state.run, row, state.rows);
      if (request) {
        const action = documentRef.createElement("div");
        action.className = "trace-fork";
        const button = documentRef.createElement("button");
        button.type = "button";
        button.textContent = request.event_id !== row.eventId
          ? `마스킹 정책으로 결과 다시 실행 · 정책 ${request.units.length}개`
          : getForkActionLabel(row, request.units.length);
        const forkDescription = getForkActionDescription(row, request);
        button.title = [
          `적용 정책: ${request.units.join(", ") || "없음"}`,
          forkDescription,
        ].join(" · ");
        button.setAttribute(
          "aria-label",
          `${index + 1}번 이벤트에서 다시 실행. ${forkDescription}`,
        );
        button.addEventListener("click", () => void forkSource(row));
        action.append(button);
        entry.append(action);
      }
      dom.trace.append(entry);
    });

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

  function renderChat(scenario, frames) {
    dom["chat-thread"].replaceChildren();
    state.chatToolNodes.clear();
    const view = deriveChatView(scenario?.prompt ?? "", frames);

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
        item.dataset.callId = tool.id;
        item.dataset.status = tool.status;
        state.chatToolNodes.set(tool.id, item);
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
        copy.append(name);
        if (tool.reason) {
          const status = documentRef.createElement("small");
          status.textContent = `${tool.summary} · ${tool.reason}`;
          copy.append(status);
        } else if (!tool.badges?.length) {
          const status = documentRef.createElement("small");
          status.textContent = tool.summary;
          copy.append(status);
        }
        const badges = makeResultBadges(documentRef, tool.badges);
        if (badges) copy.append(badges);
        if (tool.output) {
          const output = documentRef.createElement("pre");
          output.className = `chat-tool-output${tool.redactedBy ? " is-redacted" : ""}`;
          output.textContent = tool.output;
          copy.append(output);
        }
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

  function renderOutcome(frames, selectedPhase, selectedIsCurrent) {
    const terminal = selectedPhase === "terminal";
    setHidden(dom["outcome-strip"], !terminal);
    setHidden(dom.terminalActions, !(terminal && selectedIsCurrent));
    if (terminal) {
      const outcome = summarizeOutcome(frames);
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
    const frames = selectRunFrames(state.frames, state.selectedVersionRunId);
    const selectedPhase = deriveVersionPhase(frames, state.run.phase);
    const selectedIsCurrent = (
      !state.selectedVersionRunId || state.selectedVersionRunId === state.run.runId
    );
    setText(dom["run-status"], PHASE_LABELS[selectedPhase]);
    const versionMeta = frames.find((frame) => frame.kind === "meta");
    renderChips(dom["run-policies"], versionMeta?.units ?? config.unitNames);
    setHidden(dom.abort, !(selectedIsCurrent && ACTIVE_PHASES.has(state.run.phase)));
    setHidden(dom["steer-form"], !(selectedIsCurrent && canSteer(state.run)));
    setHidden(dom["run-error"], selectedPhase !== "error");
    const versionError = [...frames].reverse().find((frame) => frame.kind === "error");
    setText(dom["run-error"], versionError?.message ?? (selectedIsCurrent ? state.run.error : null));
    setHidden(dom.errorActions, !(selectedIsCurrent && selectedPhase === "error"));
    renderVersions();
    const chatFrames = selectRunFrames(
      state.frames,
      state.selectedVersionRunId,
      { inheritFork: true },
    );
    renderChat(scenario, chatFrames);
    const canApprove = selectedIsCurrent && selectedPhase === "suspended";
    setHidden(dom.approval, !canApprove);
    if (canApprove) attachInlineAction(dom.approval, state.run.pendingId, "approval");
    const canRecover = selectedIsCurrent && selectedPhase === "recoverable";
    setHidden(dom.recovery, !canRecover);
    if (canRecover) attachInlineAction(dom.recovery, null, "recoverable");
    renderOutcome(frames, selectedPhase, selectedIsCurrent);
    renderRows(selectedPhase, selectedIsCurrent);
  }

  function renderVersions() {
    const versions = deriveRunVersions(state.frames);
    dom["version-switcher"].replaceChildren();
    setHidden(dom["version-switcher"], versions.length < 2);
    for (const version of versions) {
      const button = documentRef.createElement("button");
      button.type = "button";
      button.className = "version-option";
      button.textContent = version.label;
      button.setAttribute(
        "aria-pressed",
        String(version.runId === state.selectedVersionRunId),
      );
      button.addEventListener("click", () => {
        state.selectedVersionRunId = version.runId;
        state.selectedRowId = null;
        render();
      });
      dom["version-switcher"].append(button);
    }
  }

  function renderPolicyComposer() {
    const config = visibleConfig();
    const readOnly = ACTIVE_PHASES.has(state.run.phase);
    const policyReadOnly = !canEditPolicy(state.run);
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
        input.disabled = policyReadOnly;
        input.addEventListener("change", () => {
          if (!canEditPolicy(state.run)) return;
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
      state.selectedVersionRunId = frame.run_id;
    } else if (frame.kind === "suspended") {
      state.run = suspendRun(state.run, frame.pending_id);
    } else if (frame.kind === "recoverable") {
      state.run = markRecoverable(state.run);
    } else if (frame.kind === "outcome") {
      state.run = finishRun(
        state.run,
        frame.outcome?.stop_reason ?? frame.stop_reason ?? "completed",
      );
      // A finished scene loads the next one, so returning to the launch screen offers
      // the thing that follows rather than the thing just watched.
      const next = nextGuideStep(state.run.active, state.run.stopReason);
      if (next >= 0) {
        state.run = updateDraft(state.run, {
          scenarioId: GUIDE[next].scenarioId,
          unitNames: [...GUIDE[next].unitNames],
        });
      }
    } else if (frame.kind === "contended") {
      // Not this worker's run to finish. The lease is the answer, not a retry loop.
      state.run = failRun(state.run, frame.message ?? "다른 워커가 잡고 있습니다");
    } else if (frame.kind === "indeterminate") {
      // Not a failure. The run stopped because the ledger will not claim an effect it
      // cannot vouch for, and the next move is a person's.
      state.run = failRun(state.run, frame.message ?? "이 효과는 알 수 없습니다");
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
    state.forkEventIds.clear();
    state.selectedVersionRunId = null;
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
    } catch (error) {
      // A double-click while a continuation is in flight is benign; anything else
      // means the button was live over a run that cannot continue — show that.
      if (!state.run.continuationBusy) failCurrent(error);
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
      // Whatever is selected right now, not what was selected when the call parked.
      units: [...state.run.draft.unitNames],
    };
    await continueRun("/api/resume", body);
  }

  async function recover(retryRunning = true) {
    if (state.run.phase !== "recoverable") return;
    await continueRun("/api/recover", {
      run_id: state.run.runId,
      retry_running: retryRunning,
    });
  }

  async function forkSource(row) {
    const request = getEventForkRequest(state.run, row, state.rows);
    if (!request) return;
    // The request may retarget to an earlier tool boundary; mark where the branch
    // actually starts, not where the operator clicked.
    state.forkEventIds.add(request.event_id);
    try {
      state.run = beginFork(state.run);
    } catch (error) {
      failCurrent(error);
      return;
    }
    render();
    await stream("/api/fork", request);
  }

  async function abort() {
    if (!ACTIVE_PHASES.has(state.run.phase) || !state.run.runId) return;
    try {
      await post("/api/abort", { run_id: state.run.runId });
      if (state.run.phase !== "terminal") {
        state.frames.push({
          kind: "outcome",
          run_id: state.run.runId,
          outcome: { stop_reason: "aborted" },
        });
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
    state.frames.push({
      kind: "steer",
      run_id: state.run.runId,
      status: "queued",
      source: "operator",
      text,
    });
    render();
    try {
      await post("/api/steer", { run_id: state.run.runId, text });
    } catch (error) {
      failCurrent(error);
    }
  }

  function returnDraft() {
    if (!canStartRun(state.run)) return;
    state.run = returnToDraft(state.run, { source: "active" });
    state.frames = [];
    state.selectedRowId = null;
    state.forkEventIds.clear();
    state.selectedVersionRunId = null;
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
    dom.recover.addEventListener("click", () => void recover(true));
    dom["recover-safe"].addEventListener("click", () => void recover(false));
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
    dom.rerun.addEventListener("click", () => void runActive());
    dom["retry-run"].addEventListener("click", () => void runActive());
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
