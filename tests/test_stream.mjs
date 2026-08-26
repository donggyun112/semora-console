import assert from "node:assert";

import { createNdjsonReader } from "../src/console/static/ndjson.mjs";
import { reduceFrames } from "../src/console/static/reducer.mjs";
import { startWhenIdle } from "../src/console/static/run-guard.mjs";
import {
  DEFAULT_DEMO,
  createViewState,
  rerunWithoutPolicies,
  switchMode,
} from "../src/console/static/view-state.mjs";

// Contiguous text deltas collapse into one bubble; structural frames close it.
const rows = reduceFrames([
  { kind: "meta", run_id: "r" },
  { kind: "agent", event: { type: "tool_call", name: "read_customer", input: {} } },
  { kind: "unit", unit: "pii_mask", verdict: "rewrite", message: "masked" },
  { kind: "agent", event: { type: "text", text: "고객 " } },
  { kind: "agent", event: { type: "text", text: "요약" } },
  { kind: "outcome", outcome: { stop_reason: "completed" } },
]);

const textRows = rows.filter((r) => r.cls === "text");
assert.equal(textRows.length, 1, "deltas accumulate to one bubble");
assert.equal(textRows[0].text, "고객 요약", "delta text concatenated in order");
assert.ok(rows.some((r) => r.cls === "unit v-rewrite"), "unit verdict maps to class");

// A text run interrupted by a tool_call splits into two bubbles.
const split = reduceFrames([
  { kind: "agent", event: { type: "text", text: "A" } },
  { kind: "agent", event: { type: "tool_call", name: "x", input: {} } },
  { kind: "agent", event: { type: "text", text: "B" } },
]);
assert.equal(split.filter((r) => r.cls === "text").length, 2, "structural frame closes the bubble");

// tool_result surfaces nested result text + exec count.
const res = reduceFrames([
  { kind: "agent", event: { type: "tool_result", name: "charge_card", result: { text: "charged $49", execution_count: 1 } } },
]);
assert.equal(res[0].cls, "result");
assert.ok(res[0].text.includes("charged $49"));
assert.equal(res[0].exec, 1);

// A pending request upgrades in place when the same id is later blocked.
const blocked = reduceFrames([
  { kind: "agent", event: { type: "tool_call", id: "c1", name: "send_email", input: { to: "x" } } },
  { kind: "unit", unit: "dlp_block", verdict: "deny", message: "거부 — 메일 본문에 기밀" },
  { kind: "agent", event: { type: "tool_call", id: "c1", name: "send_email", input: { to: "x" }, blocked: true } },
]);
assert.equal(blocked.filter((r) => r.cls.startsWith("tool")).length, 1);
assert.equal(blocked[0].cls, "tool blocked");
assert.ok(blocked[0].text.includes("실행 전 거부"));
assert.ok(!blocked.some((r) => r.cls === "result"), "blocked call has no result row");

const parked = reduceFrames([
  { kind: "agent", event: { type: "tool_call", id: "c1", name: "charge_card", input: {} } },
  { kind: "unit", unit: "approval", verdict: "suspend", message: "승인 필요" },
]);
assert.equal(parked.find((r) => r.cls.startsWith("tool")).cls, "tool");
assert.ok(!parked.some((r) => r.cls === "tool blocked"));

// Every lifecycle frame is a tick. Nothing is dropped by whitelist.
const life = reduceFrames([
  { kind: "lifecycle", type: "pre_tool_use", payload: { name: "send_email" } },
  { kind: "lifecycle", type: "permission_denied", payload: { name: "send_email" } },
  { kind: "lifecycle", type: "context_injected", payload: { kind: "tool_result" } },
  { kind: "lifecycle", type: "session_start", payload: { source: "console" } },
  { kind: "lifecycle", type: "context_injected", payload: { kind: "control" } },
  { kind: "lifecycle", type: "setup", payload: { version: "1" } },
]);
assert.deepEqual(
  life.map((r) => r.text),
  [
    "pre_tool_use  send_email",
    "pre_tool_use · deny  send_email",
    "inject  tool_result",
    "session_start  console",
    "inject  control",
    "setup  1",
  ],
);

// Lifecycle ticks wait until the text run closes, then sit under the full sentence.
const mid = reduceFrames([
  { kind: "agent", event: { type: "text", text: "c-002도 정상 청구되었습니다. 이어서 c-003을" } },
  { kind: "lifecycle", type: "pre_tool_use", payload: { name: "charge_card" } },
  { kind: "agent", event: { type: "text", text: " 청구합니다." } },
  { kind: "lifecycle", type: "post_tool_use", payload: { name: "charge_card" } },
  { kind: "agent", event: { type: "tool_call", name: "charge_card", input: { customer_id: "c-003" } } },
]);
const midText = mid.filter((r) => r.cls === "text");
assert.equal(midText.length, 1, "lifecycle does not split a text run");
assert.equal(midText[0].text, "c-002도 정상 청구되었습니다. 이어서 c-003을 청구합니다.");
assert.deepEqual(
  mid.map((r) => r.cls),
  ["text", "life", "life", "tool"],
  "hook ticks follow the finished sentence, not the middle of it",
);

// While the sentence is still open, ticks stay off screen.
const open = reduceFrames([
  { kind: "agent", event: { type: "text", text: "이어서 c-003을" } },
  { kind: "lifecycle", type: "pre_tool_use", payload: { name: "charge_card" } },
]);
assert.deepEqual(open.map((r) => r.cls), ["text"]);
assert.equal(open[0].text, "이어서 c-003을");

// NDJSON: last line without a trailing newline is still delivered.
const frames = [];
const nd = createNdjsonReader((f) => frames.push(f));
nd.push(new TextEncoder().encode('{"kind":"agent","event":{"type":"text","text":"c-001"}}\n{"kind":"outcome","outcome":{"stop_reason":"completed"}}'));
nd.end();
assert.equal(frames.length, 2, "tail line without newline is not dropped");
assert.equal(frames[1].kind, "outcome");

// NDJSON: incomplete UTF-8 at a chunk boundary is flushed on end (Hangul is 3 bytes).
const hangul = [];
const nd2 = createNdjsonReader((f) => hangul.push(f));
const bytes = new TextEncoder().encode('{"kind":"agent","event":{"type":"text","text":"청구"}}\n');
nd2.push(bytes.slice(0, bytes.length - 2));
nd2.push(bytes.slice(bytes.length - 2));
nd2.end();
assert.equal(hangul.length, 1, "split UTF-8 reassembles");
assert.equal(hangul[0].event.text, "청구");

// One steer queue: operator and policy injects are the same row kind.
const steered = reduceFrames([
  { kind: "steer", source: "user_steer", text: "청구하지 마", phase: "queued" },
  { kind: "steer", source: "user_steer", text: "청구하지 마", phase: "admitted" },
  { kind: "steer", source: "control", text: "종료하기 전에 기록하라", phase: "admitted" },
]);
assert.equal(steered.filter((r) => r.cls === "steer").length, 3);
assert.ok(steered[0].text.includes("대기"));
assert.ok(steered[1].text.includes("반영"));

const crashed = reduceFrames([
  { kind: "recoverable", step: "c1", message: "워커 장애" },
]);
assert.equal(crashed[0].cls, "unit v-halt");
assert.equal(crashed[0].tag, "crash");

const initialView = createViewState();
assert.deepEqual(initialView, {
  mode: "demo",
  scenarioId: "leak",
  unitNames: ["approval", "dlp_block"],
});
assert.notStrictEqual(
  createViewState().unitNames,
  createViewState().unitNames,
  "each UI state owns its unit array",
);
assert.deepEqual(DEFAULT_DEMO, {
  scenarioId: "leak",
  unitNames: ["approval", "dlp_block"],
});

const composerView = switchMode(initialView, "composer");
assert.equal(composerView.mode, "composer");
assert.equal(initialView.mode, "demo", "mode transition does not mutate the input");
assert.throws(() => switchMode(initialView, "unknown"), /unknown mode/);

const plainView = rerunWithoutPolicies(composerView);
assert.equal(plainView.scenarioId, "leak");
assert.deepEqual(plainView.unitNames, []);
assert.deepEqual(composerView.unitNames, ["approval", "dlp_block"]);

// A continuation must not mutate UI or start a second request while another
// stream is active. The application uses this boundary before recover/resume.
const activeRun = { busy: true };
let mutations = 0;
assert.equal(
  await startWhenIdle(activeRun, async () => {
    mutations += 1;
  }),
  false,
);
assert.equal(mutations, 0, "active stream blocks continuation before mutation");

const idleRun = { busy: false };
assert.equal(
  await startWhenIdle(idleRun, async () => {
    mutations += 1;
    idleRun.busy = true;
  }),
  true,
);
assert.equal(mutations, 1, "idle continuation executes exactly once");

const concurrentRun = { busy: false };
let starts = 0;
let releaseContinuation;
const pendingContinuation = new Promise((resolve) => {
  releaseContinuation = resolve;
});
const firstContinuation = startWhenIdle(concurrentRun, async () => {
  starts += 1;
  concurrentRun.busy = true;
  await pendingContinuation;
});
assert.equal(
  await startWhenIdle(concurrentRun, async () => {
    starts += 1;
  }),
  false,
  "repeated activation does not start a concurrent continuation",
);
releaseContinuation();
assert.equal(await firstContinuation, true);
assert.equal(starts, 1);

function classList() {
  const values = new Set();
  const adds = new Map();
  return {
    add(name) {
      values.add(name);
      adds.set(name, (adds.get(name) || 0) + 1);
    },
    remove(name) {
      values.delete(name);
    },
    toggle(name, enabled) {
      if (enabled) values.add(name);
      else values.delete(name);
    },
    countAdds(name) {
      return adds.get(name) || 0;
    },
  };
}

function fakeElement() {
  const listeners = new Map();
  return {
    attributes: {},
    classList: classList(),
    className: "",
    dataset: {},
    disabled: false,
    listeners,
    textContent: "",
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    append() {},
    appendChild(child) {
      return child;
    },
    focus() {},
    querySelector() {
      return null;
    },
    replaceChildren() {},
    scrollIntoView() {},
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
  };
}

function fakeStreamResponse(runId) {
  let sendMeta = Boolean(runId);
  let finish;
  const done = new Promise((resolve) => {
    finish = resolve;
  });
  return {
    finish,
    response: {
      ok: true,
      body: {
        getReader() {
          return {
            async read() {
              if (sendMeta) {
                sendMeta = false;
                return {
                  value: new TextEncoder().encode(
                    JSON.stringify({ kind: "meta", run_id: runId }) + "\n",
                  ),
                  done: false,
                };
              }
              await done;
              return { done: true };
            },
          };
        },
      },
    },
  };
}

async function tick() {
  await new Promise((resolve) => setImmediate(resolve));
}

// Exercise the app module's actual recovery and approval entry points. The
// fake DOM is deliberately minimal: it supplies only the browser operations
// used while booting and continuing a run.
const elements = Object.fromEntries(
  [
    "model", "scenarios", "units", "compose-summary", "demo-index",
    "demo-title", "demo-does", "demo-risk", "demo-prompt", "demo-policies",
    "workspace", "demo-panel", "composer-panel", "open-composer",
    "rerun-plain", "abort", "steer-form", "recover", "approve", "deny",
    "steer-box", "status", "stream", "steer-queue", "policy-strip",
    "approval", "recovery", "run-error", "boot-error-message", "boot-error",
    "boot-retry", "mode-demo", "mode-composer", "demo-run", "run",
  ].map((id) => [id, fakeElement()]),
);
globalThis.document = {
  createElement: fakeElement,
  createTextNode(text) {
    return { textContent: text };
  },
  getElementById(id) {
    return elements[id];
  },
  querySelectorAll(selector) {
    if (selector === "[data-run]") return [elements["demo-run"], elements.run];
    if (selector === "[data-mode]") return [elements["mode-demo"], elements["mode-composer"]];
    return [];
  },
};

const requests = [];
const continuations = [];
globalThis.fetch = async (url, init) => {
  if (url === "/api/scenarios") {
    return {
      ok: true,
      json: async () => [{
        id: "leak",
        title: "Leak",
        does: "Tests controls",
        risk: "위험",
        prompt: "send it",
      }],
    };
  }
  if (url === "/api/units") {
    return {
      ok: true,
      json: async () => ({
        model: "test-model",
        units: [
          { name: "approval", point: "pre_tool_use", composer: "gate", verdict: "SUSPEND", desc: "" },
          { name: "dlp_block", point: "pre_tool_use", composer: "gate", verdict: "DENY", desc: "" },
        ],
      }),
    };
  }
  const continuation = fakeStreamResponse(url === "/api/recover" ? "run-live" : null);
  requests.push({ url, body: JSON.parse(init.body) });
  continuations.push(continuation);
  return continuation.response;
};

await import(new URL(`../src/console/static/app.js?integration=${Date.now()}`, import.meta.url));
for (let i = 0; i < 4; i++) await tick();
assert.equal(requests.length, 0, "boot does not auto-run");

const recoveryRun = elements.recover.listeners.get("click")();
for (let i = 0; i < 3; i++) await tick();
assert.equal(requests.filter((request) => request.url === "/api/recover").length, 1);
assert.equal(elements.recovery.classList.countAdds("hidden"), 1);
elements.recover.listeners.get("click")();
await tick();
assert.equal(requests.filter((request) => request.url === "/api/recover").length, 1);
assert.equal(elements.recovery.classList.countAdds("hidden"), 1);
continuations[0].finish();
await recoveryRun;

elements.approval.dataset.pending = "pending-9";
const resumeRun = elements.approve.listeners.get("click")();
for (let i = 0; i < 3; i++) await tick();
assert.equal(requests.filter((request) => request.url === "/api/resume").length, 1);
assert.deepEqual(requests[1].body, {
  run_id: "run-live",
  pending_id: "pending-9",
  approved: true,
});
assert.equal(elements.approval.classList.countAdds("hidden"), 1);
elements.approve.listeners.get("click")();
await tick();
assert.equal(requests.filter((request) => request.url === "/api/resume").length, 1);
assert.equal(elements.approval.classList.countAdds("hidden"), 1);
continuations[1].finish();
await resumeRun;

console.log("stream reducer ok");
