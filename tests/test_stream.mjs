import assert from "node:assert";

import { reduceFrames } from "../src/console/static/reducer.mjs";

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

console.log("stream reducer ok");
