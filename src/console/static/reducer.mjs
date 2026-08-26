// Pure stream-assembly reducer — the single source of truth for the run view.
// Folds the ndjson frames received so far into an ordered list of render rows.
// Contiguous text deltas collapse into ONE row; any structural frame closes the
// current text/thinking run so the next delta opens a fresh row. Shared by the live
// UI (app.js re-derives rows from all frames) and the headless test.

const LIFE_LABEL = {
  post_tool_use: "after_tool_call",
  post_tool_use_failure: "after_tool_call · fail",
  permission_denied: "pre_tool_use · deny",
  permission_request: "pre_tool_use · suspend",
  context_injected: "inject",
  user_prompt_submit: "on_inputs",
};

function lifeRow(f) {
  const t = f.type || "event";
  const p = f.payload || {};
  const hook = LIFE_LABEL[t] || t;
  const name = p.name || p.source || p.reason || p.kind || p.call_id || p.version || "";
  return { cls: "life", text: name ? `${hook}  ${name}` : hook };
}

export function reduceFrames(frames) {
  const rows = [];
  let openText = null; // index of the open text row, or null
  const pendingLife = [];

  const flushLife = () => {
    for (const row of pendingLife) rows.push(row);
    pendingLife.length = 0;
  };

  const close = () => {
    openText = null;
    flushLife();
  };

  const appendText = (incoming) => {
    if (openText === null) {
      rows.push({ cls: "text", text: "" });
      openText = rows.length - 1;
    }
    const prev = rows[openText].text;
    // Some providers resend the whole prefix each chunk; others send a delta.
    rows[openText].text = incoming.startsWith(prev) ? incoming : prev + incoming;
  };

  for (const f of frames) {
    if (f.kind === "meta") {
      continue;
    }
    if (f.kind === "agent") {
      const e = f.event || {};
      const piece = e.type === "text" ? (e.text || (typeof e.content === "string" ? e.content : "")) : "";
      if (e.type === "text" && piece) {
        appendText(piece);
      } else if (e.type === "thinking" && e.thinking) {
        // CoT is usually English on this model; the operator view is Korean.
        continue;
      } else if (e.type === "tool_call") {
        close();
        const args = JSON.stringify(e.input || {});
        const id = e.id || "";
        if (e.blocked) {
          const prev = [...rows].reverse().find((r) => r.cls === "tool" && r.id === id);
          if (prev) {
            prev.cls = "tool blocked";
            prev.text = `⊘ ${e.name}(${args}) · 실행 전 거부`;
          } else {
            rows.push({ cls: "tool blocked", text: `⊘ ${e.name}(${args}) · 실행 전 거부`, id });
          }
        } else {
          rows.push({ cls: "tool", text: `→ ${e.name}(${args})`, id });
        }
      } else if (e.type === "tool_result") {
        close();
        const r = e.result;
        const text = r && typeof r === "object" ? (r.text ?? r.message ?? "") : (e.text ?? e.message ?? "");
        const exec = r && typeof r === "object" ? r.execution_count : undefined;
        rows.push({ cls: "result", text: `← ${text}`, exec });
      } else if (e.type && e.type !== "thinking") {
        close();
        rows.push({ cls: "life", text: e.type });
      }
    } else if (f.kind === "steer") {
      close();
      const src = f.source === "user_steer" ? "운영자" : f.source === "control" ? "정책" : (f.source || "지시");
      const phase = f.phase === "queued" ? "대기" : "반영";
      rows.push({ cls: "steer", text: `⤷ ${phase} · ${src}  ${f.text || ""}` });
    } else if (f.kind === "unit") {
      close();
      rows.push({ cls: `unit v-${f.verdict}`, tag: f.unit, text: f.message });
    } else if (f.kind === "suspended") {
      close(); // the suspend card is rendered separately by app.js
    } else if (f.kind === "outcome") {
      close();
      const reason = (f.outcome && (f.outcome.stop_reason || f.outcome.reason)) || "done";
      rows.push({ cls: "outcome", text: `── 실행 종료 · ${reason} ──` });
    } else if (f.kind === "recoverable") {
      close();
      rows.push({ cls: "unit v-halt", tag: "crash", text: f.message || "워커 장애" });
    } else if (f.kind === "error") {
      close();
      rows.push({ cls: "unit v-deny", tag: "error", text: f.message });
    } else if (f.kind === "lifecycle") {
      const row = lifeRow(f);
      // Hold ticks while a text run is open. Flushing them immediately split
      // "이어서 c-003을" from the rest of the sentence and looked like a cut token.
      if (openText !== null) pendingLife.push(row);
      else rows.push(row);
    }
    // policy_summary is not a stream row — app.js renders the policy strip.
  }
  return rows;
}
