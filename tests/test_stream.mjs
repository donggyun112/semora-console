import assert from "node:assert/strict";

import {
  NdjsonParseError,
  createNdjsonReader,
} from "../src/console/static/ndjson.mjs";
import {
  DEFAULT_DRAFT,
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
} from "../src/console/static/run-state.mjs";

const idle = createRunState();
assert.deepEqual(idle, {
  phase: "idle",
  draft: { scenarioId: "leak", unitNames: ["approval", "dlp_block"] },
  active: null,
  runId: null,
  pendingId: null,
  continuationBusy: false,
  stopReason: null,
  error: null,
});
assert.deepEqual(DEFAULT_DRAFT, {
  scenarioId: "leak",
  unitNames: ["approval", "dlp_block"],
});
assert.notStrictEqual(
  createRunState().draft.unitNames,
  createRunState().draft.unitNames,
  "each run state owns its draft unit array",
);

const edited = updateDraft(idle, { scenarioId: "baseline", unitNames: [] });
assert.equal(idle.draft.scenarioId, "leak");
assert.deepEqual(edited.draft, { scenarioId: "baseline", unitNames: [] });

const streaming = attachRunId(startRun(idle), "run-1");
assert.deepEqual(streaming.active, {
  scenarioId: "leak",
  unitNames: ["approval", "dlp_block"],
});
assert.equal(canStartRun(streaming), false);
assert.equal(canEditDraft(streaming), false);
assert.equal(canSteer(streaming), true);

const suspended = suspendRun(streaming, "pending-1");
assert.equal(suspended.phase, "suspended");
assert.equal(canStartRun(suspended), false);
assert.equal(canSteer(suspended), false);
assert.equal(acceptsStreamEnd(suspended), true);

const resuming = beginContinuation(suspended);
assert.equal(resuming.phase, "streaming");
assert.equal(resuming.continuationBusy, true);
assert.equal(resuming.runId, "run-1");
assert.equal(resuming.pendingId, "pending-1");
assert.throws(() => beginContinuation(resuming), /continuation already active/);

const recoverable = markRecoverable(streaming);
assert.equal(recoverable.phase, "recoverable");
assert.equal(canStartRun(recoverable), false);
assert.equal(acceptsStreamEnd(recoverable), true);

const terminal = finishRun(streaming, "completed");
assert.equal(terminal.phase, "terminal");
assert.equal(terminal.stopReason, "completed");
assert.equal(canStartRun(terminal), true);

const failed = failRun(streaming, "truncated stream");
assert.equal(failed.phase, "error");
assert.equal(failed.error, "truncated stream");
assert.equal(acceptsStreamEnd(failed), true);

const retryDraft = returnToDraft(failed, {
  source: "active",
  withoutPolicies: true,
});
assert.deepEqual(retryDraft.draft, {
  scenarioId: "leak",
  unitNames: [],
});
assert.equal(retryDraft.phase, "idle");
assert.throws(
  () => updateDraft(suspended, { scenarioId: "other" }),
  /draft locked/,
);

const frames = [];
const ndjson = createNdjsonReader((frame) => frames.push(frame));
ndjson.push(
  new TextEncoder().encode(
    '{"kind":"agent","event":{"type":"text","text":"청구"}}\n' +
      '{"kind":"outcome","outcome":{"stop_reason":"completed"}}',
  ),
);
ndjson.end();
assert.equal(frames.length, 2, "valid trailing frame is delivered");
assert.equal(frames[0].event.text, "청구");

const hangul = [];
const splitBytes = new TextEncoder().encode(
  '{"kind":"agent","event":{"type":"text","text":"거부"}}\n',
);
const splitReader = createNdjsonReader((frame) => hangul.push(frame));
splitReader.push(splitBytes.slice(0, splitBytes.length - 2));
splitReader.push(splitBytes.slice(splitBytes.length - 2));
splitReader.end();
assert.equal(hangul[0].event.text, "거부");

const malformed = createNdjsonReader(() => {});
assert.throws(
  () => malformed.push(new TextEncoder().encode('{"kind":}\n')),
  (error) =>
    error instanceof NdjsonParseError &&
    error.line === '{"kind":}',
);

const truncated = createNdjsonReader(() => {});
truncated.push(new TextEncoder().encode('{"kind":"agent"'));
assert.throws(
  () => truncated.end(),
  (error) =>
    error instanceof NdjsonParseError &&
    error.message.includes("trailing"),
);

console.log("run inspector state ok");
