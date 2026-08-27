# Fork Run Masking Demo Design

## Goal

Add a two-step portfolio scenario that first shows a prompt entering model context in masked form,
then calls `nexora_fork.fork_run` once to rewind the same conversation and re-run the ledger original
under a different control set.

The demo must make three facts visible:

- the source branch remains observable after the conversation head moves;
- the fork owns a new run ledger and event lineage;
- removing the masking screen lets the original value enter the new transcript and
  `context_injected` event, so the fork makes that value durable.

The existing denial trace is corrected in the same delivery so the deciding policy appears before
its generic permission consequence:

```text
dlp_block          DENY
permission_denied  send_email
send_email         실행 안 됨
```

## Chosen interaction

The scenario is deliberately two-step.

1. The operator selects `마스킹 이후 다시 실행` and starts it.
2. The source run builds a shared conversation prefix, injects the sensitive prompt through an
   input-masking screen, and completes with a masked active branch.
3. The terminal view exposes `원문으로 분기 실행`.
4. That button starts one real `fork_run` call with the input masker removed.
5. The chat area shows the preserved source branch and the new active fork branch separately.

This interaction was chosen over two alternatives:

- A one-click source-plus-fork sequence was rejected because it hides the security boundary where
  the host chooses new controls.
- A client-side branch simulation was rejected because it would not prove ledger restoration,
  transcript rewind, or new run lineage.

## Scenario and control unit

Add scenario metadata:

```python
{
    "id": "fork_masking",
    "title": "마스킹 이후 다시 실행",
    "risk": "원문이 새 실행 기록에 남음",
    "prompt": "ssn is 123-45",
    "default_units": ["input_mask"],
    "forkable": True,
}
```

Add `input_mask` at the `on_inputs` control point using Nexora's `Ingress` composer. It preserves
each `PendingInput.kind` and `origin_id` while replacing `123-45` and full SSN-shaped values in
human-message text with `***`.

This unit is distinct from the existing `pii_mask`:

- `input_mask` rewrites queued input before model context injection;
- `pii_mask` rewrites tool results after execution.

Selecting the fork scenario applies `default_units` in the composer. The operator may inspect or
change the source configuration before running it. The fork action always starts from the source
configuration with `input_mask` removed; the remaining selected units are passed to `fork_run`.

## Source-run construction

The source phase uses one conversation and two run ledgers:

```text
conversation: conv-<id>
run-a: fixed intro prompt -> first model response
run-b: origin p2, "ssn is 123-45" -> input_mask -> "ssn is ***" -> source response
```

The public stream is owned by `run-b`. Its session record stores:

- `conversation_id`;
- `prefix_run_id` (`run-a`);
- `source_run_id` (`run-b`);
- `origin_id` (`p2`-scoped unique value);
- the shared `Agent`;
- source unit names;
- whether a fork has already started.

Both source turns use the real `AgentRuntime` and transcript/store configured by the server. The
source prompt goes through `input_mask`, so the source transcript contains the masked value while
the `run-b` input ledger retains the pre-screen original.

After `run-b` completes, the server reads its committed history and emits a
`branch_snapshot` lifecycle frame:

```json
{
  "kind": "lifecycle",
  "type": "branch_snapshot",
  "payload": {
    "branch": "source",
    "run_id": "run-b",
    "conversation_id": "conv",
    "origin_id": "p2",
    "active": true,
    "messages": []
  }
}
```

`messages` is a UI-safe projection of committed messages: message id, role, and display content.
The source snapshot contains the masked value, not the ledger original.

## Fork endpoint

Add:

```text
POST /api/fork
{
  "run_id": "run-b"
}
```

The endpoint accepts only a completed `fork_masking` source session and rejects unknown,
non-forkable, in-flight, or already-forked sources with an appropriate 404 or 409 response.

It creates `run-c`, reuses the source conversation and agent, and calls:

```python
await fork_run(
    runtime,
    _store,
    from_run_id=source_run_id,
    origin_id=origin_id,
    run_id=fork_run_id,
    model=agent,
    controls=compose_controls(fork_units),
    conversation_id=conversation_id,
    on_event=on_event,
    should_stop_after_turn=_capped,
    aborted=lambda: _is_aborted(fork_run_id),
)
```

The runtime reads the original `PendingInput` from `run-b`, finds the masked message carrying the
same `origin_id` in committed history, submits the original to `run-c`, and starts from the history
prefix before that message.

When the call completes, the server emits a second `branch_snapshot` with `branch: "fork"`. The
source snapshot is retained in the browser and marked inactive; the fork snapshot is active. The
new snapshot contains the original value because the input masker was removed.

The fork stream emits a new `meta` frame and updates the inspector's active run id to `run-c`.
Source run data in `_sessions`, its ledger, and its event history are not edited.

## Event projection

`permission_denied` already contains its structured reason, including the deciding unit. The
server projects the policy row at that lifecycle boundary, immediately before the generic
permission row. It records the call id as projected so the later refused `tool_result` only upgrades
the tool row to `실행 안 됨`; it does not synthesize the policy a second time.

The relevant denial sequence is therefore:

```text
pre_tool_use
dlp_block          DENY
permission_denied
send_email         실행 안 됨
```

## Frontend state and presentation

The run state gains a fork transition available only from a completed `fork_masking` source run.
Starting the fork:

- keeps existing source frames and its branch snapshot;
- changes the phase to streaming;
- clears the terminal outcome strip;
- removes `input_mask` from the active fork configuration;
- accepts the new `meta.run_id` as the active run;
- appends fork events to the same trace.

The terminal action area shows `원문으로 분기 실행` only for a completed, unforked source phase.
Existing rerun actions remain available after the fork completes.

For this scenario the chat panel renders two labeled branch groups rather than flattening all
messages into one turn:

```text
이전 브랜치 · run-b
intro → response → ssn is *** → response

현재 브랜치 · run-c
intro → response → ssn is 123-45 → response
```

The fork group includes this warning next to its label:

> 원문이 새 원장과 대화 기록에 남습니다.

Raw `context_injected` payloads remain available only in the event details drawer. Trace summaries
must not copy sensitive payload content into the always-visible list.

## Dependency changes

The console installs the new extra and local workspace package:

```toml
dependencies = [
  "nexora[fork,openrouter]",
]

[tool.uv.sources]
nexora-fork = { path = "../nexora-python/packages/nexora-fork", editable = true }
```

The lockfile is regenerated so a clean `uv sync` installs `nexora-fork`.

## Errors and safety

- Missing source ledger record or missing transcript cut point is surfaced as a failed fork stream;
  no successful branch snapshot is emitted.
- A second fork request against the same source returns 409 and creates no new run.
- Forking never mutates or deletes source-run records.
- The frontend warning is shown before and after the fork action; it is not hidden in event details.
- The server never places the ledger original into source branch metadata.
- The original is expected in the fork transcript and `context_injected` payload. This is the
  demonstrated risk, not a redaction defect.

## Verification

Tests cover these observable contracts:

- `input_mask` preserves origin identity while masking content;
- the scenario selects `input_mask` by default and distinguishes it from tool-result masking;
- the denial projector produces exactly one `dlp_block` row before `permission_denied`;
- a real in-memory `fork_run` restores the source ledger original after the preserved prefix;
- source ledger and source snapshot stay unchanged while the fork receives a new run id;
- `/api/fork` rejects invalid and repeated requests without side effects;
- frontend state keeps the source snapshot, appends the fork snapshot, and switches active run id;
- the fork action and durable-original warning appear only in the intended phase;
- the full Python suite, stream/UI tests, JavaScript syntax check, and `git diff --check` pass.

Live acceptance runs the source phase, confirms `***` in the source branch, invokes the fork action,
then confirms the original value appears only in the new active branch.

## Out of scope

- Running two active branches concurrently;
- choosing an arbitrary historical message as the fork point;
- deleting or redacting the durable fork after execution;
- presenting a general conversation-tree explorer;
- changing `fork_run` semantics in the Nexora package.
