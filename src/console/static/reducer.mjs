// Pure stream-assembly reducer — the single source of truth for the run view.
// Folds the ndjson frames received so far into an ordered list of render rows.
// Contiguous text deltas collapse into ONE row; any structural frame closes the
// current text/thinking run so the next delta opens a fresh row. Shared by the live
// UI (app.js re-derives rows from all frames) and the headless test.

export function reduceFrames(frames) {
  const rows = [];
  let openText = null; // index of the open text row, or null
  let openThink = null;

  const close = () => {
    openText = null;
    openThink = null;
  };

  for (const f of frames) {
    if (f.kind === "meta") {
      continue;
    }
    if (f.kind === "agent") {
      const e = f.event || {};
      if (e.type === "text" && e.text) {
        if (openText === null) {
          rows.push({ cls: "text", text: "" });
          openText = rows.length - 1;
        }
        rows[openText].text += e.text;
      } else if (e.type === "thinking" && e.thinking) {
        if (openThink === null) {
          rows.push({ cls: "thinking", text: "" });
          openThink = rows.length - 1;
        }
        rows[openThink].text += e.thinking;
      } else if (e.type === "tool_call") {
        close();
        rows.push({ cls: "tool", text: `→ ${e.name}(${JSON.stringify(e.input || {})})` });
      } else if (e.type === "tool_result") {
        close();
        const r = e.result;
        const text = r && typeof r === "object" ? (r.text ?? r.message ?? "") : (e.text ?? e.message ?? "");
        const exec = r && typeof r === "object" ? r.execution_count : undefined;
        rows.push({ cls: "result", text: `← ${text}`, exec });
      }
    } else if (f.kind === "unit") {
      close();
      rows.push({ cls: `unit v-${f.verdict}`, tag: f.unit, text: f.message });
    } else if (f.kind === "suspended") {
      close(); // the suspend card is rendered separately by app.js
    } else if (f.kind === "outcome") {
      close();
      const reason = (f.outcome && (f.outcome.stop_reason || f.outcome.reason)) || "done";
      rows.push({ cls: "outcome", text: `── 실행 종료 · ${reason} ──` });
    } else if (f.kind === "error") {
      close();
      rows.push({ cls: "unit v-deny", tag: "error", text: f.message });
    }
    // policy_summary is not a stream row — app.js renders the policy strip.
  }
  return rows;
}
