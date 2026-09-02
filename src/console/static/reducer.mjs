function makeRow(sequence, kind, label, summary, options = {}) {
  const raw = options.details?.raw;
  const coordinateFrame = Array.isArray(raw) ? raw.at(-1) : raw;
  return {
    id: options.id ?? `${kind}:${sequence}`,
    kind,
    label,
    summary,
    verdict: options.verdict ?? null,
    badges: options.badges ?? [],
    tone: options.tone ?? "neutral",
    details: options.details ?? {},
    eventId: options.eventId ?? coordinateFrame?.event_id ?? null,
    forkOriginId: options.forkOriginId ?? coordinateFrame?.fork_origin_id ?? null,
    forkable: options.forkable ?? Boolean(coordinateFrame?.forkable),
    forkEdge: options.forkEdge ?? coordinateFrame?.restore_edge ?? null,
    // Which boundary this coordinate belongs to, decided by the projector that recorded
    // it. Rows sharing one are one branch, and the client never has to work that out.
    seam: options.seam ?? coordinateFrame?.seam ?? null,
    boundary: options.boundary ?? coordinateFrame?.boundary ?? null,
    // The control point the runtime enters first when restored here, per the framework.
    resumesAt: options.resumesAt ?? coordinateFrame?.resumes_at ?? null,
    rejournalAt: options.rejournalAt ?? coordinateFrame?.rejournal_at ?? null,
    // The coordinate that makes this boundary again instead of restoring it, for when the
    // operator changed a policy that only has something to say while the tool runs.
    rebuild: options.rebuild ?? coordinateFrame?.rebuild ?? null,
    runId: options.runId ?? null,
    callId: options.callId ?? coordinateFrame?.call_id ?? coordinateFrame?.payload?.call_id ?? null,
  };
}

function updateCoordinate(row, frame) {
  if (frame?.event_id) row.eventId = frame.event_id;
  if (frame?.fork_origin_id) row.forkOriginId = frame.fork_origin_id;
}

export function isResumeGate(frame) {
  // The runtime asks twice: pre_tool_use before the call, on_resume after a person
  // answers. Both arrive as pre_tool_use events, and labelling them alike made one
  // decision look like the same row printed twice.
  return frame?.type === "pre_tool_use" && (frame.payload ?? {}).source === "on_resume";
}

function lifecycleSummary(frame) {
  const payload = frame.payload ?? {};
  if (frame.type === "branch_snapshot") {
    return `${payload.branch ?? "unknown"} branch`;
  }
  if (isResumeGate(frame)) {
    return `승인 후 재검증 · ${payload.name ?? "tool"}`;
  }
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

// The ledger takes the steer either way; what changes is when the loop next drains.
const WHEN_ADMITTED = {
  next_drain: "다음 도구 경계에 반영",
  on_resume: "재개될 때 반영",
};

export function steerSummary(frame, admitted) {
  if (admitted) return "지시 반영";
  return WHEN_ADMITTED[frame?.admits] ?? "지시 대기";
}

function steerLabel(source) {
  if (source === "operator" || source === "user_steer") return "운영자";
  if (source === "policy" || source === "control") return "정책";
  return source ?? "steer";
}

// A call row and its result row both carried the bare tool name, so the trace read
// as the same event twice. The role rides on the label; the tool name stays intact.
export const toolCallLabel = (name) => `${name ?? "tool"} 호출`;
export const toolResultLabel = (name) => `${name ?? "tool"} 결과`;

export function toolResultOutput(event) {
  return event.output ?? event.result ?? event.content ?? null;
}

export const CALL_REPLAY_BADGE = {
  kind: "call_replay",
  label: "CALL REPLAY",
  detail: "도구 호출 생략",
};

export const PAYMENT_DEDUPE_BADGE = {
  kind: "payment_dedupe",
  label: "PAYMENT DEDUPE",
  detail: "결제 원장 재사용",
};

export const RESULT_MASK_BADGE = {
  kind: "result_mask",
  label: "RESULT MASK",
  detail: "도구 결과 가림",
};

export function resultBadges(event) {
  const result = toolResultOutput(event);
  const badges = [];
  if (result?.execution?.replayed === true) {
    badges.push({ ...CALL_REPLAY_BADGE });
  }
  if (event?.name === "charge_card" && result?.idempotency?.replayed === true) {
    badges.push({ ...PAYMENT_DEDUPE_BADGE });
  }
  if (result?.redacted_by) {
    badges.push({ ...RESULT_MASK_BADGE });
  }
  return badges;
}

function toolCallSummary(event) {
  const input = event.input ?? {};
  if (
    event.name === "charge_card"
    && input.customer_id != null
    && input.amount != null
  ) {
    return `${input.customer_id} · $${input.amount}`;
  }
  return "도구 호출 요청";
}

export function unitSummary(frame) {
  const message = frame.message ?? "정책 평가";
  if (!frame.call_id) return message;
  const target = toolCallSummary({ name: frame.name, input: frame.input });
  const label = target === "도구 호출 요청" ? frame.name : `${frame.name} · ${target}`;
  return label ? `${label} — ${message}` : message;
}

export function reduceFrames(frames) {
  const rows = [];
  const toolIndexes = new Map();
  const restored = new Map();
  let sequence = 0;
  let openText = null;
  let currentRunId = null;

  const append = (kind, label, summary, options = {}) => {
    const row = makeRow(sequence, kind, label, summary, {
      runId: currentRunId,
      ...options,
    });
    sequence += 1;
    rows.push(row);
    return row;
  };

  for (const frame of frames) {
    if (!frame || typeof frame !== "object") continue;
    for (const update of frame.restore_updates ?? []) {
      restored.set(update.event_id, {
        edge: update.restore_edge,
        seam: update.seam ?? null,
        boundary: update.boundary ?? null,
        resumesAt: update.resumes_at ?? null,
        rejournalAt: update.rejournal_at ?? null,
        rebuild: update.rebuild ?? null,
      });
    }

    if (frame.kind === "meta") {
      currentRunId = frame.run_id ?? currentRunId;
      openText = null;
      continue;
    }

    if (frame.kind === "agent" && frame.event?.type === "text") {
      const text = frame.event.text ?? "";
      if (!openText) {
        openText = append("agent", "agent", "응답 생성", {
          details: { output: text, raw: [frame] },
        });
      } else {
        openText.details.output += text;
        openText.details.raw.push(frame);
        updateCoordinate(openText, frame);
      }
      continue;
    }

    if (frame.kind !== "lifecycle") openText = null;

    if (frame.kind === "lifecycle") {
      const payload = frame.payload ?? {};
      const label = isResumeGate(frame) ? "on_resume" : frame.type ?? "lifecycle";
      append("lifecycle", label, lifecycleSummary(frame), {
        callId: payload.call_id ?? null,
        details: { raw: frame, output: payload.result ?? null },
      });
      continue;
    }

    if (frame.kind === "agent" && frame.event?.type === "tool_call") {
      const event = frame.event;
      const callId = event.id ?? `anonymous-${sequence}`;
      const stableId = `tool:${callId}`;
      const prior = toolIndexes.get(stableId);
      if (prior !== undefined) {
        prior.label = event.name ? toolCallLabel(event.name) : prior.label;
        prior.summary = event.blocked ? "실행 안 됨" : prior.summary;
        prior.verdict = event.blocked ? null : prior.verdict;
        prior.tone = event.blocked ? "deny" : prior.tone;
        prior.details = {
          ...prior.details,
          input: event.input ?? prior.details.input,
          raw: [...(prior.details.raw ?? []), frame],
        };
        if (!prior.eventId || !prior.forkOriginId) {
          updateCoordinate(prior, {
            event_id: prior.eventId ?? frame.event_id,
            fork_origin_id: prior.forkOriginId ?? frame.fork_origin_id,
          });
        }
      } else {
        const row = append(
          "tool",
          toolCallLabel(event.name),
          event.blocked ? "실행 안 됨" : toolCallSummary(event),
          {
            id: stableId,
            callId,
            verdict: null,
            tone: event.blocked ? "deny" : "neutral",
            details: { input: event.input ?? null, raw: [frame] },
          },
        );
        toolIndexes.set(stableId, row);
      }
      continue;
    }

    if (frame.kind === "agent" && frame.event?.type === "tool_result") {
      const event = frame.event;
      const result = toolResultOutput(event);
      const execution = result?.execution;
      const idempotency = result?.idempotency;
      // A refused call has a result too — the refusal — and calling it 실행 완료 would be
      // the console vouching for an effect the gate stopped.
      const refused = event.executed === false;
      append(
        "result",
        toolResultLabel(event.name),
        refused ? "실행 안 됨" : "실행 완료",
        {
          tone: refused ? "deny" : "neutral",
          callId: execution?.call_id ?? event.id ?? null,
          badges: refused ? [] : resultBadges(event),
          details: {
            output: result,
            executionCount: (
              result?.execution_count ??
              event.execution_count ??
              event.executionCount ??
              null
            ),
            callId: execution?.call_id ?? event.id ?? null,
            idempotencyKey: idempotency?.key ?? null,
            raw: frame,
          },
        },
      );
      continue;
    }

    if (frame.kind === "unit") {
      const verdict = String(frame.verdict ?? "").toUpperCase() || null;
      append("policy", frame.unit ?? "policy", unitSummary(frame), {
        callId: frame.call_id ?? null,
        verdict,
        tone: verdict?.toLowerCase() ?? "neutral",
        details: { input: frame.input ?? null, raw: frame },
      });
      continue;
    }

    if (frame.kind === "steer") {
      const admitted = (frame.status ?? frame.phase) === "admitted";
      append("steer", steerLabel(frame.source), steerSummary(frame, admitted), {
        tone: admitted ? "allow" : "neutral",
        details: { text: frame.text ?? frame.message ?? "", raw: frame },
      });
      continue;
    }

    if (frame.kind === "fenced") {
      append("fenced", "fenced", frame.message ?? "이 워커의 차례는 지났습니다", {
        tone: "deny",
        details: {
          worker: frame.worker ?? null,
          presented: frame.presented ?? null,
          issued: frame.issued ?? null,
          raw: frame,
        },
      });
      continue;
    }

    if (frame.kind === "contended") {
      append("contended", "contended", frame.message ?? "다른 워커가 잡고 있습니다", {
        tone: "halt",
        details: { worker: frame.worker ?? null, raw: frame },
      });
      continue;
    }

    if (frame.kind === "indeterminate") {
      append("indeterminate", "indeterminate", frame.message ?? "이 효과는 알 수 없습니다", {
        tone: "halt",
        details: { step: frame.step ?? null, raw: frame },
      });
      continue;
    }

    if (frame.kind === "recoverable") {
      // The approval-gate crash fires before the park is written, so the pre_tool_use
      // row it left behind records work that never committed. Drop that orphan or the
      // recovered replay reads as a duplicate of it. A crash at the commit seam carries
      // an effect key rather than a call id, so it matches nothing and keeps its rows.
      const voided = rows.findIndex((row) => (
        row.kind === "lifecycle"
        && row.label === "pre_tool_use"
        && row.callId === frame.step
      ));
      if (voided >= 0) rows.splice(voided, 1);
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

  for (const row of rows) {
    const update = restored.get(row.eventId);
    if (!update) continue;
    row.forkable = true;
    row.forkEdge = update.edge;
    row.seam = update.seam ?? row.seam;
    row.boundary = update.boundary ?? row.boundary;
    row.resumesAt = update.resumesAt ?? row.resumesAt;
    row.rejournalAt = update.rejournalAt ?? row.rejournalAt;
    row.rebuild = update.rebuild ?? row.rebuild;
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
