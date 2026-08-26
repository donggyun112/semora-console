import assert from "node:assert";
import { readFileSync } from "node:fs";

import { createNdjsonReader } from "../src/console/static/ndjson.mjs";
import { reduceFrames } from "../src/console/static/reducer.mjs";
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

// The browser integration consumes the immutable view state for one shared,
// representative live run; this remains a static contract so the suite needs
// no JS DOM implementation.
const appSource = readFileSync(
  new URL("../src/console/static/app.js", import.meta.url),
  "utf8",
);
assert.match(appSource, /createViewState[\s\S]*rerunWithoutPolicies[\s\S]*switchMode as nextMode/);
assert.match(appSource, /const initial = createViewState\(\);/);
assert.match(appSource, /mode: initial\.mode,[\s\S]*scenario: initial\.scenarioId,[\s\S]*units: new Set\(initial\.unitNames\)/);
assert.match(appSource, /function resetRunView\(\)[\s\S]*\$\("run-error"\)\.textContent = "";/);
assert.match(appSource, /setMode\("demo"\);/);
assert.match(appSource, /const usedPolicies = state\.units\.size > 0;[\s\S]*\$\("rerun-plain"\)\.classList\.toggle\("hidden", !usedPolicies\)/);
assert.match(appSource, /if \(!res\.ok \|\| !res\.body\) \{[\s\S]*실행 요청 실패/);
assert.match(appSource, /async function run\(\) \{\s*if \(state\.busy\) return;/);
assert.match(appSource, /async function runWithoutPolicies\(\) \{\s*if \(state\.busy\) return;/);

console.log("stream reducer ok");
