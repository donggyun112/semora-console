import assert from "node:assert/strict";

import {
  deriveBranchView,
  deriveChatView,
  deriveRunVersions,
  deriveVersionPhase,
  deriveVersionRows,
  describeFork,
  editedArgs,
  getForkActionLabel,
  GUIDE,
  getEventForkRequest,
  guideMatch,
  isRetargetedFork,
  policiesAt,
  isForkRepresentative,
  nextGuideStep,
  pendingCallArgs,
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
  attachBranchId,
  beginFork,
  beginContinuation,
  canEditDraft,
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
} from "../src/console/static/run-state.mjs";
import {
  CALL_REPLAY_BADGE,
  PAYMENT_DEDUPE_BADGE,
  RESULT_MASK_BADGE,
  isResumeGate,
  reduceFrames,
  resultBadges,
  steerSummary,
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
  { kind: "meta", branch_id: "mask-v1", units: ["pii_mask"] },
  {
    kind: "agent", branch_id: "mask-v1", event_id: "mask-call",
    event: { type: "tool_call", id: "read-1", name: "read_customer", input: {} },
  },
  {
    kind: "agent", branch_id: "mask-v1", event_id: "mask-result",
    event: {
      type: "tool_result", id: "read-1", name: "read_customer",
      executed: true, result: maskedCustomer,
    },
  },
  {
    kind: "lifecycle", branch_id: "mask-v1", type: "branch_snapshot",
    payload: {
      branch: "source",
      branch_id: "mask-v1",
      messages: [{ role: "user", content: "조회해줘." }],
    },
  },
  {
    kind: "meta", branch_id: "mask-v2", units: [],
    fork_parent: "mask-v1", fork_event_id: "mask-call", fork_edge: "before",
  },
  {
    kind: "lifecycle", branch_id: "mask-v2", type: "branch_snapshot",
    payload: {
      branch: "fork",
      branch_id: "mask-v2",
      messages: [{ role: "user", content: "조회해줘." }],
    },
  },
  {
    kind: "agent", branch_id: "mask-v2", event_id: "plain-result",
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
  branchId: null,
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

const streaming = attachBranchId(startRun(idle), "run-1");
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
// A parked run still takes a steer: the queue is the ledger, and the loop drains on
// resume. Only a run with no drain left ahead of it refuses.
assert.equal(canSteer(suspended), true);
assert.equal(acceptsStreamEnd(suspended), true);

const resuming = beginContinuation(suspended);
assert.equal(resuming.phase, "streaming");
assert.equal(resuming.continuationBusy, true);
assert.equal(resuming.branchId, "run-1");
assert.equal(resuming.pendingId, "pending-1");
assert.throws(() => beginContinuation(resuming), /continuation already active/);

const recoverable = markRecoverable(streaming);
assert.equal(recoverable.phase, "recoverable");
assert.equal(canStartRun(recoverable), false);
assert.equal(acceptsStreamEnd(recoverable), true);

// A kept run took several requests. Replaying its frames, the next stream's meta lands on
// whatever phase the previous stream left — and only a streaming run may take a run id.
for (const parked of [recoverable, suspendRun(streaming, "p-1"), finishRun(streaming, "completed")]) {
  const again = replayStream(parked, ["approval"]);
  assert.equal(again.phase, "streaming", `a replayed stream begins from ${parked.phase}`);
  assert.deepEqual(again.active.unitNames, ["approval"], "with the units that stream ran under");
  assert.equal(attachBranchId(again, "run-x").branchId, "run-x");
}
assert.throws(() => replayStream(createRunState()), /no active run/);

const terminal = finishRun(streaming, "completed");
assert.equal(terminal.phase, "terminal");
assert.equal(terminal.stopReason, "completed");
assert.equal(canStartRun(terminal), true);

const forkSourceTerminal = finishRun(
  attachBranchId(
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
assert.equal(forking.branchId, null);
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
    branch_id: "run-b",
    event_id: "event-03",
    edge: "before",
    units: ["pii_mask", "dlp_block", "approval"],
    rejournal: false,
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
    branch_id: "run-b",
    event_id: "event-03",
    edge: "after",
    units: [],
    rejournal: false,
  },
  "forking a tool result with masking off sends an empty control set",
);
// The framework says where a coordinate resumes and which control points run again
// from there; each unit says which point it lives at. That is the whole plan — the
// page never decides from a policy's name whether a branch will reach it.
const PLAN = {
  points: new Map([
    ["input_mask", "on_inputs"],
    ["approval", "pre_tool_use"], ["dlp_block", "pre_tool_use"], ["rate_cap", "pre_tool_use"],
    ["pii_mask", "post_tool_use"], ["context_firewall", "post_tool_use"],
    ["injection_guard", "post_tool_use"], ["result_drop", "post_tool_use"],
    ["log_gate", "before_finish"],
  ]),
  reruns: {
    on_inputs: ["on_inputs", "before_model", "pre_tool_use", "post_tool_use", "before_finish"],
    pre_tool_use: ["pre_tool_use", "post_tool_use", "before_finish"],
    post_tool_use: ["post_tool_use", "before_finish"],
    before_model: ["before_model", "before_finish"],
  },
};
assert.deepEqual(
  policiesAt("before_model", ["pii_mask", "dlp_block", "log_gate"], PLAN),
  { applies: ["log_gate"], skipped: ["pii_mask", "dlp_block"] },
  "after a recorded result neither the gate nor the journal runs again for that call",
);
assert.deepEqual(
  policiesAt("pre_tool_use", ["pii_mask", "dlp_block", "log_gate"], PLAN),
  { applies: ["pii_mask", "dlp_block", "log_gate"], skipped: [] },
);
assert.deepEqual(
  policiesAt("before_model", ["mystery"], PLAN),
  { applies: [], skipped: ["mystery"] },
  "a unit whose point nobody declared is not promised to run",
);

// The projector sends both coordinates for a saved result: where it restores, and
// where it was made. A changed policy this coordinate would not reach, but the other
// would, takes the second one — whichever control point that policy lives at.
const postToolRow = {
  eventId: "event-post",
  label: "post_tool_use",
  kind: "lifecycle",
  forkable: true,
  forkEdge: "after",
  resumesAt: "before_model",
  rebuild: {
    event_id: "event-pre", edge: "before",
    resumes_at: "pre_tool_use", rejournal_at: "post_tool_use",
  },
};
assert.deepEqual(
  getEventForkRequest(unmaskedMaskingTerminal, postToolRow, PLAN),
  {
    branch_id: "run-b",
    event_id: "event-pre",
    edge: "before",
    units: [],
    rejournal: false,
  },
  "turning everything off rewinds to the gate — dlp_block lives there, and a changed gate "
  + "has to be asked again",
);
const unmaskedOnly = updateDraft(forkSourceTerminal, { unitNames: ["dlp_block"] });
assert.deepEqual(
  getEventForkRequest(unmaskedOnly, postToolRow, PLAN),
  {
    branch_id: "run-b",
    event_id: "event-pre",
    edge: "before",
    units: ["dlp_block"],
    rejournal: true,
  },
  "turning only pii_mask off journals the recorded result again without asking the gate, "
  + "which decided nothing new",
);
assert.deepEqual(
  getEventForkRequest(forkSourceTerminal, postToolRow, PLAN),
  {
    branch_id: "run-b",
    event_id: "event-post",
    edge: "after",
    units: ["pii_mask", "dlp_block"],
    rejournal: false,
  },
  "unchanged units keep the after-result continue edge",
);
const gatedLater = updateDraft(forkSourceTerminal, {
  unitNames: ["pii_mask", "dlp_block", "approval"],
});
const gated = getEventForkRequest(gatedLater, postToolRow, PLAN);
assert.equal(
  gated.event_id,
  "event-pre",
  "adding a gate policy at a saved result rewinds too — a gate is not a journal, and it "
  + "would not run from here either",
);
assert.equal(gated.rejournal, false, "and a changed gate has to be asked, so no skipping it");
const finishOnly = updateDraft(forkSourceTerminal, {
  unitNames: ["pii_mask", "dlp_block", "log_gate"],
});
assert.equal(
  getEventForkRequest(finishOnly, postToolRow, PLAN).event_id,
  "event-post",
  "a finish policy runs from after the result, so nothing moves",
);
const described = describeFork(gatedLater, postToolRow, PLAN);
assert.equal(described.retargeted, true);
assert.equal(described.mode, "retarget");
assert.equal(described.resumesAt, "pre_tool_use", "the policies are judged where the branch lands");
const rejournaled = describeFork(unmaskedOnly, postToolRow, PLAN);
assert.equal(rejournaled.mode, "rejournal");
assert.equal(rejournaled.resumesAt, "post_tool_use", "a journal-only change resumes past the gate");
assert.equal(
  getForkActionLabel(postToolRow, rejournaled.policies, rejournaled.mode),
  "기록된 결과에 새 정책만 적용 · 적용 없음 · 건너뜀 dlp_block",
  "the gate policy stays selected but is not consulted — the button says so",
);
const refusedRow = { ...postToolRow, rebuild: { ...postToolRow.rebuild, rejournal_at: null } };
assert.equal(
  getEventForkRequest(unmaskedOnly, refusedRow, PLAN).rejournal,
  false,
  "a call that never finished has no record to journal again, so the gate it is",
);
assert.deepEqual(described.policies, {
  applies: ["pii_mask", "dlp_block", "approval"], skipped: [],
});
assert.deepEqual(
  describeFork(finishOnly, postToolRow, PLAN).policies,
  { applies: ["log_gate"], skipped: ["pii_mask", "dlp_block"] },
  "and a button that stays says which selected policies it will not reach",
);
assert.deepEqual(
  getEventForkRequest(unmaskedMaskingTerminal, {
    eventId: "event-post",
    forkable: true,
    forkEdge: "after",
  }),
  {
    branch_id: "run-b",
    event_id: "event-post",
    edge: "after",
    units: [],
    rejournal: false,
  },
  "a boundary the projector cannot rebuild stays where it is",
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
    branch_id: "run-1",
    event_id: "event-generic",
    edge: "before",
    units: ["rate_cap"],
    rejournal: false,
  },
  "completed runs expose event forks for every scenario",
);
const secondVersion = finishRun(
  attachBranchId(beginFork(editedGenericTerminal), "run-2"),
  "completed",
);
assert.deepEqual(
  getEventForkRequest(secondVersion, {
    eventId: "event-from-v1",
    branchId: "run-1",
    forkable: true,
    forkEdge: "before",
  }, true),
  {
    branch_id: "run-1",
    event_id: "event-from-v1",
    edge: "before",
    units: ["rate_cap"],
    rejournal: false,
  },
  "an older selected version remains forkable after newer versions exist",
);

assert.deepEqual(
  deriveBranchView([
    { kind: "lifecycle", type: "branch_snapshot", payload: {
      branch: "source", branch_id: "run-b", active: true,
      messages: [{ role: "user", content: "ssn is ***" }],
    } },
    { kind: "lifecycle", type: "branch_snapshot", payload: {
      branch: "fork", branch_id: "run-c", active: true,
      messages: [{ role: "user", content: "ssn is 123-45" }],
    } },
  ]),
  [
    {
      branch: "source", branchId: "run-b", active: false,
      messages: [{ role: "user", content: "ssn is ***" }],
    },
    {
      branch: "fork", branchId: "run-c", active: true,
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
  { kind: "meta", branch_id: "run-1" },
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
  { kind: "meta", branch_id: "run-source" },
  { kind: "lifecycle", type: "session_start", event_id: "source-event", payload: {} },
  { kind: "meta", branch_id: "run-fork" },
  { kind: "lifecycle", type: "session_start", event_id: "fork-event", payload: {} },
]);
assert.deepEqual(
  branchedRows.map(({ eventId, branchId }) => ({ eventId, branchId })),
  [
    { eventId: "source-event", branchId: "run-source" },
    { eventId: "fork-event", branchId: "run-fork" },
  ],
  "trace rows retain the run boundary needed to separate source and fork branches",
);

const versionFrames = [
  { kind: "meta", branch_id: "run-source" },
  { kind: "lifecycle", type: "session_start", branch_id: "run-source" },
  { kind: "meta", branch_id: "run-fork" },
  { kind: "lifecycle", type: "session_start", branch_id: "run-fork" },
];
assert.deepEqual(deriveRunVersions(versionFrames), [
  { branchId: "run-source", number: 1, label: "v1 · 원본" },
  { branchId: "run-fork", number: 2, label: "v2 · 분기" },
]);
assert.deepEqual(
  deriveRunVersions([
    { kind: "meta", branch_id: "run-masked", units: ["pii_mask"] },
    { kind: "meta", branch_id: "run-plain", units: [] },
  ]),
  [
    { branchId: "run-masked", number: 1, label: "v1 · 원본 · pii_mask" },
    { branchId: "run-plain", number: 2, label: "v2 · 분기 · 정책 없음" },
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
      branch_id: "run-source",
      outcome: { stop_reason: "completed" },
    },
  ], "streaming"),
  "terminal",
  "a selected completed version keeps its own status while another version streams",
);

// A run the ledger stopped is stopped. Missing these kinds left the header on 실행 중
// with no way out — the console claiming a run was in flight after it had ended.
for (const kind of ["indeterminate", "fenced", "contended"]) {
  assert.equal(
    deriveVersionPhase([
      { kind: "meta", branch_id: "run-x" },
      { kind, branch_id: "run-x", message: "..." },
    ]),
    "error",
    `${kind} ends the run`,
  );
}

const versionedTraceFrames = [
  { kind: "meta", branch_id: "run-v1", units: ["dlp_block"] },
  {
    kind: "lifecycle", branch_id: "run-v1", event_id: "v1-session",
    type: "session_start", payload: {},
  },
  {
    kind: "lifecycle", branch_id: "run-v1", event_id: "v1-submit",
    type: "user_prompt_submit", payload: { kind: "user_prompt" },
  },
  {
    kind: "lifecycle", branch_id: "run-v1", event_id: "v1-context",
    type: "context_injected", payload: { kind: "user_prompt" },
  },
  {
    kind: "agent", branch_id: "run-v1", event_id: "v1-send",
    event: { type: "tool_call", id: "send-v1", name: "send_email", input: {} },
  },
  {
    kind: "unit", branch_id: "run-v1", event_id: "v1-deny",
    unit: "dlp_block", verdict: "deny", message: "거부",
  },
  {
    kind: "meta", branch_id: "run-v2", units: [],
    fork_parent: "run-v1", fork_event_id: "v1-send", fork_edge: "before",
  },
  {
    kind: "lifecycle", branch_id: "run-v2", event_id: "v2-session",
    type: "session_start", payload: {},
  },
  {
    kind: "lifecycle", branch_id: "run-v2", event_id: "v2-submit",
    type: "user_prompt_submit", payload: { kind: "user_prompt" },
  },
  {
    kind: "lifecycle", branch_id: "run-v2", event_id: "v2-context",
    type: "context_injected", payload: { kind: "user_prompt" },
  },
  {
    kind: "agent", branch_id: "run-v2", event_id: "v2-send",
    event: { type: "tool_call", id: "send-v2", name: "send_email", input: {} },
  },
  {
    kind: "agent", branch_id: "run-v2", event_id: "v2-result",
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
  { kind: "meta", branch_id: "run-tools" },
  {
    kind: "agent", branch_id: "run-tools", event_id: "event-request",
    event: { type: "tool_call", id: "read-1", name: "read_customer", input: {} },
  },
  {
    kind: "lifecycle", branch_id: "run-tools", event_id: "event-pre",
    type: "pre_tool_use", payload: { call_id: "read-1", name: "read_customer" },
  },
  {
    kind: "lifecycle", branch_id: "run-tools", event_id: "event-post",
    type: "post_tool_use", payload: { call_id: "read-1", name: "read_customer" },
  },
  {
    kind: "agent", branch_id: "run-tools", event_id: "event-result",
    event: { type: "tool_result", id: "read-1", name: "read_customer", executed: true },
    restore_updates: [
      { event_id: "event-request", restore_edge: "after", boundary: "tool" },
      { event_id: "event-pre", restore_edge: "before", boundary: "tool" },
    ],
  },
  {
    kind: "lifecycle", branch_id: "run-tools", event_id: "event-context",
    type: "context_injected", payload: { kind: "tool_result", origin_id: "read-1" },
    forkable: true, restore_edge: "after", boundary: "result",
    restore_updates: [
      { event_id: "event-post", restore_edge: "after", boundary: "result" },
      { event_id: "event-result", restore_edge: "after", boundary: "result" },
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
const both = { applies: ["pii_mask", "dlp_block"], skipped: [] };
assert.equal(
  getForkActionLabel(restoredToolRows[0], both),
  "툴 실행 전 분기 · 적용 pii_mask, dlp_block",
);
assert.equal(
  getForkActionLabel(restoredToolRows[2], { applies: [], skipped: ["pii_mask", "dlp_block"] }),
  "툴 결과에서 분기 · 적용 없음 · 건너뜀 pii_mask, dlp_block",
  "a branch names what it will run and what it will not — a count said neither",
);

const leafVersionFrames = [
  { kind: "meta", branch_id: "leaf-v1" },
  {
    kind: "lifecycle", branch_id: "leaf-v1", event_id: "leaf-session",
    type: "session_start", payload: {},
  },
  {
    kind: "agent", branch_id: "leaf-v1", event_id: "leaf-request",
    event: { type: "tool_call", id: "read-1", name: "read_customer", input: {} },
  },
  {
    kind: "lifecycle", branch_id: "leaf-v1", event_id: "leaf-pre",
    type: "pre_tool_use", payload: { call_id: "read-1", name: "read_customer" },
  },
  {
    kind: "meta", branch_id: "leaf-v2", fork_parent: "leaf-v1",
    fork_event_id: "leaf-pre", fork_edge: "before", fork_mode: "leaf",
  },
  {
    kind: "lifecycle", branch_id: "leaf-v2", event_id: "leaf-child-session",
    type: "session_start", payload: {},
  },
  {
    kind: "lifecycle", branch_id: "leaf-v2", event_id: "leaf-child-pre",
    type: "pre_tool_use", payload: { call_id: "read-1", name: "read_customer" },
  },
  {
    kind: "lifecycle", branch_id: "leaf-v2", event_id: "leaf-child-post",
    type: "post_tool_use", payload: { call_id: "read-1", name: "read_customer" },
  },
];
assert.deepEqual(
  deriveVersionRows(leafVersionFrames, "leaf-v2").map(({ eventId }) => eventId),
  ["leaf-session", "leaf-request", "leaf-child-pre", "leaf-child-post"],
  "a leaf continuation keeps the parent prefix and does not trim its shorter child stream",
);

const chargeCall = (branchId, eventId, callId, customerId) => ({
  kind: "agent",
  branch_id: branchId,
  event_id: eventId,
  event: {
    type: "tool_call",
    id: callId,
    name: "charge_card",
    input: { customer_id: customerId, amount: "10" },
  },
});
const chargePre = (branchId, eventId, callId) => ({
  kind: "lifecycle",
  branch_id: branchId,
  event_id: eventId,
  type: "pre_tool_use",
  payload: { call_id: callId, name: "charge_card" },
});
const chargePost = (branchId, eventId, callId) => ({
  kind: "lifecycle",
  branch_id: branchId,
  event_id: eventId,
  type: "post_tool_use",
  payload: { call_id: callId, name: "charge_card" },
});
const replayedSplitFrames = [
  { kind: "meta", branch_id: "pay-v1" },
  {
    kind: "lifecycle", branch_id: "pay-v1", event_id: "pay-session",
    type: "session_start", payload: {},
  },
  chargeCall("pay-v1", "pay-v1-c1", "c-001", "c-001"),
  chargePre("pay-v1", "pay-v1-c1-pre", "c-001"),
  chargeCall("pay-v1", "pay-v1-c2", "c-002", "c-002"),
  chargePre("pay-v1", "pay-v1-c2-pre", "c-002"),
  chargeCall("pay-v1", "pay-v1-c3", "c-003", "c-003"),
  chargePre("pay-v1", "pay-v1-c3-pre", "c-003"),
  {
    kind: "meta", branch_id: "pay-v2", fork_parent: "pay-v1",
    fork_event_id: "pay-v1-c2-pre", fork_edge: "before", fork_mode: "leaf",
  },
  {
    kind: "lifecycle", branch_id: "pay-v2", event_id: "pay-v2-session",
    type: "session_start", payload: {},
  },
  chargeCall("pay-v2", "pay-v2-c1", "c-001", "c-001"),
  chargePre("pay-v2", "pay-v2-c1-pre", "c-001"),
  chargePost("pay-v2", "pay-v2-c1-post", "c-001"),
  chargeCall("pay-v2", "pay-v2-c2", "c-002", "c-002"),
  chargePre("pay-v2", "pay-v2-c2-pre", "c-002"),
  chargePost("pay-v2", "pay-v2-c2-post", "c-002"),
  chargeCall("pay-v2", "pay-v2-c3", "c-003", "c-003"),
  chargePre("pay-v2", "pay-v2-c3-pre", "c-003"),
  chargePost("pay-v2", "pay-v2-c3-post", "c-003"),
];
const replayedSplitRows = deriveVersionRows(replayedSplitFrames, "pay-v2");
assert.deepEqual(
  replayedSplitRows.map(({ eventId, versionOrigin, forkStart }) => ({
    eventId, versionOrigin, forkStart,
  })),
  [
    { eventId: "pay-session", versionOrigin: "inherited", forkStart: false },
    { eventId: "pay-v1-c1", versionOrigin: "inherited", forkStart: false },
    { eventId: "pay-v1-c1-pre", versionOrigin: "inherited", forkStart: false },
    { eventId: "pay-v1-c2", versionOrigin: "inherited", forkStart: false },
    { eventId: "pay-v2-c2-pre", versionOrigin: "current", forkStart: true },
    { eventId: "pay-v2-c2-post", versionOrigin: "current", forkStart: false },
    { eventId: "pay-v2-c3", versionOrigin: "current", forkStart: false },
    { eventId: "pay-v2-c3-pre", versionOrigin: "current", forkStart: false },
    { eventId: "pay-v2-c3-post", versionOrigin: "current", forkStart: false },
  ],
  "forking at event 2 keeps 1 shared and puts 2 and 3 on the branch, dropping the child's replay of 1",
);

const chargePerm = (branchId, eventId, callId) => ({
  kind: "lifecycle",
  branch_id: branchId,
  event_id: eventId,
  type: "permission_request",
  payload: { call_id: callId, name: "charge_card" },
});
const parallelCrashForkFrames = [
  { kind: "meta", branch_id: "par-v1" },
  {
    kind: "lifecycle", branch_id: "par-v1", event_id: "par-session",
    type: "session_start", payload: {},
  },
  {
    kind: "lifecycle", branch_id: "par-v1", event_id: "par-submit",
    type: "user_prompt_submit", payload: { kind: "user_prompt" },
  },
  {
    kind: "lifecycle", branch_id: "par-v1", event_id: "par-context",
    type: "context_injected", payload: { kind: "user_prompt" },
  },
  chargeCall("par-v1", "par-v1-c1", "c-001", "c-001"),
  chargeCall("par-v1", "par-v1-c2", "c-002", "c-002"),
  chargeCall("par-v1", "par-v1-c3", "c-003", "c-003"),
  chargePre("par-v1", "par-v1-c1-pre", "c-001"),
  chargePre("par-v1", "par-v1-c2-pre", "c-002"),
  {
    kind: "recoverable", branch_id: "par-v1", event_id: "par-crash",
    step: "c-002", message: "워커 장애",
  },
  chargePre("par-v1", "par-v1-c2-pre2", "c-002"),
  chargePre("par-v1", "par-v1-c3-pre", "c-003"),
  {
    kind: "meta", branch_id: "par-v2", fork_parent: "par-v1",
    fork_event_id: "par-v1-c3-pre", fork_edge: "before", fork_mode: "leaf",
  },
  {
    kind: "lifecycle", branch_id: "par-v2", event_id: "par-v2-session",
    type: "session_start", payload: {},
  },
  chargeCall("par-v2", "par-v2-c1", "c-001", "c-001"),
  chargePre("par-v2", "par-v2-c1-pre", "c-001"),
  chargePerm("par-v2", "par-v2-c1-perm", "c-001"),
  chargeCall("par-v2", "par-v2-c2", "c-002", "c-002"),
  chargePre("par-v2", "par-v2-c2-pre", "c-002"),
  chargePerm("par-v2", "par-v2-c2-perm", "c-002"),
  chargeCall("par-v2", "par-v2-c3", "c-003", "c-003"),
  chargePre("par-v2", "par-v2-c3-pre", "c-003"),
  chargePerm("par-v2", "par-v2-c3-perm", "c-003"),
];
const parallelCrashRows = deriveVersionRows(parallelCrashForkFrames, "par-v2");
assert.deepEqual(
  parallelCrashRows.map(({ eventId, versionOrigin, forkStart }) => ({
    eventId, versionOrigin, forkStart,
  })),
  [
    { eventId: "par-session", versionOrigin: "inherited", forkStart: false },
    { eventId: "par-submit", versionOrigin: "inherited", forkStart: false },
    { eventId: "par-context", versionOrigin: "inherited", forkStart: false },
    { eventId: "par-v2-c1", versionOrigin: "current", forkStart: true },
    { eventId: "par-v2-c1-pre", versionOrigin: "current", forkStart: false },
    { eventId: "par-v2-c1-perm", versionOrigin: "current", forkStart: false },
    { eventId: "par-v2-c2", versionOrigin: "current", forkStart: false },
    { eventId: "par-v2-c2-pre", versionOrigin: "current", forkStart: false },
    { eventId: "par-v2-c2-perm", versionOrigin: "current", forkStart: false },
    { eventId: "par-v2-c3", versionOrigin: "current", forkStart: false },
    { eventId: "par-v2-c3-pre", versionOrigin: "current", forkStart: false },
    { eventId: "par-v2-c3-perm", versionOrigin: "current", forkStart: false },
  ],
  "a gate fork inside a parallel batch starts the branch at the batch, not after v1's calls and crash",
);

const versionedChatFrames = [
  { kind: "meta", branch_id: "chat-v1" },
  {
    kind: "agent", branch_id: "chat-v1", event_id: "chat-read-request",
    event: { type: "tool_call", id: "read-1", name: "read_customer", input: {} },
  },
  {
    kind: "agent", branch_id: "chat-v1", event_id: "chat-read-result",
    event: { type: "tool_result", id: "read-1", name: "read_customer", executed: true },
  },
  {
    kind: "agent", branch_id: "chat-v1", event_id: "chat-send-request",
    event: { type: "tool_call", id: "send-1", name: "send_email", input: {} },
  },
  {
    kind: "lifecycle", branch_id: "chat-v1", event_id: "chat-send-pre",
    type: "pre_tool_use", payload: { call_id: "send-1", name: "send_email" },
  },
  {
    kind: "lifecycle", branch_id: "chat-v1", event_id: "chat-send-post",
    type: "post_tool_use", payload: { call_id: "send-1", name: "send_email" },
  },
  {
    kind: "agent", branch_id: "chat-v1", event_id: "chat-send-result",
    event: { type: "tool_result", id: "send-1", name: "send_email", executed: true },
  },
  {
    kind: "agent", branch_id: "chat-v1", event_id: "chat-old-text",
    event: { type: "text", text: "이전 응답" },
  },
  {
    kind: "meta", branch_id: "chat-v2", fork_parent: "chat-v1",
    fork_event_id: "chat-send-post", fork_edge: "after", fork_mode: "leaf",
  },
  {
    kind: "lifecycle", branch_id: "chat-v2", event_id: "chat-child-session",
    type: "session_start", payload: {},
  },
  {
    kind: "agent", branch_id: "chat-v2", event_id: "chat-new-text",
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
    kind: "agent", branch_id: "chat-v2", event_id: "chat-audit-request",
    event: { type: "tool_call", id: "audit-1", name: "write_audit", input: {} },
  },
  {
    kind: "lifecycle", branch_id: "chat-v2", event_id: "chat-audit-pre",
    type: "pre_tool_use", payload: { call_id: "audit-1", name: "write_audit" },
  },
  {
    kind: "agent", branch_id: "chat-v2", event_id: "chat-audit-old-result",
    event: { type: "tool_result", id: "audit-1", name: "write_audit", executed: false },
  },
  {
    kind: "meta", branch_id: "chat-v3", fork_parent: "chat-v2",
    fork_event_id: "chat-audit-pre", fork_edge: "before", fork_mode: "leaf",
  },
  {
    kind: "lifecycle", branch_id: "chat-v3", event_id: "chat-v3-session",
    type: "session_start", payload: {},
  },
  {
    kind: "lifecycle", branch_id: "chat-v3", event_id: "chat-audit-new-post",
    type: "post_tool_use", payload: { call_id: "audit-1", name: "write_audit" },
  },
  {
    kind: "agent", branch_id: "chat-v3", event_id: "chat-audit-new-result",
    event: { type: "tool_result", id: "audit-1", name: "write_audit", executed: true },
  },
  {
    kind: "agent", branch_id: "chat-v3", event_id: "chat-final-text",
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

// Which coordinate rebuilds a boundary is the projector's answer, not a scan over
// rows — tests/test_fork_demo.py holds that property. Here it is only carried.

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
assert.deepEqual(GUIDE.map((scene) => scene.scenarioId), [
  "charge", "charge", "leak", "crash", "unknown_effect", "fork_masking",
]);
assert.deepEqual(
  GUIDE.filter((scene) => scene.then).map((scene) => scene.scenarioId),
  ["leak", "fork_masking"],
  "a scene that needs the operator to act after the run parks says so",
);
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
assert.equal(nextGuideStep({ scenarioId: "crash", unitNames: ["approval"] }, "completed"), 4);
assert.equal(
  nextGuideStep({ scenarioId: "fork_masking", unitNames: ["pii_mask"] }, "completed"),
  GUIDE.length - 1,
  "the last scene stays put rather than running off the end",
);
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

// A run another worker holds is not this one's to finish, and not a failure either.
const contended = reduceFrames([
  { kind: "contended", worker: "worker-b", message: "다른 워커가 이 실행을 잡고 있습니다" },
]);
assert.deepEqual([contended[0].kind, contended[0].label], ["contended", "contended"]);
assert.equal(contended[0].details.worker, "worker-b");

// A worker back from the dead presents a token the run has already moved past.
const fenced = reduceFrames([
  { kind: "fenced", worker: "worker-a", presented: 1, issued: 2,
    message: "이 워커의 차례는 지났습니다" },
]);
assert.deepEqual([fenced[0].kind, fenced[0].tone], ["fenced", "deny"]);
assert.deepEqual([fenced[0].details.presented, fenced[0].details.issued], [1, 2]);

assert.equal(canSteer(terminal), false, "a finished run has nothing left to drain");
assert.equal(canSteer(recoverable), true);

// The operator cannot tell from the browser when the loop next drains; the server can.
assert.equal(steerSummary({ admits: "next_drain" }, false), "다음 도구 경계에 반영");
assert.equal(steerSummary({ admits: "on_resume" }, false), "재개될 때 반영");
assert.equal(steerSummary({}, false), "지시 대기");
assert.equal(steerSummary({ admits: "next_drain" }, true), "지시 반영");

// The projector stamps every coordinate of one boundary with a shared seam, so the
// button lands on the row that names it and branches from the last one.
const seamRows = [
  { kind: "lifecycle", label: "context_injected", forkable: true, forkEdge: "before",
    seam: "input:p1", eventId: "ev-input" },
  { kind: "tool", label: "read_customer 호출", forkable: true, forkEdge: "after",
    seam: "leaf-a", eventId: "ev-call" },
  { kind: "lifecycle", label: "pre_tool_use", forkable: true, forkEdge: "before",
    seam: "leaf-a", eventId: "ev-gate" },
  { kind: "lifecycle", label: "post_tool_use", forkable: true, forkEdge: "after",
    seam: "leaf-b", eventId: "ev-post" },
  { kind: "result", label: "read_customer 결과", forkable: true, forkEdge: "after",
    seam: "leaf-b", eventId: "ev-result" },
  { kind: "lifecycle", label: "context_injected", forkable: true, forkEdge: "after",
    seam: "leaf-b", eventId: "ev-ctx" },
  { kind: "policy", label: "dlp_block", forkable: false },
];
assert.deepEqual(
  seamRows.map((row, index) => (isForkRepresentative(seamRows, index)
    ? `${index}:${row.label}` : null)).filter(Boolean),
  ["0:context_injected", "2:pre_tool_use", "5:context_injected"],
  "one button per boundary, on the event it branches from",
);
assert.equal(isForkRepresentative(seamRows, 1), false, "a tool's name is not its gate");
assert.equal(isForkRepresentative(seamRows, 6), false, "a row that is no coordinate");

// A replayed call adds no transcript entry, so the boundary before it and the one after
// it restore to the same leaf while staying two different places to branch from. The
// projector names both; grouping by the leaf alone merged them into one button.
const replayedRows = [
  { kind: "lifecycle", label: "pre_tool_use", forkable: true, forkEdge: "before",
    seam: "run-child:leaf", boundary: "tool", eventId: "ev-pre" },
  { kind: "lifecycle", label: "post_tool_use", forkable: true, forkEdge: "after",
    seam: "run-child:leaf", boundary: "result", eventId: "ev-post" },
  { kind: "lifecycle", label: "context_injected", forkable: true, forkEdge: "after",
    seam: "run-child:leaf", boundary: "result", eventId: "ev-ctx" },
];
assert.deepEqual(
  replayedRows.map((row, index) => (isForkRepresentative(replayedRows, index)
    ? `${index}:${getForkActionLabel(row, { applies: [], skipped: [] })}` : null)).filter(Boolean),
  ["0:툴 실행 전 분기 · 적용 없음", "2:툴 결과에서 분기 · 적용 없음"],
  "one leaf, two boundaries, two buttons",
);

// A result coordinate holds the result a journal unit already rewrote, so changing
// that unit moves the branch back to the call. The button says so rather than naming a
// boundary it is leaving.
const resultRow = {
  eventId: "ev-result", label: "context_injected", forkable: true, forkEdge: "after",
  boundary: "result",
};
assert.equal(isRetargetedFork(resultRow, { event_id: "ev-gate" }), true);
assert.equal(isRetargetedFork(resultRow, { event_id: "ev-result" }), false);
assert.equal(isRetargetedFork(resultRow, null), false);
const one = { applies: ["pii_mask"], skipped: [] };
assert.equal(
  getForkActionLabel(resultRow, one, "retarget"),
  "저장된 결과를 버리고 툴부터 다시 · 적용 pii_mask",
);
assert.equal(getForkActionLabel(resultRow, one, null), "툴 결과에서 분기 · 적용 pii_mask");
assert.equal(
  getForkActionLabel({ label: "pre_tool_use", forkable: true, boundary: "tool" }, both),
  "툴 실행 전 분기 · 적용 pii_mask, dlp_block",
);
// The name comes from the projector, so an event renamed upstream keeps its button
// text — and a coordinate the projector did not name says nothing it cannot support.
assert.equal(
  getForkActionLabel({ label: "그 무엇이든", forkable: true, boundary: "input" }, one),
  "이 입력에서 분기 · 적용 pii_mask",
);
assert.equal(
  getForkActionLabel({ label: "pre_tool_use", forkable: true }, one),
  "이 지점에서 분기 · 적용 pii_mask",
);

// Approving with edited arguments: the form starts from what the model asked for, and
// only a real change travels as `args`.
const parkedFrames = [
  { kind: "meta", branch_id: "r-1" },
  { kind: "agent", event: { type: "tool_call", id: "c-1", name: "charge_card", input: { customer_id: "c-001", amount: "49" } } },
  { kind: "suspended", pending_id: "c-1" },
];
assert.deepEqual(pendingCallArgs(parkedFrames, "c-1"), { customer_id: "c-001", amount: "49" });
assert.equal(pendingCallArgs(parkedFrames, "missing"), null);
const proposed = pendingCallArgs(parkedFrames, "c-1");
assert.equal(editedArgs(JSON.stringify(proposed, null, 2), proposed), null, "untouched → no args");
assert.deepEqual(editedArgs('{"customer_id": "c-001", "amount": "5"}', proposed), { customer_id: "c-001", amount: "5" });
assert.throws(() => editedArgs("not json", proposed), SyntaxError);
assert.throws(() => editedArgs("[1]", proposed), SyntaxError, "an array is not a call's arguments");

console.log("run inspector state ok");
