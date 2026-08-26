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
