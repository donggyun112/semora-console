import { createNdjsonReader } from "./ndjson.mjs";
import {
  reduceFrames,
  resultBadges,
  summarizeOutcome,
  toolResultOutput,
} from "./reducer.mjs";
import {
  acceptsStreamEnd,
  attachBranchId,
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
  replayStream,
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

// The demo offers eighty combinations and no order to read them in. These are the
// order, and each one only makes sense after the one before it: what the agent does
// unguarded, what one gate changes, what a person deciding costs, what survives the
// worker dying, what nobody can decide at all, and what the record keeps regardless.
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
    scenarioId: "leak", unitNames: Object.freeze(["approval"]),
    label: "승인 후 재검증", teaches: "멈춘 채 dlp_block 을 켜고 승인하면 거부된다",
    then: "정책에서 dlp_block 을 켠 다음 승인을 누르세요.",
  }),
  Object.freeze({
    scenarioId: "crash", unitNames: Object.freeze(["approval"]),
    label: "대기 중 장애", teaches: "복구해도 청구는 한 번",
  }),
  Object.freeze({
    scenarioId: "unknown_effect", unitNames: Object.freeze([]),
    label: "청구 도중 장애", teaches: "나갔는지 아무도 모른다",
  }),
  Object.freeze({
    scenarioId: "fork_masking", unitNames: Object.freeze(["pii_mask"]),
    label: "마스킹 후 분기", teaches: "정책을 끄고 되감으면 원본이 돌아온다",
    then: "끝난 뒤 pii_mask 를 끄고 입력에서 분기해 보세요.",
  }),
]);


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

export function policiesAt(resumesAt, unitNames, plan) {
  // Which of these policies a branch resuming at `resumesAt` would run over the
  // recorded round. The coordinate says where the runtime picks up, the framework's
  // table says which control points run again from there, and each unit says which
  // point it lives at — nothing here is read off a name.
  const points = plan?.reruns?.[resumesAt] ?? [];
  const applies = [];
  const skipped = [];
  for (const name of unitNames ?? []) {
    const point = plan?.points?.get(name);
    (point && points.includes(point) ? applies : skipped).push(name);
  }
  return { applies, skipped };
}

function unitsChanged(runState) {
  const before = new Set(runState?.active?.unitNames ?? []);
  const after = new Set(runState?.draft?.unitNames ?? []);
  return [...before, ...after].filter((name) => before.has(name) !== after.has(name));
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

// The arguments the model proposed for the parked call, as the approval form's
// starting text. The tool_call event is the only frame that carries them.
export function pendingCallArgs(frames, pendingId) {
  for (const frame of frames) {
    const event = frame?.kind === "agent" ? frame.event : null;
    if (event?.type === "tool_call" && event.id === pendingId) return event.input ?? {};
  }
  return null;
}

// null when the operator left the arguments alone; throws on JSON that is not an object.
export function editedArgs(text, original) {
  const args = JSON.parse(text);
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    throw new SyntaxError("인자는 JSON 객체여야 합니다.");
  }
  return JSON.stringify(args) === JSON.stringify(original) ? null : args;
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
      branchId: payload.branch_id,
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
  const branchIds = [];
  const unitsByRun = new Map();
  for (const frame of frames) {
    if (frame?.kind !== "meta" || !frame.branch_id || seen.has(frame.branch_id)) continue;
    seen.add(frame.branch_id);
    branchIds.push(frame.branch_id);
    if (Array.isArray(frame.units)) unitsByRun.set(frame.branch_id, frame.units);
  }
  return branchIds.map((branchId, index) => ({
    branchId,
    number: index + 1,
    label: `v${index + 1} · ${index === 0 ? "원본" : "분기"}${versionPolicySuffix(unitsByRun.get(branchId))}`,
  }));
}

function selectVersionChatFrames(frames, branchId, ancestors = new Set()) {
  const directFrames = frames.filter((frame) => frame?.branch_id === branchId);
  const meta = directFrames.find((frame) => frame?.kind === "meta");
  const parentBranchId = meta?.fork_parent;
  if (!parentBranchId || ancestors.has(branchId)) return directFrames;

  const nextAncestors = new Set(ancestors);
  nextAncestors.add(branchId);
  const parentFrames = selectVersionChatFrames(frames, parentBranchId, nextAncestors);
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

export function selectRunFrames(frames, branchId, options = {}) {
  if (!branchId) return frames;
  if (options.inheritFork) return selectVersionChatFrames(frames, branchId);
  return frames.filter((frame) => frame?.branch_id === branchId);
}

function rowFingerprint(row) {
  // callId first: three charge_card gates share kind/label/summary, and matching
  // the first one put the child's replay of event 1 onto a branch from event 2.
  return [row.callId ?? "", row.kind, row.label, row.summary].join("\u0000");
}

function parallelBatchStart(rows, forkIndex) {
  const fork = rows[forkIndex];
  if (!fork?.callId) return null;
  const callStart = rows.findIndex((row) => row.callId === fork.callId);
  if (callStart < 0 || callStart > forkIndex) return null;
  const parallel = rows.slice(callStart, forkIndex).some(
    (row) => row.callId && row.callId !== fork.callId,
  );
  if (!parallel) return null;
  let start = callStart;
  for (let i = callStart - 1; i >= 0; i -= 1) {
    if (rows[i].kind === "tool" || rows[i].callId) start = i;
    else break;
  }
  return start;
}

function childBatchStart(rows) {
  const index = rows.findIndex((row) => row.kind === "tool" || row.callId);
  return index < 0 ? 0 : index;
}

export function deriveVersionRows(frames, branchId, ancestors = new Set()) {
  const directFrames = selectRunFrames(frames, branchId);
  const directRows = reduceFrames(directFrames).map((row) => ({
    ...row,
    id: `${branchId ?? "run"}:${row.id}`,
    versionOrigin: "current",
    forkStart: false,
  }));
  const meta = directFrames.find((frame) => frame?.kind === "meta");
  const parentBranchId = meta?.fork_parent;
  if (!branchId || !parentBranchId || ancestors.has(branchId)) return directRows;

  const nextAncestors = new Set(ancestors);
  nextAncestors.add(branchId);
  const parentRows = deriveVersionRows(frames, parentBranchId, nextAncestors);
  const forkIndex = parentRows.findIndex((row) => row.eventId === meta.fork_event_id);
  if (forkIndex < 0) return directRows;

  const after = meta.fork_edge === "after";
  let prefixEnd = forkIndex + (after ? 1 : 0);
  const selectedFingerprint = rowFingerprint(parentRows[forkIndex]);
  let childStart = directRows.findIndex(
    (row) => rowFingerprint(row) === selectedFingerprint,
  );
  let matchedChild = childStart >= 0;
  if (childStart < 0) childStart = Math.min(prefixEnd, directRows.length);
  else if (after) childStart += 1;
  // Parallel tools emit every call before any gate. Cutting at the clicked
  // pre kept v1's whole batch in the prefix, then drew the child batch again.
  if (!after) {
    const batchStart = parallelBatchStart(parentRows, forkIndex);
    if (batchStart !== null) {
      prefixEnd = batchStart;
      childStart = childBatchStart(directRows);
      matchedChild = true;
    }
  }

  const inherited = parentRows.slice(0, prefixEnd).map((row) => ({
    ...row,
    versionOrigin: "inherited",
    forkStart: false,
  }));
  // Leaf used to append the whole child stream so a short continuation stayed
  // intact. That also stacked CALL REPLAY of 1-2-3 after the parent prefix.
  // When the child actually re-emits the fork row, cut there like input forks.
  const current = meta.fork_mode === "leaf" && !matchedChild
    ? directRows.filter((row) => row.label !== "session_start")
    : directRows.slice(childStart);
  if (current.length) current[0] = { ...current[0], forkStart: true };
  return [...inherited, ...current];
}

export function isForkRepresentative(rows, index) {
  // One button per boundary, on the event it actually branches from. A boundary spans
  // several rows and only its last one carries the coordinate: a tool boundary is the
  // gate, not the call the model made. Putting the button on the tool's name read
  // better and said something untrue about where the run resumes.
  const members = forkSeam(rows, index);
  return members !== null && index === members[members.length - 1];
}

function forkSeam(rows, index) {
  const row = rows?.[index];
  if (!row?.forkable) return null;
  const members = [];
  for (let at = 0; at < rows.length; at += 1) {
    const other = rows[at];
    if (!other.forkable) continue;
    // Seam and name together: a replayed call adds no transcript entry, so its before
    // and after boundaries restore to one leaf while staying two places to branch from.
    const same = other.seam != null
      ? other.seam === row.seam && other.boundary === row.boundary
      : other === row;
    if (same) members.push(at);
  }
  return members.length ? members : [index];
}

const BOUNDARY_LABELS = {
  input: "이 입력에서 분기",
  tool: "툴 실행 전 분기",
  result: "툴 결과에서 분기",
};

export function getForkActionLabel(row, policies, mode = null) {
  // A result row can only branch to where the transcript has a leaf, and that leaf holds
  // the result a journal unit already rewrote. Change one and there is nothing at this
  // coordinate to change it on, so the branch moves back to the call — which the button
  // has to say, because "결과에서 분기" would be describing a boundary it is leaving.
  // When only the journal changed, the call is not re-gated either: the recorded result
  // is journaled again and nothing is asked of anyone.
  const name = mode === "rejournal"
    ? "기록된 결과에 새 정책만 적용"
    : mode === "retarget"
      ? "저장된 결과를 버리고 툴부터 다시"
      : BOUNDARY_LABELS[row?.boundary] ?? "이 지점에서 분기";
  // The policies by name, split by whether this branch reaches them. A count said how
  // many were selected; what a person deciding where to branch needs is which of them
  // will run from here.
  const applies = policies?.applies ?? [];
  const skipped = policies?.skipped ?? [];
  const parts = [name, `적용 ${applies.length ? applies.join(", ") : "없음"}`];
  if (skipped.length) parts.push(`건너뜀 ${skipped.join(", ")}`);
  return parts.join(" · ");
}

export function isRetargetedFork(row, request) {
  // The branch starts somewhere other than the row it is offered on.
  return Boolean(request && row?.eventId && request.event_id !== row.eventId);
}

// A run the ledger stopped: it will not claim an effect it cannot vouch for, it is not
// this worker's turn, or someone else holds the lease. `apply` parks all three the same
// way, and this has to agree — read off the frames instead, they showed as 실행 중 with no
// action, which is the one thing that is certainly untrue.
const LEDGER_STOPS = new Set(["indeterminate", "fenced", "contended"]);

export function deriveVersionPhase(frames, fallback = "idle") {
  for (const frame of [...frames].reverse()) {
    if (frame?.kind === "outcome") return "terminal";
    if (frame?.kind === "error" || LEDGER_STOPS.has(frame?.kind)) return "error";
    if (frame?.kind === "suspended") return "suspended";
    if (frame?.kind === "recoverable") return "recoverable";
    if (frame?.kind === "meta") return "streaming";
  }
  return fallback;
}

function forkTarget(runState, row, plan) {
  // Where a branch from this row goes, and how it resumes there.
  const changed = unitsChanged(runState);
  let target = {
    eventId: row.eventId,
    edge: row.forkEdge ?? "before",
    resumesAt: row.resumesAt ?? null,
    rejournalAt: row.rejournalAt ?? null,
  };
  if (row.rebuild) {
    // A policy the operator just changed that this coordinate would not reach, but the
    // coordinate that makes the boundary again would, moves the branch there: a
    // recorded result restores as a result, and a journal unit has nothing to rewrite
    // on it unless the tool round runs again.
    const missed = policiesAt(row.resumesAt, changed, plan).skipped;
    const reached = policiesAt(row.rebuild.resumes_at, missed, plan).applies;
    if (reached.length) {
      target = {
        eventId: row.rebuild.event_id,
        edge: row.rebuild.edge,
        resumesAt: row.rebuild.resumes_at ?? null,
        rejournalAt: row.rebuild.rejournal_at ?? null,
      };
    }
  }
  // Nothing that changed lives at the gate, and the target has a recorded result to
  // journal again: skip the gate. Re-masking a result should not re-ask for approval
  // of an effect that already happened.
  const rejournal = Boolean(target.rejournalAt)
    && changed.length > 0
    && policiesAt(target.rejournalAt, changed, plan).skipped.length === 0;
  return { target, changed, rejournal };
}

export function getEventForkRequest(runState, row, plan = null) {
  if (
    runState?.phase !== "terminal" ||
    !runState?.branchId ||
    !row?.eventId ||
    !row?.forkable
  ) {
    return null;
  }
  const { target, rejournal } = forkTarget(runState, row, plan);
  return {
    branch_id: row.branchId ?? runState.branchId,
    event_id: target.eventId,
    edge: target.edge,
    units: [...runState.draft.unitNames],
    rejournal,
  };
}

export function describeFork(runState, row, plan) {
  // Everything one branch button says, from one place: where the request goes, how it
  // resumes there, and which of the selected policies it will run once there.
  const request = getEventForkRequest(runState, row, plan);
  if (!request) return null;
  const { target, rejournal } = forkTarget(runState, row, plan);
  const retargeted = isRetargetedFork(row, request);
  const resumesAt = rejournal ? target.rejournalAt : target.resumesAt;
  return {
    request,
    retargeted,
    mode: rejournal ? "rejournal" : retargeted ? "retarget" : null,
    resumesAt: resumesAt ?? null,
    policies: policiesAt(resumesAt, request.units, plan),
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
    "rerun", "retry-run", "return-draft", "trace", "guide", "guide-note",
    "details-drawer", "details-close", "details-copy", "details-title",
    "details-body", "steer-form", "steer-text", "policy-drawer", "policy-close",
    "units", "compose-summary", "approval", "approval-args", "approve", "deny",
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
    reruns: {},
    frames: [],
    rows: [],
    selectedRowId: null,
    abortCtl: null,
    steers: [],
    policyActivity: new Map(),
    booted: false,
    // True only while a reload is replaying a run the server kept, so the frame handler
    // can tell a restored run from one that is happening now.
    restoring: false,
    policyOpener: null,
    forkEventIds: new Set(),
    selectedVersionBranchId: null,
    chatToolNodes: new Map(),
  };

  const RUN_KEY = "semora-console:run";

  function rememberRun(branchId) {
    // The run id is the whole of what a reload loses: the ledger holds the run, the
    // conversation holds its frames, and the server rebuilds a session it never saw from
    // either. Only which run this browser was watching lives nowhere else.
    try {
      if (branchId) globalThis.localStorage?.setItem(RUN_KEY, branchId);
      else globalThis.localStorage?.removeItem(RUN_KEY);
    } catch {
      // Storage refused: the run simply is not offered back after a reload.
    }
  }

  function rememberedRun() {
    try {
      return globalThis.localStorage?.getItem(RUN_KEY) ?? "";
    } catch {
      return "";
    }
  }

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
      // Label only. What each scene teaches belongs to the one being read, not to six
      // cards at once — six of those was a second row, and a second row pushed the
      // thing you press off the screen.
      button.textContent = `${index + 1}. ${scene.label}`;
      button.title = scene.teaches;
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
    const scene = GUIDE[at];
    setText(
      dom["guide-note"],
      scene ? [scene.teaches, scene.then].filter(Boolean).join(" · ") : "",
    );
    setHidden(dom["guide-note"], !scene);
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
    state.rows = deriveVersionRows(state.frames, state.selectedVersionBranchId);
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
      const fork = isForkRepresentative(state.rows, index)
        ? describeFork(state.run, row, forkPlan())
        : null;
      if (fork) {
        const { mode, policies } = fork;
        const action = documentRef.createElement("div");
        action.className = "trace-fork";
        const button = documentRef.createElement("button");
        button.type = "button";
        button.textContent = getForkActionLabel(row, policies, mode);
        button.title = `${fork.resumesAt ?? "?"}부터 다시 실행`;
        button.setAttribute("aria-label", `${index + 1}번 이벤트에서 다시 실행`);
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
    const frames = selectRunFrames(state.frames, state.selectedVersionBranchId);
    const selectedPhase = deriveVersionPhase(frames, state.run.phase);
    const selectedIsCurrent = (
      !state.selectedVersionBranchId || state.selectedVersionBranchId === state.run.branchId
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
      state.selectedVersionBranchId,
      { inheritFork: true },
    );
    renderChat(scenario, chatFrames);
    const canApprove = selectedIsCurrent && selectedPhase === "suspended";
    setHidden(dom.approval, !canApprove);
    if (canApprove) {
      attachInlineAction(dom.approval, state.run.pendingId, "approval");
      const args = dom["approval-args"];
      // Seed once per parked call; a re-render must not overwrite an edit in progress.
      if (args.dataset.pendingId !== state.run.pendingId) {
        args.dataset.pendingId = state.run.pendingId;
        args.value = JSON.stringify(pendingCallArgs(state.frames, state.run.pendingId) ?? {}, null, 2);
        args.setCustomValidity("");
        // The card lands at the bottom of a scrolling thread; bring its buttons into view once.
        dom.approval.scrollIntoView?.({ block: "nearest" });
      }
    }
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
        String(version.branchId === state.selectedVersionBranchId),
      );
      button.addEventListener("click", () => {
        state.selectedVersionBranchId = version.branchId;
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
      state.run = attachBranchId(state.run, frame.branch_id);
      state.selectedVersionBranchId = frame.branch_id;
      if (!state.restoring) rememberRun(frame.branch_id);
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
    } else if (frame.kind === "fenced") {
      // The run moved on while this worker was away. Its writes are refused, which is
      // the whole point: a worker back from the dead does not get to finish.
      state.run = failRun(state.run, frame.message ?? "이 워커의 차례는 지났습니다");
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
    state.selectedVersionBranchId = null;
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
      branch_id: state.run.branchId,
      pending_id: state.run.pendingId,
      approved,
      // Whatever is selected right now, not what was selected when the call parked.
      units: [...state.run.draft.unitNames],
    };
    if (approved) {
      const field = dom["approval-args"];
      try {
        const args = editedArgs(field.value, pendingCallArgs(state.frames, state.run.pendingId));
        if (args) body.args = args;
        field.setCustomValidity("");
      } catch (error) {
        field.setCustomValidity(error.message);
        field.reportValidity();
        return;
      }
    }
    await continueRun("/api/resume", body);
  }

  async function recover() {
    if (state.run.phase !== "recoverable") return;
    await continueRun("/api/recover", { branch_id: state.run.branchId });
  }

  function forkPlan() {
    return {
      points: new Map([...state.unitsMeta].map(([name, unit]) => [name, unit.point])),
      reruns: state.reruns,
    };
  }

  async function forkSource(row) {
    const request = getEventForkRequest(state.run, row, forkPlan());
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
    if (!ACTIVE_PHASES.has(state.run.phase) || !state.run.branchId) return;
    try {
      await post("/api/abort", { branch_id: state.run.branchId });
      if (state.run.phase !== "terminal") {
        state.frames.push({
          kind: "outcome",
          branch_id: state.run.branchId,
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
    const queued = {
      kind: "steer",
      branch_id: state.run.branchId,
      status: "queued",
      source: "operator",
      text,
    };
    state.frames.push(queued);
    render();
    try {
      const response = await post("/api/steer", { branch_id: state.run.branchId, text });
      // The server says when the loop will next drain, which is the only thing the
      // operator cannot tell from here.
      queued.admits = (await response.json())?.admits ?? null;
      render();
    } catch (error) {
      failCurrent(error);
    }
  }

  function returnDraft() {
    if (!canStartRun(state.run)) return;
    rememberRun(null);
    state.run = returnToDraft(state.run, { source: "active" });
    state.frames = [];
    state.selectedRowId = null;
    state.forkEventIds.clear();
    state.selectedVersionBranchId = null;
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
    // The wordmark is home. It is a link to "/", and a reload now brings the remembered
    // run straight back — so going home has to mean leaving the run on purpose, which
    // only a run that is not waiting on the operator may do. A parked or streaming run
    // is left through its own buttons; the pointer goes to the one that ends it.
    documentRef.querySelector(".brand")?.addEventListener("click", (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
      event.preventDefault();
      if (canStartRun(state.run)) returnDraft();
      else dom.abort.focus();
    });
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
    dom.rerun.addEventListener("click", () => void runActive());
    dom["retry-run"].addEventListener("click", () => void runActive());
    dom["return-draft"].addEventListener("click", returnDraft);
    dom["boot-retry"].addEventListener("click", () => void boot());
  }

  async function restoreRun() {
    // Everything the run was is on the server; the browser only asks for it back. Frames
    // are replayed through the same handler the stream uses, so a restored approval, a
    // parked recovery and a branch point are the live ones rather than a second rendering
    // of them.
    const branchId = rememberedRun();
    if (!branchId || !canStartRun(state.run)) return;
    let kept = null;
    try {
      const response = await fetchRef(`/api/branches/${encodeURIComponent(branchId)}/frames`);
      if (!response.ok) throw new Error(String(response.status));
      kept = await response.json();
    } catch {
      // The ledger no longer knows this run — a wiped volume, or a demo restarted clean.
      rememberRun(null);
      return;
    }
    if (!kept.frames?.length) {
      rememberRun(null);
      return;
    }
    state.restoring = true;
    try {
      state.run = updateDraft(state.run, {
        scenarioId: kept.scenario_id ?? state.run.draft.scenarioId,
        unitNames: [...(kept.units ?? [])],
      });
      state.run = startRun(state.run);
      let streams = 0;
      for (const frame of kept.frames) {
        if (frame?.kind === "meta") {
          // Every request after the first began from wherever the previous one ended,
          // and the button that began it is not here to say so.
          if (streams > 0) state.run = replayStream(state.run, frame.units ?? null);
          streams += 1;
        }
        handleFrame(frame);
      }
    } catch (error) {
      // A run this page cannot rebuild must not be shown half-rebuilt: a parked-looking
      // run with a 복구 button the ledger will refuse is worse than the launch screen.
      // The id stays remembered; a later page may know how to replay it.
      console.error("restore failed", error);
      state.run = createRunState();
      state.frames = [];
      state.rows = [];
      state.selectedVersionBranchId = null;
    } finally {
      state.restoring = false;
    }
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
      state.reruns = unitPayload.reruns ?? {};
      if (!state.booted) bind();
      state.booted = true;
      await restoreRun();
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
  globalThis.__SEMORA_CONSOLE__ = consoleApp;
  void consoleApp.boot();
}
