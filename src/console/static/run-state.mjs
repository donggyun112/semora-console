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

export function beginFork(state) {
  if (
    state.phase !== "terminal" ||
    !state.runId
  ) {
    throw new Error("no forkable source run");
  }
  const unitNames = state.active.scenarioId === "fork_masking"
    ? state.draft.unitNames.filter((name) => name !== "input_mask")
    : [...state.draft.unitNames];
  return {
    ...state,
    phase: "streaming",
    active: {
      ...state.active,
      unitNames,
    },
    runId: null,
    stopReason: null,
    error: null,
  };
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
