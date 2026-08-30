import assert from "node:assert/strict";

import {
  deriveBranchView,
  deriveChatView,
  deriveRunVersions,
  deriveVersionPhase,
  deriveVersionRows,
  getForkActionLabel,
  GUIDE,
  getEventForkRequest,
  guideMatch,
  nextGuideStep,
  pickInlineActionHost,
  getLaunchCopy,
  makeResultBadges,
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
  CALL_REPLAY_BADGE,
  PAYMENT_DEDUPE_BADGE,
  RESULT_MASK_BADGE,
  isResumeGate,
  reduceFrames,
  resultBadges,
  unitSummary,
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
          badges: [],
          output: "customer found",
        },
        {
          id: "call-send",
          name: "send_email",
          status: "blocked",
          summary: "정책으로 차단",
          reason: "메일 본문에 개인정보가 있습니다",
          badges: [],
        },
      ],
    },
  },
  "chat view turns protocol frames into one user turn and one assistant turn",
);

function replayPresentation(prompt, frames) {
  const chat = deriveChatView(prompt, frames).assistant.tools[0];
  const row = reduceFrames(frames).find((item) => item.kind === "result");
  return {
    chat: {
      id: chat.id,
      name: chat.name,
      status: chat.status,
      summary: chat.summary,
      reason: chat.reason,
      badges: chat.badges,
    },
    trace: {
      summary: row.summary,
      verdict: row.verdict,
      badges: row.badges,
      tone: row.tone,
      callId: row.details.callId,
      idempotencyKey: row.details.idempotencyKey,
    },
  };
}

function toolResultFrames(id, name, result) {
  return [
    {
      kind: "agent",
      event: {
        type: "tool_call",
        id,
        name,
        input: name === "charge_card"
          ? { customer_id: "c-001", amount: "10" }
          : { customer_id: "c-001" },
      },
    },
    {
      kind: "agent",
      event: {
        type: "tool_result",
        id,
        name,
        executed: true,
        result,
      },
    },
  ];
}

const replayedToolFrames = toolResultFrames("read-1", "read_customer", {
  type: "text",
  text: "customer found",
  execution_count: 1,
  execution: { call_id: "read-1", replayed: true },
});
assert.deepEqual(
  replayPresentation("조회해줘.", replayedToolFrames),
  {
    chat: {
      id: "read-1",
      name: "read_customer",
      status: "completed",
      summary: "실행 완료",
      reason: null,
      badges: [CALL_REPLAY_BADGE],
    },
    trace: {
      summary: "실행 완료",
      verdict: null,
      badges: [CALL_REPLAY_BADGE],
      tone: "neutral",
      callId: "read-1",
      idempotencyKey: null,
    },
  },
  "call replay only: CALL REPLAY on the chat card and event row",
);

const replayedChargeFrames = toolResultFrames("charge-1", "charge_card", {
  type: "text",
  text: '{"status":"charged","amount":"10"}',
  execution_count: 1,
  execution: { call_id: "charge-1", replayed: false },
  idempotency: { key: "batch-1:c-001", replayed: true },
});
assert.deepEqual(
  replayPresentation("결제해줘.", replayedChargeFrames),
  {
    chat: {
      id: "charge-1",
      name: "charge_card",
      status: "completed",
      summary: "실행 완료",
      reason: null,
      badges: [PAYMENT_DEDUPE_BADGE],
    },
    trace: {
      summary: "실행 완료",
      verdict: null,
      badges: [PAYMENT_DEDUPE_BADGE],
      tone: "neutral",
      callId: "charge-1",
      idempotencyKey: "batch-1:c-001",
    },
  },
  "payment dedupe only: PAYMENT DEDUPE on the chat card and event row",
);

const bothReplayFrames = toolResultFrames("charge-2", "charge_card", {
  type: "text",
  text: '{"status":"charged","amount":"10"}',
  execution_count: 1,
  execution: { call_id: "charge-2", replayed: true },
  idempotency: { key: "batch-1:c-001", replayed: true },
});
assert.deepEqual(
  replayPresentation("결제해줘.", bothReplayFrames),
  {
    chat: {
      id: "charge-2",
      name: "charge_card",
      status: "completed",
      summary: "실행 완료",
      reason: null,
      badges: [CALL_REPLAY_BADGE, PAYMENT_DEDUPE_BADGE],
    },
    trace: {
      summary: "실행 완료",
      verdict: null,
      badges: [CALL_REPLAY_BADGE, PAYMENT_DEDUPE_BADGE],
      tone: "neutral",
      callId: "charge-2",
      idempotencyKey: "batch-1:c-001",
    },
  },
  "both markers: CALL REPLAY and PAYMENT DEDUPE show together",
);

const maskedCustomer = {
  type: "text",
  text: '{"email":"j***@***","ssn":"***-**-****","plan":"pro"}',
  redacted_by: "pii_mask",
  control_note: "이메일·주민번호 가림",
};
const rawCustomer = {
  type: "text",
  text: '{"email":"jane@doe.io","ssn":"123-45-6789","plan":"pro"}',
};
assert.deepEqual(
  resultBadges({ name: "read_customer", result: maskedCustomer }),
  [RESULT_MASK_BADGE],
  "a rewritten tool result carries a result-mask badge",
);
assert.deepEqual(
  resultBadges({ name: "read_customer", result: rawCustomer }),
  [],
  "an unmasked tool result has no result-mask badge",
);

const maskedChat = deriveChatView(
  "read_customer로 고객을 조회해줘.",
  toolResultFrames("read-mask", "read_customer", maskedCustomer),
).assistant.tools[0];
assert.equal(maskedChat.output, maskedCustomer.text);
assert.equal(maskedChat.redactedBy, "pii_mask");
assert.deepEqual(maskedChat.badges, [RESULT_MASK_BADGE]);

const rawChat = deriveChatView(
  "read_customer로 고객을 조회해줘.",
  toolResultFrames("read-raw", "read_customer", rawCustomer),
).assistant.tools[0];
assert.equal(rawChat.output, rawCustomer.text);
assert.equal(rawChat.redactedBy, undefined);
assert.deepEqual(rawChat.badges, []);

const maskingForkFrames = [
  { kind: "meta", run_id: "mask-v1", units: ["pii_mask"] },
  {
    kind: "agent", run_id: "mask-v1", event_id: "mask-call",
    event: { type: "tool_call", id: "read-1", name: "read_customer", input: {} },
  },
  {
    kind: "agent", run_id: "mask-v1", event_id: "mask-result",
    event: {
      type: "tool_result", id: "read-1", name: "read_customer",
      executed: true, result: maskedCustomer,
    },
  },
  {
    kind: "lifecycle", run_id: "mask-v1", type: "branch_snapshot",
    payload: {
      branch: "source",
      run_id: "mask-v1",
      messages: [{ role: "user", content: "조회해줘." }],
    },
  },
  {
    kind: "meta", run_id: "mask-v2", units: [],
    fork_parent: "mask-v1", fork_event_id: "mask-call", fork_edge: "before",
  },
  {
    kind: "lifecycle", run_id: "mask-v2", type: "branch_snapshot",
    payload: {
      branch: "fork",
      run_id: "mask-v2",
      messages: [{ role: "user", content: "조회해줘." }],
    },
  },
  {
    kind: "agent", run_id: "mask-v2", event_id: "plain-result",
    event: {
      type: "tool_result", id: "read-1", name: "read_customer",
      executed: true, result: rawCustomer,
    },
  },
];
const forkedMaskChat = deriveChatView(
  "조회해줘.",
  selectRunFrames(maskingForkFrames, "mask-v2", { inheritFork: true }),
).assistant.tools[0];
assert.equal(
  forkedMaskChat.output,
  rawCustomer.text,
  "a tool-call fork with masking off shows the original tool result in chat",
);
assert.equal(forkedMaskChat.redactedBy, undefined);
assert.deepEqual(
  deriveChatView(
    "조회해줘.",
    selectRunFrames(maskingForkFrames, "mask-v1", { inheritFork: true }),
  ).assistant.tools[0].output,
  maskedCustomer.text,
  "the source version keeps the masked tool result after the fork exists",
);

const postOnlyMasked = deriveChatView("조회해줘.", [
  {
    kind: "agent",
    event: { type: "tool_call", id: "read-1", name: "read_customer", input: {} },
  },
  {
    kind: "lifecycle",
    type: "post_tool_use",
    payload: { call_id: "read-1", name: "read_customer", result: maskedCustomer },
  },
]).assistant.tools[0];
assert.equal(
  postOnlyMasked.output,
  maskedCustomer.text,
  "a completed tool without a tool_result frame still shows the post_tool_use payload",
);
assert.equal(postOnlyMasked.redactedBy, "pii_mask");
assert.deepEqual(postOnlyMasked.badges, [RESULT_MASK_BADGE]);

const replayUnmasks = deriveChatView("조회해줘.", [
  {
    kind: "agent",
    event: { type: "tool_call", id: "read-1", name: "read_customer", input: {} },
  },
  {
    kind: "agent",
    event: {
      type: "tool_result",
      id: "read-1",
      name: "read_customer",
      executed: true,
      result: maskedCustomer,
    },
  },
  {
    kind: "lifecycle",
    type: "post_tool_use",
    payload: { call_id: "read-1", name: "read_customer", result: rawCustomer },
  },
]).assistant.tools[0];
assert.equal(
  replayUnmasks.output,
  rawCustomer.text,
  "a later post_tool_use payload replaces the inherited masked tool result",
);
assert.equal(replayUnmasks.redactedBy, undefined);

function miniDocument() {
  return {
    createElement(tag) {
      const node = {
        tagName: String(tag).toUpperCase(),
        className: "",
        textContent: "",
        childNodes: [],
        append(...kids) {
          this.childNodes.push(...kids);
        },
      };
      return node;
    },
  };
}

function serializeNode(node) {
  if (!node) return "";
  const inner = node.childNodes.length
    ? node.childNodes.map(serializeNode).join("")
    : node.textContent;
  const cls = node.className ? ` class="${node.className}"` : "";
  return `<${node.tagName.toLowerCase()}${cls}>${inner}</${node.tagName.toLowerCase()}>`;
}

assert.equal(
  serializeNode(makeResultBadges(miniDocument(), [CALL_REPLAY_BADGE])),
  '<span class="result-badges"><span class="result-badge kind-call_replay">CALL REPLAY · 도구 호출 생략</span></span>',
  "call replay badge markup",
);
assert.equal(
  serializeNode(makeResultBadges(miniDocument(), [PAYMENT_DEDUPE_BADGE])),
  '<span class="result-badges"><span class="result-badge kind-payment_dedupe">PAYMENT DEDUPE · 결제 원장 재사용</span></span>',
  "payment dedupe badge markup",
);
assert.equal(
  serializeNode(makeResultBadges(
    miniDocument(),
    [CALL_REPLAY_BADGE, PAYMENT_DEDUPE_BADGE],
  )),
  '<span class="result-badges">'
    + '<span class="result-badge kind-call_replay">CALL REPLAY · 도구 호출 생략</span>'
    + '<span class="result-badge kind-payment_dedupe">PAYMENT DEDUPE · 결제 원장 재사용</span>'
    + "</span>",
  "both badges render side by side in one group",
);
assert.equal(
  serializeNode(makeResultBadges(miniDocument(), [RESULT_MASK_BADGE])),
  '<span class="result-badges"><span class="result-badge kind-result_mask">RESULT MASK · 도구 결과 가림</span></span>',
  "result mask badge markup",
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
      unitNames: ["pii_mask", "dlp_block"],
    })),
    "run-b",
  ),
  "completed",
);
const forking = beginFork(forkSourceTerminal);
assert.deepEqual(forking.active.unitNames, ["pii_mask", "dlp_block"]);
assert.equal(forking.phase, "streaming");
assert.equal(forking.runId, null);
const genericForking = beginFork(terminal);
assert.deepEqual(genericForking.active.unitNames, ["approval", "dlp_block"]);
assert.equal(genericForking.phase, "streaming");
const editedGenericTerminal = updateDraft(terminal, { unitNames: ["rate_cap"] });
assert.deepEqual(beginFork(editedGenericTerminal).active.unitNames, ["rate_cap"]);
const editedMaskingTerminal = updateDraft(forkSourceTerminal, {
  unitNames: ["pii_mask", "dlp_block", "approval"],
});
assert.deepEqual(beginFork(editedMaskingTerminal).active.unitNames, [
  "pii_mask",
  "dlp_block",
  "approval",
]);
const unmaskedMaskingTerminal = updateDraft(forkSourceTerminal, { unitNames: [] });
assert.deepEqual(
  beginFork(unmaskedMaskingTerminal).active.unitNames,
  [],
  "turning pii_mask off is the fork control set, not an automatic strip",
);
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
    units: ["pii_mask", "dlp_block", "approval"],
  },
  "an event-row fork keeps the operator-selected masking policy",
);
assert.deepEqual(
  getEventForkRequest(unmaskedMaskingTerminal, {
    eventId: "event-03",
    forkable: true,
    forkEdge: "after",
  }),
  {
    run_id: "run-b",
    event_id: "event-03",
    edge: "after",
    units: [],
  },
  "forking a tool result with masking off sends an empty control set",
);
const preToolRow = {
  eventId: "event-pre",
  label: "pre_tool_use",
  kind: "lifecycle",
  forkable: true,
  forkEdge: "before",
  callId: "read-1",
};
const postToolRow = {
  eventId: "event-post",
  label: "post_tool_use",
  kind: "lifecycle",
  forkable: true,
  forkEdge: "after",
  callId: "read-1",
};
assert.deepEqual(
  getEventForkRequest(unmaskedMaskingTerminal, postToolRow, [preToolRow, postToolRow]),
  {
    run_id: "run-b",
    event_id: "event-pre",
    edge: "before",
    units: [],
  },
  "turning pii_mask off at a saved tool result replays the original through the new journal",
);
assert.deepEqual(
  getEventForkRequest(forkSourceTerminal, postToolRow, [preToolRow, postToolRow]),
  {
    run_id: "run-b",
    event_id: "event-post",
    edge: "after",
    units: ["pii_mask", "dlp_block"],
  },
  "unchanged journal units keep the after-result continue edge",
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
      label: "send_email 호출",
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
  deriveRunVersions([
    { kind: "meta", run_id: "run-masked", units: ["pii_mask"] },
    { kind: "meta", run_id: "run-plain", units: [] },
  ]),
  [
    { runId: "run-masked", number: 1, label: "v1 · 원본 · pii_mask" },
    { runId: "run-plain", number: 2, label: "v2 · 분기 · 정책 없음" },
  ],
  "version labels expose the masking policy that produced each run",
);
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
          summary: "실행 완료", reason: null, badges: [],
        },
        {
          id: "send-1", name: "send_email", status: "completed",
          summary: "실행 완료", reason: null, badges: [],
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
        summary: "실행 완료", reason: null, badges: [],
      },
      {
        id: "send-1", name: "send_email", status: "completed",
        summary: "실행 완료", reason: null, badges: [],
      },
      {
        id: "audit-1", name: "write_audit", status: "completed",
        summary: "실행 완료", reason: null, badges: [],
      },
    ],
  },
  "a nested fork keeps chat context inherited through every ancestor version",
);

// A parallel batch parks once per call: the second suspend frame used to throw,
// flipping the run to "error" while the frame log still read "suspended" — the
// approve panel rendered over a dead button.
const batchParked = suspendRun(suspendRun(streaming, "call-a"), "call-b");
assert.equal(batchParked.phase, "suspended");
assert.equal(batchParked.pendingId, "call-b");
assert.equal(markRecoverable(batchParked).phase, "recoverable");
assert.equal(suspendRun(markRecoverable(streaming), "call-c").phase, "suspended");
assert.throws(() => suspendRun(terminal, "call-d"), /cannot suspend run/);
assert.throws(() => suspendRun(failRun(streaming, "x"), "call-e"), /cannot suspend run/);

// A suspend verdict names the call it gates, so N identical rows stay tellable apart.
assert.equal(
  unitSummary({
    unit: "approval", verdict: "suspend", message: "승인이 필요합니다.",
    call_id: "call-a", name: "charge_card",
    input: { customer_id: "c-002", amount: "10" },
  }),
  "charge_card · c-002 · $10 — 승인이 필요합니다.",
);
assert.equal(unitSummary({ unit: "approval", message: "정책" }), "정책");

const suspendRows = reduceFrames([
  {
    kind: "unit", unit: "approval", verdict: "suspend",
    message: "charge_card은 되돌릴 수 없습니다. 승인이 필요합니다.",
    call_id: "call-b", name: "charge_card",
    input: { customer_id: "c-003", amount: "10" },
  },
]);
assert.equal(suspendRows[0].callId, "call-b");
assert.match(suspendRows[0].summary, /c-003 · \$10/);

// The approval-gate crash rolls back: its pre_tool_use never committed, so the row
// must not survive next to the replayed one. The recover marker itself stays.
const crashRows = reduceFrames([
  { kind: "agent", event: { type: "tool_call", id: "call-a", name: "charge_card", input: { customer_id: "c-001", amount: "10" } } },
  { kind: "lifecycle", type: "pre_tool_use", payload: { call_id: "call-a", name: "charge_card" } },
  { kind: "recoverable", step: "call-a", message: "워커 장애" },
  { kind: "lifecycle", type: "pre_tool_use", payload: { call_id: "call-a", name: "charge_card" } },
]);
assert.deepEqual(
  crashRows.map((row) => `${row.kind}:${row.label}`),
  ["tool:charge_card 호출", "recovery:recover", "lifecycle:pre_tool_use"],
  "the uncommitted pre-crash pre_tool_use row is dropped, the recover marker kept",
);

// A crash at the commit seam carries an effect key, not a call id — nothing is dropped.
const commitCrashRows = reduceFrames([
  { kind: "lifecycle", type: "pre_tool_use", payload: { call_id: "call-a", name: "charge_card" } },
  { kind: "recoverable", step: "effect:charge_card:1", message: "워커 장애" },
]);
assert.deepEqual(
  commitCrashRows.map((row) => `${row.kind}:${row.label}`),
  ["lifecycle:pre_tool_use", "recovery:recover"],
);

// An approved call leaves two forkable pre_tool_use rows: the gate, and the
// 승인 후 재검증 replay. Re-running the journal must resume from the later one —
// forking from the gate rewinds past the approval and drops the operator's decision.
const resumedPreToolRow = {
  eventId: "event-pre-resumed",
  label: "on_resume",
  kind: "lifecycle",
  forkable: true,
  forkEdge: "before",
  callId: "read-1",
};
assert.deepEqual(
  getEventForkRequest(
    unmaskedMaskingTerminal,
    postToolRow,
    [preToolRow, resumedPreToolRow, postToolRow],
  ),
  { run_id: "run-b", event_id: "event-pre-resumed", edge: "before", units: [] },
  "an approved call re-runs from the post-approval boundary, not the gate",
);
// Only boundaries that precede the forked row count.
assert.deepEqual(
  getEventForkRequest(
    unmaskedMaskingTerminal,
    postToolRow,
    [preToolRow, postToolRow, resumedPreToolRow],
  ),
  { run_id: "run-b", event_id: "event-pre", edge: "before", units: [] },
);

// A call and its result are two different events; the trace must not print the
// same name twice for them.
const callAndResult = reduceFrames([
  { kind: "agent", event: { type: "tool_call", id: "c1", name: "charge_card", input: { customer_id: "c-001", amount: "10" } } },
  { kind: "agent", event: { type: "tool_result", id: "c1", name: "charge_card", executed: true, result: { type: "text", text: "{}" } } },
]);
assert.deepEqual(
  callAndResult.map((row) => [row.kind, row.label]),
  [["tool", "charge_card 호출"], ["result", "charge_card 결과"]],
);

// 승인 대기 must land on the call the server actually parked, not on whichever
// call happens to be last — a parallel batch parks a specific one.
const parkedChat = deriveChatView("청구해줘", [
  { kind: "agent", event: { type: "tool_call", id: "call-a", name: "charge_card" } },
  { kind: "agent", event: { type: "tool_call", id: "call-b", name: "charge_card" } },
  { kind: "agent", event: { type: "tool_call", id: "call-c", name: "charge_card" } },
  { kind: "suspended", pending_id: "call-b" },
]);
assert.deepEqual(
  parkedChat.assistant.tools.map((tool) => [tool.id, tool.status]),
  [["call-a", "running"], ["call-b", "approval"], ["call-c", "running"]],
);

const crashedChat = deriveChatView("청구해줘", [
  { kind: "agent", event: { type: "tool_call", id: "call-a", name: "charge_card" } },
  { kind: "agent", event: { type: "tool_call", id: "call-b", name: "charge_card" } },
  { kind: "recoverable", step: "call-a" },
]);
assert.deepEqual(
  crashedChat.assistant.tools.map((tool) => [tool.id, tool.status]),
  [["call-a", "recoverable"], ["call-b", "running"]],
);

// The approve panel attaches to the parked call inside the chat, not to the trace.
const node = (id, status) => ({ id, dataset: { callId: id, status } });
const chatNodes = new Map([
  ["call-a", node("call-a", "completed")],
  ["call-b", node("call-b", "approval")],
  ["call-c", node("call-c", "running")],
]);
assert.equal(pickInlineActionHost(chatNodes, "call-b", "approval").id, "call-b");
assert.equal(
  pickInlineActionHost(chatNodes, null, "approval").id,
  "call-b",
  "with no pending id it still finds the call awaiting approval",
);
assert.equal(
  pickInlineActionHost(chatNodes, "call-gone", "approval").id,
  "call-b",
  "a stale pending id falls back to the call actually parked",
);
assert.equal(pickInlineActionHost(chatNodes, null, "recoverable"), null);
assert.equal(pickInlineActionHost(new Map(), "call-b", "approval"), null);

// The gate and the post-approval revalidation are two decisions, not one row twice.
const twoGates = reduceFrames([
  { kind: "lifecycle", type: "pre_tool_use", payload: { call_id: "c1", name: "send_email" } },
  { kind: "lifecycle", type: "pre_tool_use",
    payload: { call_id: "c1", name: "send_email", source: "on_resume" } },
]);
assert.deepEqual(
  twoGates.map((row) => [row.label, row.summary]),
  [["pre_tool_use", "send_email"], ["on_resume", "승인 후 재검증 · send_email"]],
);
assert.equal(isResumeGate({ type: "pre_tool_use", payload: { source: "on_resume" } }), true);
assert.equal(isResumeGate({ type: "pre_tool_use", payload: {} }), false);

// The three scenes are an order to read the demo in: unguarded, gated, and gated
// through a worker death.
assert.deepEqual(GUIDE.map((scene) => [scene.scenarioId, [...scene.unitNames]]), [
  ["charge", []],
  ["charge", ["approval"]],
  ["crash", ["approval"]],
]);
assert.equal(guideMatch({ scenarioId: "charge", unitNames: [] }), 0);
assert.equal(guideMatch({ scenarioId: "charge", unitNames: ["approval"] }), 1);
assert.equal(
  guideMatch({ scenarioId: "leak", unitNames: ["dlp_block"] }),
  -1,
  "wandering off the path leaves the guide behind rather than mislabelling a scene",
);
assert.equal(nextGuideStep({ scenarioId: "charge", unitNames: [] }, "completed"), 1);
assert.equal(
  nextGuideStep({ scenarioId: "charge", unitNames: [] }, "aborted"),
  0,
  "an abandoned run does not march the operator past a scene they never saw",
);
assert.equal(nextGuideStep({ scenarioId: "crash", unitNames: ["approval"] }, "completed"), 2);
assert.equal(nextGuideStep({ scenarioId: "leak", unitNames: [] }, "completed"), -1);

// A step that started and never reported is its own outcome, not a failure row.
const unknown = reduceFrames([
  { kind: "recoverable", step: "call-a", message: "워커 장애" },
  { kind: "indeterminate", step: "call-a",
    message: "이 효과는 나갔을 수도, 안 나갔을 수도 있습니다" },
]);
assert.deepEqual(unknown.map((row) => [row.kind, row.label]), [
  ["recovery", "recover"],
  ["indeterminate", "indeterminate"],
]);
assert.equal(unknown[1].details.step, "call-a");
assert.equal(unknown[1].tone, "halt");

console.log("run inspector state ok");
