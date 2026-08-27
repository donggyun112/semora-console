import assert from "node:assert/strict";

import {
  deriveBranchView,
  deriveChatView,
  deriveRunVersions,
  deriveVersionPhase,
  deriveVersionRows,
  getForkActionLabel,
  getEventForkRequest,
  getLaunchCopy,
  selectRunFrames,
} from "../src/console/static/app.js";
import {
  NdjsonParseError,
  createNdjsonReader,
} from "../src/console/static/ndjson.mjs";
import {
  DEFAULT_DRAFT,
  acceptsStreamEnd,
  attachRunId,
  beginFork,
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

assert.deepEqual(
  getLaunchCopy({
    title: "고객 데이터 처리",
    does: "고객을 조회해 청구팀에 요약한다",
    prompt: "read_customer로 고객을 조회해줘.",
  }),
  {
    title: "고객 데이터 처리",
    prompt: "read_customer로 고객을 조회해줘.",
  },
  "launch copy uses the scenario name and request without a narrator sentence",
);

assert.deepEqual(
  deriveChatView("고객을 조회하고 메일로 보내줘.", [
    {
      kind: "agent",
      event: {
        type: "tool_call",
        id: "call-read",
        name: "read_customer",
        input: { customer_id: "c-001" },
      },
    },
    {
      kind: "agent",
      event: {
        type: "tool_result",
        id: "call-read",
        name: "read_customer",
        executed: true,
        result: { type: "text", text: "customer found" },
      },
    },
    {
      kind: "agent",
      event: {
        type: "tool_call",
        id: "call-send",
        name: "send_email",
        input: { to: "outside@example.com" },
      },
    },
    {
      kind: "unit",
      unit: "dlp_block",
      verdict: "deny",
      message: "메일 본문에 개인정보가 있습니다",
    },
    {
      kind: "agent",
      event: {
        type: "tool_call",
        id: "call-send",
        name: "send_email",
        blocked: true,
      },
    },
    { kind: "agent", event: { type: "text", text: "메일 전송이 " } },
    { kind: "agent", event: { type: "text", text: "차단됐습니다." } },
  ]),
  {
    user: { role: "user", text: "고객을 조회하고 메일로 보내줘." },
    assistant: {
      role: "assistant",
      text: "메일 전송이 차단됐습니다.",
      tools: [
        {
          id: "call-read",
          name: "read_customer",
          status: "completed",
          summary: "실행 완료",
          reason: null,
        },
        {
          id: "call-send",
          name: "send_email",
          status: "blocked",
          summary: "정책으로 차단",
          reason: "메일 본문에 개인정보가 있습니다",
        },
      ],
    },
  },
  "chat view turns protocol frames into one user turn and one assistant turn",
);

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

const forkSourceTerminal = finishRun(
  attachRunId(
    startRun(updateDraft(createRunState(), {
      scenarioId: "fork_masking",
      unitNames: ["input_mask", "dlp_block"],
    })),
    "run-b",
  ),
  "completed",
);
const forking = beginFork(forkSourceTerminal);
assert.deepEqual(forking.active.unitNames, ["dlp_block"]);
assert.equal(forking.phase, "streaming");
assert.equal(forking.runId, null);
const genericForking = beginFork(terminal);
assert.deepEqual(genericForking.active.unitNames, ["approval", "dlp_block"]);
assert.equal(genericForking.phase, "streaming");
const editedGenericTerminal = updateDraft(terminal, { unitNames: ["rate_cap"] });
assert.deepEqual(beginFork(editedGenericTerminal).active.unitNames, ["rate_cap"]);
const editedMaskingTerminal = updateDraft(forkSourceTerminal, {
  unitNames: ["input_mask", "dlp_block", "approval"],
});
assert.deepEqual(beginFork(editedMaskingTerminal).active.unitNames, [
  "dlp_block",
  "approval",
]);
assert.deepEqual(
  getEventForkRequest(editedMaskingTerminal, { eventId: "event-09" }),
  null,
  "an observation-only event does not advertise an exact restore point",
);
assert.deepEqual(
  getEventForkRequest(editedMaskingTerminal, {
    eventId: "event-03",
    forkable: true,
    forkEdge: "before",
  }),
  {
    run_id: "run-b",
    event_id: "event-03",
    edge: "before",
    units: ["dlp_block", "approval"],
  },
  "an event-row fork preserves the selected durable event coordinate",
);
assert.equal(
  getEventForkRequest(streaming, { eventId: "event-09" }),
  null,
  "an in-flight run cannot start an event fork",
);
assert.deepEqual(
  getEventForkRequest(editedGenericTerminal, {
    eventId: "event-generic",
    forkable: true,
    forkEdge: "before",
  }),
  {
    run_id: "run-1",
    event_id: "event-generic",
    edge: "before",
    units: ["rate_cap"],
  },
  "completed runs expose event forks for every scenario",
);
const secondVersion = finishRun(
  attachRunId(beginFork(editedGenericTerminal), "run-2"),
  "completed",
);
assert.deepEqual(
  getEventForkRequest(secondVersion, {
    eventId: "event-from-v1",
    runId: "run-1",
    forkable: true,
    forkEdge: "before",
  }, true),
  {
    run_id: "run-1",
    event_id: "event-from-v1",
    edge: "before",
    units: ["rate_cap"],
  },
  "an older selected version remains forkable after newer versions exist",
);

assert.deepEqual(
  deriveBranchView([
    { kind: "lifecycle", type: "branch_snapshot", payload: {
      branch: "source", run_id: "run-b", active: true,
      messages: [{ role: "user", content: "ssn is ***" }],
    } },
    { kind: "lifecycle", type: "branch_snapshot", payload: {
      branch: "fork", run_id: "run-c", active: true,
      messages: [{ role: "user", content: "ssn is 123-45" }],
    } },
  ]),
  [
    {
      branch: "source", runId: "run-b", active: false,
      messages: [{ role: "user", content: "ssn is ***" }],
    },
    {
      branch: "fork", runId: "run-c", active: true,
      messages: [{ role: "user", content: "ssn is 123-45" }],
    },
  ],
);

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
    event_id: "event-pre-tool",
    fork_origin_id: "prompt-2",
    payload: { name: "send_email" },
  },
  {
    kind: "agent",
    event_id: "event-tool-request",
    fork_origin_id: "prompt-2",
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
    event_id: "event-tool-blocked",
    fork_origin_id: "prompt-2",
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
assert.equal(trace[0].eventId, "event-pre-tool");
assert.equal(trace[0].forkOriginId, "prompt-2");
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
      summary: "실행 안 됨",
      verdict: null,
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
assert.equal(
  trace[1].eventId,
  "event-tool-request",
  "a consolidated blocked tool keeps the coordinate where its visible row began",
);
assert.equal(
  trace.filter((row) => row.verdict === "DENY").length,
  1,
  "one policy denial produces one DENY badge",
);
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
  { kind: "steer", phase: "admitted", source: "control", text: "계속" },
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
assert.deepEqual(
  operationalRows.slice(0, 2).map(({ label }) => label),
  ["운영자", "정책"],
);

const resumedApprovalRows = reduceFrames([
  {
    kind: "lifecycle", type: "pre_tool_use",
    payload: { call_id: "charge-1", name: "charge_card", source: "pre_tool_use" },
  },
  {
    kind: "lifecycle", type: "pre_tool_use",
    payload: { call_id: "charge-1", name: "charge_card", source: "on_resume" },
  },
]);
assert.deepEqual(
  resumedApprovalRows.map(({ summary }) => summary),
  ["charge_card", "승인 후 재검증 · charge_card"],
  "the resumed permission check is distinguishable from the original gate",
);

const sequentialChargeRows = reduceFrames([
  {
    kind: "agent",
    event: {
      type: "tool_call", id: "charge-1", name: "charge_card",
      input: { customer_id: "c-001", amount: 10 },
    },
  },
  {
    kind: "agent",
    event: {
      type: "tool_call", id: "charge-2", name: "charge_card",
      input: { customer_id: "c-002", amount: 10 },
    },
  },
]);
assert.deepEqual(
  sequentialChargeRows.map(({ summary }) => summary),
  ["c-001 · $10", "c-002 · $10"],
  "sequential charge requests expose the customer and amount in the trace",
);

const branchedRows = reduceFrames([
  { kind: "meta", run_id: "run-source" },
  { kind: "lifecycle", type: "session_start", event_id: "source-event", payload: {} },
  { kind: "meta", run_id: "run-fork" },
  { kind: "lifecycle", type: "session_start", event_id: "fork-event", payload: {} },
]);
assert.deepEqual(
  branchedRows.map(({ eventId, runId }) => ({ eventId, runId })),
  [
    { eventId: "source-event", runId: "run-source" },
    { eventId: "fork-event", runId: "run-fork" },
  ],
  "trace rows retain the run boundary needed to separate source and fork branches",
);

const versionFrames = [
  { kind: "meta", run_id: "run-source" },
  { kind: "lifecycle", type: "session_start", run_id: "run-source" },
  { kind: "meta", run_id: "run-fork" },
  { kind: "lifecycle", type: "session_start", run_id: "run-fork" },
];
assert.deepEqual(deriveRunVersions(versionFrames), [
  { runId: "run-source", number: 1, label: "v1 · 원본" },
  { runId: "run-fork", number: 2, label: "v2 · 분기" },
]);
assert.deepEqual(
  selectRunFrames(versionFrames, "run-source"),
  versionFrames.slice(0, 2),
  "selecting a version projects only that run's chat and trace frames",
);
assert.equal(
  deriveVersionPhase([
    ...versionFrames.slice(0, 2),
    {
      kind: "outcome",
      run_id: "run-source",
      outcome: { stop_reason: "completed" },
    },
  ], "streaming"),
  "terminal",
  "a selected completed version keeps its own status while another version streams",
);

const versionedTraceFrames = [
  { kind: "meta", run_id: "run-v1", units: ["dlp_block"] },
  {
    kind: "lifecycle", run_id: "run-v1", event_id: "v1-session",
    type: "session_start", payload: {},
  },
  {
    kind: "lifecycle", run_id: "run-v1", event_id: "v1-submit",
    type: "user_prompt_submit", payload: { kind: "user_prompt" },
  },
  {
    kind: "lifecycle", run_id: "run-v1", event_id: "v1-context",
    type: "context_injected", payload: { kind: "user_prompt" },
  },
  {
    kind: "agent", run_id: "run-v1", event_id: "v1-send",
    event: { type: "tool_call", id: "send-v1", name: "send_email", input: {} },
  },
  {
    kind: "unit", run_id: "run-v1", event_id: "v1-deny",
    unit: "dlp_block", verdict: "deny", message: "거부",
  },
  {
    kind: "meta", run_id: "run-v2", units: [],
    fork_parent: "run-v1", fork_event_id: "v1-send", fork_edge: "before",
  },
  {
    kind: "lifecycle", run_id: "run-v2", event_id: "v2-session",
    type: "session_start", payload: {},
  },
  {
    kind: "lifecycle", run_id: "run-v2", event_id: "v2-submit",
    type: "user_prompt_submit", payload: { kind: "user_prompt" },
  },
  {
    kind: "lifecycle", run_id: "run-v2", event_id: "v2-context",
    type: "context_injected", payload: { kind: "user_prompt" },
  },
  {
    kind: "agent", run_id: "run-v2", event_id: "v2-send",
    event: { type: "tool_call", id: "send-v2", name: "send_email", input: {} },
  },
  {
    kind: "agent", run_id: "run-v2", event_id: "v2-result",
    event: { type: "tool_result", id: "send-v2", name: "send_email", executed: true },
  },
];
const versionedRows = deriveVersionRows(versionedTraceFrames, "run-v2");
assert.deepEqual(
  versionedRows.map(({ eventId }) => eventId),
  ["v1-session", "v1-submit", "v1-context", "v2-send", "v2-result"],
  "a child version keeps the parent prefix and replaces the replayed prefix at the fork",
);
assert.deepEqual(
  versionedRows.map(({ versionOrigin, forkStart }) => ({ versionOrigin, forkStart })),
  [
    { versionOrigin: "inherited", forkStart: false },
    { versionOrigin: "inherited", forkStart: false },
    { versionOrigin: "inherited", forkStart: false },
    { versionOrigin: "current", forkStart: true },
    { versionOrigin: "current", forkStart: false },
  ],
  "the trace exposes one readable version boundary",
);

const restoredToolRows = reduceFrames([
  { kind: "meta", run_id: "run-tools" },
  {
    kind: "agent", run_id: "run-tools", event_id: "event-request",
    event: { type: "tool_call", id: "read-1", name: "read_customer", input: {} },
  },
  {
    kind: "lifecycle", run_id: "run-tools", event_id: "event-pre",
    type: "pre_tool_use", payload: { call_id: "read-1", name: "read_customer" },
  },
  {
    kind: "lifecycle", run_id: "run-tools", event_id: "event-post",
    type: "post_tool_use", payload: { call_id: "read-1", name: "read_customer" },
  },
  {
    kind: "agent", run_id: "run-tools", event_id: "event-result",
    event: { type: "tool_result", id: "read-1", name: "read_customer", executed: true },
    restore_updates: [
      { event_id: "event-request", restore_edge: "after" },
      { event_id: "event-pre", restore_edge: "before" },
    ],
  },
  {
    kind: "lifecycle", run_id: "run-tools", event_id: "event-context",
    type: "context_injected", payload: { kind: "tool_result", origin_id: "read-1" },
    forkable: true, restore_edge: "after",
    restore_updates: [
      { event_id: "event-post", restore_edge: "after" },
      { event_id: "event-result", restore_edge: "after" },
    ],
  },
]);
assert.deepEqual(
  restoredToolRows.map(({ eventId, forkable, forkEdge }) => ({
    eventId, forkable, forkEdge,
  })),
  [
    { eventId: "event-request", forkable: true, forkEdge: "after" },
    { eventId: "event-pre", forkable: true, forkEdge: "before" },
    { eventId: "event-post", forkable: true, forkEdge: "after" },
    { eventId: "event-result", forkable: true, forkEdge: "after" },
    { eventId: "event-context", forkable: true, forkEdge: "after" },
  ],
  "late durable coordinates activate the earlier tool rows they stabilize",
);
assert.equal(
  getForkActionLabel(restoredToolRows[0], 2),
  "툴 실행 전 분기 · 정책 2개",
);
assert.equal(
  getForkActionLabel(restoredToolRows[2], 2),
  "툴 결과에서 분기 · 정책 2개",
);

const leafVersionFrames = [
  { kind: "meta", run_id: "leaf-v1" },
  {
    kind: "lifecycle", run_id: "leaf-v1", event_id: "leaf-session",
    type: "session_start", payload: {},
  },
  {
    kind: "agent", run_id: "leaf-v1", event_id: "leaf-request",
    event: { type: "tool_call", id: "read-1", name: "read_customer", input: {} },
  },
  {
    kind: "lifecycle", run_id: "leaf-v1", event_id: "leaf-pre",
    type: "pre_tool_use", payload: { call_id: "read-1", name: "read_customer" },
  },
  {
    kind: "meta", run_id: "leaf-v2", fork_parent: "leaf-v1",
    fork_event_id: "leaf-pre", fork_edge: "before", fork_mode: "leaf",
  },
  {
    kind: "lifecycle", run_id: "leaf-v2", event_id: "leaf-child-session",
    type: "session_start", payload: {},
  },
  {
    kind: "lifecycle", run_id: "leaf-v2", event_id: "leaf-child-pre",
    type: "pre_tool_use", payload: { call_id: "read-1", name: "read_customer" },
  },
  {
    kind: "lifecycle", run_id: "leaf-v2", event_id: "leaf-child-post",
    type: "post_tool_use", payload: { call_id: "read-1", name: "read_customer" },
  },
];
assert.deepEqual(
  deriveVersionRows(leafVersionFrames, "leaf-v2").map(({ eventId }) => eventId),
  ["leaf-session", "leaf-request", "leaf-child-pre", "leaf-child-post"],
  "a leaf continuation keeps the parent prefix and does not trim its shorter child stream",
);

const versionedChatFrames = [
  { kind: "meta", run_id: "chat-v1" },
  {
    kind: "agent", run_id: "chat-v1", event_id: "chat-read-request",
    event: { type: "tool_call", id: "read-1", name: "read_customer", input: {} },
  },
  {
    kind: "agent", run_id: "chat-v1", event_id: "chat-read-result",
    event: { type: "tool_result", id: "read-1", name: "read_customer", executed: true },
  },
  {
    kind: "agent", run_id: "chat-v1", event_id: "chat-send-request",
    event: { type: "tool_call", id: "send-1", name: "send_email", input: {} },
  },
  {
    kind: "lifecycle", run_id: "chat-v1", event_id: "chat-send-pre",
    type: "pre_tool_use", payload: { call_id: "send-1", name: "send_email" },
  },
  {
    kind: "lifecycle", run_id: "chat-v1", event_id: "chat-send-post",
    type: "post_tool_use", payload: { call_id: "send-1", name: "send_email" },
  },
  {
    kind: "agent", run_id: "chat-v1", event_id: "chat-send-result",
    event: { type: "tool_result", id: "send-1", name: "send_email", executed: true },
  },
  {
    kind: "agent", run_id: "chat-v1", event_id: "chat-old-text",
    event: { type: "text", text: "이전 응답" },
  },
  {
    kind: "meta", run_id: "chat-v2", fork_parent: "chat-v1",
    fork_event_id: "chat-send-post", fork_edge: "after", fork_mode: "leaf",
  },
  {
    kind: "lifecycle", run_id: "chat-v2", event_id: "chat-child-session",
    type: "session_start", payload: {},
  },
  {
    kind: "agent", run_id: "chat-v2", event_id: "chat-new-text",
    event: { type: "text", text: "새 응답" },
  },
];
assert.deepEqual(
  deriveChatView(
    "고객을 조회하고 메일로 보내줘.",
    selectRunFrames(versionedChatFrames, "chat-v2", { inheritFork: true }),
  ),
  {
    user: { role: "user", text: "고객을 조회하고 메일로 보내줘." },
    assistant: {
      role: "assistant",
      text: "새 응답",
      tools: [
        {
          id: "read-1", name: "read_customer", status: "completed",
          summary: "실행 완료", reason: null,
        },
        {
          id: "send-1", name: "send_email", status: "completed",
          summary: "실행 완료", reason: null,
        },
      ],
    },
  },
  "a forked chat keeps completed parent tools and replaces the abandoned response",
);

const nestedVersionedChatFrames = [
  ...versionedChatFrames,
  {
    kind: "agent", run_id: "chat-v2", event_id: "chat-audit-request",
    event: { type: "tool_call", id: "audit-1", name: "write_audit", input: {} },
  },
  {
    kind: "lifecycle", run_id: "chat-v2", event_id: "chat-audit-pre",
    type: "pre_tool_use", payload: { call_id: "audit-1", name: "write_audit" },
  },
  {
    kind: "agent", run_id: "chat-v2", event_id: "chat-audit-old-result",
    event: { type: "tool_result", id: "audit-1", name: "write_audit", executed: false },
  },
  {
    kind: "meta", run_id: "chat-v3", fork_parent: "chat-v2",
    fork_event_id: "chat-audit-pre", fork_edge: "before", fork_mode: "leaf",
  },
  {
    kind: "lifecycle", run_id: "chat-v3", event_id: "chat-v3-session",
    type: "session_start", payload: {},
  },
  {
    kind: "lifecycle", run_id: "chat-v3", event_id: "chat-audit-new-post",
    type: "post_tool_use", payload: { call_id: "audit-1", name: "write_audit" },
  },
  {
    kind: "agent", run_id: "chat-v3", event_id: "chat-audit-new-result",
    event: { type: "tool_result", id: "audit-1", name: "write_audit", executed: true },
  },
  {
    kind: "agent", run_id: "chat-v3", event_id: "chat-final-text",
    event: { type: "text", text: "최종 응답" },
  },
];
assert.deepEqual(
  deriveChatView(
    "고객을 조회하고 메일로 보내줘.",
    selectRunFrames(nestedVersionedChatFrames, "chat-v3", { inheritFork: true }),
  ).assistant,
  {
    role: "assistant",
    text: "새 응답최종 응답",
    tools: [
      {
        id: "read-1", name: "read_customer", status: "completed",
        summary: "실행 완료", reason: null,
      },
      {
        id: "send-1", name: "send_email", status: "completed",
        summary: "실행 완료", reason: null,
      },
      {
        id: "audit-1", name: "write_audit", status: "completed",
        summary: "실행 완료", reason: null,
      },
    ],
  },
  "a nested fork keeps chat context inherited through every ancestor version",
);

console.log("run inspector state ok");
