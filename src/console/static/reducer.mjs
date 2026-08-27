function makeRow(sequence, kind, label, summary, options = {}) {
  return {
    id: options.id ?? `${kind}:${sequence}`,
    kind,
    label,
    summary,
    verdict: options.verdict ?? null,
    tone: options.tone ?? "neutral",
    details: options.details ?? {},
  };
}

function lifecycleSummary(frame) {
  const payload = frame.payload ?? {};
  return (
    payload.name ??
    payload.source ??
    payload.reason ??
    payload.kind ??
    payload.version ??
    frame.message ??
    "lifecycle event"
  );
}

function steerLabel(source) {
  if (source === "operator" || source === "user_steer") return "운영자";
  if (source === "policy" || source === "control") return "정책";
  return source ?? "steer";
}

function toolResultOutput(event) {
  return event.output ?? event.result ?? event.content ?? null;
}

export function reduceFrames(frames) {
  const rows = [];
  const toolIndexes = new Map();
  let sequence = 0;
  let openText = null;

  const append = (kind, label, summary, options = {}) => {
    const row = makeRow(sequence, kind, label, summary, options);
    sequence += 1;
    rows.push(row);
    return row;
  };

  for (const frame of frames) {
    if (!frame || typeof frame !== "object") continue;

    if (frame.kind === "agent" && frame.event?.type === "text") {
      const text = frame.event.text ?? "";
      if (!openText) {
        openText = append("agent", "agent", "응답 생성", {
          details: { output: text, raw: [frame] },
        });
      } else {
        openText.details.output += text;
        openText.details.raw.push(frame);
      }
      continue;
    }

    if (frame.kind !== "lifecycle") openText = null;

    if (frame.kind === "lifecycle") {
      append("lifecycle", frame.type ?? "lifecycle", lifecycleSummary(frame), {
        details: { raw: frame },
      });
      continue;
    }

    if (frame.kind === "agent" && frame.event?.type === "tool_call") {
      const event = frame.event;
      const callId = event.id ?? `anonymous-${sequence}`;
      const stableId = `tool:${callId}`;
      const priorIndex = toolIndexes.get(stableId);
      if (priorIndex !== undefined) {
        const prior = rows[priorIndex];
        prior.label = event.name ?? prior.label;
        prior.summary = event.blocked ? "실행 안 됨" : prior.summary;
        prior.verdict = event.blocked ? null : prior.verdict;
        prior.tone = event.blocked ? "deny" : prior.tone;
        prior.details = {
          ...prior.details,
          input: event.input ?? prior.details.input,
          raw: [...(prior.details.raw ?? []), frame],
        };
      } else {
        const row = append("tool", event.name ?? "tool", event.blocked ? "실행 안 됨" : "도구 호출 요청", {
          id: stableId,
          verdict: null,
          tone: event.blocked ? "deny" : "neutral",
          details: { input: event.input ?? null, raw: [frame] },
        });
        toolIndexes.set(stableId, rows.indexOf(row));
      }
      continue;
    }

    if (frame.kind === "agent" && frame.event?.type === "tool_result") {
      const event = frame.event;
      append("result", event.name ?? "tool result", "실행 완료", {
        details: {
          output: toolResultOutput(event),
          executionCount: event.execution_count ?? event.executionCount ?? null,
          raw: frame,
        },
      });
      continue;
    }

    if (frame.kind === "unit") {
      const verdict = String(frame.verdict ?? "").toUpperCase() || null;
      append("policy", frame.unit ?? "policy", frame.message ?? "정책 평가", {
        verdict,
        tone: verdict?.toLowerCase() ?? "neutral",
        details: { raw: frame },
      });
      continue;
    }

    if (frame.kind === "steer") {
      const admitted = (frame.status ?? frame.phase) === "admitted";
      append("steer", steerLabel(frame.source), admitted ? "지시 반영" : "지시 대기", {
        tone: admitted ? "allow" : "neutral",
        details: { text: frame.text ?? frame.message ?? "", raw: frame },
      });
      continue;
    }

    if (frame.kind === "recoverable") {
      append("recovery", "recover", frame.message ?? "worker failure", {
        tone: "halt",
        details: { raw: frame },
      });
      continue;
    }

    if (frame.kind === "outcome") {
      const stopReason = frame.outcome?.stop_reason ?? frame.stop_reason ?? "completed";
      append("outcome", "outcome", stopReason, {
        tone: stopReason === "completed" ? "allow" : "halt",
        details: { raw: frame },
      });
      continue;
    }

    if (frame.kind === "error") {
      append("error", "error", frame.message ?? "run failed", {
        tone: "deny",
        details: { raw: frame },
      });
    }
  }

  return rows;
}

export function summarizeOutcome(frames) {
  let blockingPolicy = null;
  let blockedTool = null;
  let stopReason = null;

  for (const frame of frames) {
    if (frame?.kind === "unit" && String(frame.verdict).toLowerCase() === "deny") {
      blockingPolicy = frame;
    }
    if (
      frame?.kind === "agent" &&
      frame.event?.type === "tool_call" &&
      frame.event.blocked
    ) {
      blockedTool = frame.event;
    }
    if (frame?.kind === "outcome") {
      stopReason = frame.outcome?.stop_reason ?? frame.stop_reason ?? stopReason;
    }
  }

  if (blockedTool) {
    return {
      verdict: String(blockingPolicy?.verdict ?? "deny").toUpperCase(),
      tool: blockedTool.name ?? "tool",
      result: "실행 안 됨",
    };
  }

  const completed = stopReason === "completed";
  return {
    verdict: completed ? "ALLOW" : String(stopReason ?? "UNKNOWN").toUpperCase(),
    tool: null,
    result: completed ? "완료" : "중단됨",
  };
}
