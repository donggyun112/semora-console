# Event-point forking implementation plan

> Execute with TDD. Preserve the existing uncommitted `nexora-fork` package and packaging changes in `../nexora-python`.

**Goal:** Give every visible event a durable audit coordinate, while exposing a fork action only where the console can restore that exact execution boundary.

**Architecture:** `nexora-fork` stores opaque `fork_checkpoint` entries in the existing append-only transcript. Each entry maps one observation `event_id` to `before` and `after` coordinates: source run, input origin, and transcript leaf. `fork_event` resolves that durable entry. The console records coordinates for audit/version lineage on every visible row. Input injection restores from its input origin; tool request and `pre_tool_use` rows are stabilized to the durable assistant tool-call leaf; `post_tool_use`, tool result, and tool-result injection rows are stabilized to the durable `ToolMessage` leaf. The UI activates an action only after that exact coordinate exists, and `/api/fork` enforces event ownership and the supported edge.

## Task 1: Add durable event checkpoints to `nexora-fork`

**Files:**
- Modify: `../nexora-python/packages/nexora-fork/src/nexora_fork/__init__.py`
- Modify: `../nexora-python/tests/test_fork.py`

1. Add failing tests proving a checkpoint survives in `MemoryTranscript`, `before` replays the ledger original through new controls, `after` continues from its explicit transcript leaf, and unknown event/edge fail without creating a new run input.
2. Add typed `ForkCoordinate` and `EventCheckpoint` values.
3. Add `record_event_checkpoint`, `read_event_checkpoint`, and `fork_event`.
4. Run `uv run --offline pytest -q tests/test_fork.py` from `../nexora-python`.

## Task 2: Record a checkpoint for every console event

**Files:**
- Modify: `src/console/server.py`
- Modify: `src/console/fork_demo.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_fork_demo.py`

1. Add failing stream tests asserting every visible lifecycle/agent/policy row has an `event_id` and a durable checkpoint.
2. Give the prefix prompt an explicit origin and keep an origin→source-run route in the source session.
3. While projecting frames, track input routes, transcript leaves, and pending tool boundaries by call ID; record each checkpoint and publish capability updates when an earlier tool row becomes durable.
4. Change `ForkRequest` to `{run_id, event_id, edge, units}` and execute `fork_event` from the selected checkpoint under the newly selected controls.
5. Preserve the existing source branch and emit the fork branch snapshot after completion.

## Task 3: Put the action on exact restore rows

**Files:**
- Modify: `src/console/static/app.js`
- Modify: `src/console/static/index.html`
- Modify: `src/console/static/styles.css`
- Modify: `src/console/static/reducer.mjs`
- Modify: `src/console/static/run-state.mjs`
- Modify: `tests/test_stream.mjs`
- Modify: `tests/test_server.py`

1. Add failing pure tests proving reduced rows retain `eventId` and the clicked event is sent to `/api/fork`.
2. Remove the global `#fork-run` terminal action.
3. Render a fork action beside exact input-injection and durable tool boundaries. Tool requests continue after the request row, `pre_tool_use` restarts before execution, and completed tool rows continue after the saved result without repeating the effect.
4. Treat each source/fork run as `v1`, `v2`, and later versions. Show one version's chat, event trace, outcome, and policy chips at a time, while keeping earlier versions selectable.
5. Keep the durable-original warning on the inline action. Use the post-run policy selection for the new version; the masking incident omits `input_mask` from that selection.
5. Run Node and static-shell tests.

## Task 4: Verify both repositories

1. `cd ../nexora-python && uv run --offline pytest -q tests/test_fork.py tests/test_packaging.py`
2. `cd ../nexora-python && uv run --offline ruff check packages/nexora-fork tests/test_fork.py`
3. `cd ../nexora-python && uv run --offline mypy packages/nexora-fork`
4. `cd nexora-console && .venv/bin/pytest -q`
5. `node --test tests/test_stream.mjs && node --check src/console/static/app.js`
6. Run compile, lockfile, and `git diff --check` in both repositories.
