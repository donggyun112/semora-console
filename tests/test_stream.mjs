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
import {
  reduceFrames,
  summarizeOutcome,
} from "../src/console/static/reducer.mjs";

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

const blockedInput = {
  to: "leaker@personal-mail.com",
  body: "secret",
};
const traceFrames = [
  { kind: "meta", run_id: "run-1" },
  {
    kind: "lifecycle",
    type: "pre_tool_use",
    payload: { name: "send_email" },
  },
  {
    kind: "agent",
    event: {
      type: "tool_call",
      id: "call-1",
      name: "send_email",
      input: blockedInput,
    },
  },
  {
    kind: "unit",
    unit: "dlp_block",
    verdict: "deny",
    message: "주민번호가 외부 주소로 나가는 요청",
  },
  {
    kind: "agent",
    event: {
      type: "tool_call",
      id: "call-1",
      name: "send_email",
      input: blockedInput,
      blocked: true,
    },
  },
];
const trace = reduceFrames(traceFrames);
assert.deepEqual(
  trace.map(({ kind, label, summary, verdict }) => ({
    kind,
    label,
    summary,
    verdict,
  })),
  [
    {
      kind: "lifecycle",
      label: "pre_tool_use",
      summary: "send_email",
      verdict: null,
    },
    {
      kind: "tool",
      label: "send_email",
      summary: "실행 전 거부",
      verdict: "DENY",
    },
    {
      kind: "policy",
      label: "dlp_block",
      summary: "주민번호가 외부 주소로 나가는 요청",
      verdict: "DENY",
    },
  ],
);
assert.deepEqual(trace[1].details.input, blockedInput);
assert.ok(
  trace.every((row) => !row.summary.includes("leaker@personal-mail.com")),
  "raw tool arguments stay out of trace summaries",
);
assert.deepEqual(
  summarizeOutcome([
    ...traceFrames,
    { kind: "outcome", outcome: { stop_reason: "completed" } },
  ]),
  { verdict: "DENY", tool: "send_email", result: "실행 안 됨" },
);

const aggregated = reduceFrames([
  { kind: "agent", event: { type: "text", text: "A" } },
  { kind: "agent", event: { type: "text", text: "B" } },
]);
assert.equal(aggregated.length, 1);
assert.deepEqual(
  {
    kind: aggregated[0].kind,
    label: aggregated[0].label,
    summary: aggregated[0].summary,
    output: aggregated[0].details.output,
  },
  { kind: "agent", label: "agent", summary: "응답 생성", output: "AB" },
);

const ordered = reduceFrames([
  { kind: "agent", event: { type: "text", text: "A" } },
  { kind: "lifecycle", type: "pre_tool_use", payload: { name: "lookup" } },
  {
    kind: "agent",
    event: { type: "tool_call", id: "lookup-1", name: "lookup", input: {} },
  },
]);
assert.deepEqual(
  ordered.map(({ kind }) => kind),
  ["agent", "lifecycle", "tool"],
  "lifecycle hooks emitted during text generation remain ahead of their tool",
);

const resultRows = reduceFrames([
  {
    kind: "agent",
    event: {
      type: "tool_result",
      name: "lookup",
      output: { found: true },
      execution_count: 2,
    },
  },
]);
assert.deepEqual(
  {
    kind: resultRows[0].kind,
    summary: resultRows[0].summary,
    output: resultRows[0].details.output,
    executionCount: resultRows[0].details.executionCount,
  },
  {
    kind: "result",
    summary: "실행 완료",
    output: { found: true },
    executionCount: 2,
  },
);

const operationalRows = reduceFrames([
  { kind: "steer", status: "queued", source: "operator", text: "중단" },
  { kind: "steer", status: "admitted", source: "policy", text: "계속" },
  { kind: "recoverable", message: "worker lost" },
  { kind: "outcome", outcome: { stop_reason: "completed" } },
]);
assert.deepEqual(
  operationalRows.map(({ kind, summary }) => ({ kind, summary })),
  [
    { kind: "steer", summary: "지시 대기" },
    { kind: "steer", summary: "지시 반영" },
    { kind: "recovery", summary: "worker lost" },
    { kind: "outcome", summary: "completed" },
  ],
);

console.log("run inspector state ok");
