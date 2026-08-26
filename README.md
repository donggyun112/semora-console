# Nexora Control Plane Console

**You are the operator.** An AI agent holds real, effectful tools — it can charge a card,
send an email, write to a store. Compose [nexora](../nexora-python)'s control plane from
**units across different lifecycle hooks**, and watch the *same task* behave differently by
policy alone.

> **Seven hooks. Every hook composes the same way.**

```
on_inputs → Ingress   pre_tool_use → Permissions   after_tool_call → Journal
before_model → Steering   before_finish → FinishPolicy   on_suspend → Suspending
```

nexora's control plane is seven hooks, and every one composes variadically. A **unit** here
declares the hook it attaches to and one function with that hook's signature;
`compose_controls` groups the selected units by hook and assembles a single `ControlPlane`.
Each run stores one `Agent` definition that owns its model, tools, and system prompt;
`AgentRuntime` owns attempt state, controls, durable suspension, and recovery.

## The units

| Unit | Hook → Composer | Verdict | What it does |
| --- | --- | --- | --- |
| `approval` | `pre_tool_use` → Permissions | **Suspend** | Halts the whole loop for human sign-off on any effect (write/charge/send); a pure read passes. |
| `dlp_block` | `pre_tool_use` → Permissions | **Deny** | Scans an outbound send's **payload**; denies it if the body carries confidential data (email/SSN). Real egress DLP — it inspects what is leaving, not merely that a read happened. |
| `rate_cap` | `pre_tool_use` → Permissions | **Deny** | Denies irreversible effects (charge/send) past a per-run budget. Session logging is not counted, so `log_gate` can still record after the cap. |
| `pii_mask` | `after_tool_call` → Journal | **Rewrite** | Anonymizes email/SSN in a tool result in place — the model keeps a usable, masked record; raw PII never crosses to the model provider. |
| `context_firewall` | `after_tool_call` → Journal | **Block** | The strong form: replaces a confidential result **wholesale** with a policy notice, so the raw data never enters the model's context at all. |
| `injection_guard` | `after_tool_call` → Journal | **Rewrite** | Decomposes any tool result and forwards `{신뢰할 수 없는 상태, source, structure}` — does not inspect or drop content. |
| `log_gate` | `before_finish` → FinishPolicy | **Steer** | Vetoes completion until the outcome is logged. The `Proceed` is not a second channel — it enqueues on the run's one steer queue. |

**The two ingest units are a matched pair** (`examples/04_control_plane.py`'s lesson —
policy lands at a seam and reaches a destination): both run at `after_tool_call` and
guard **ingest** — what enters the model. `pii_mask` anonymizes and lets the model keep
working; `context_firewall` blanks the result entirely. Both reach the model's view and
the UI stream but **not** the durable ledger copy (recorded inside the durable step, before
any hook) — masking that is a Tools-wrapper seam, out of scope by design.

**Why ingest is the real data boundary.** With a third-party model, a tool result egresses
to the provider's network the moment it enters the *next* model request. So blocking a
later `send_email` does **not** protect that data — it already left. The only real
protection is at ingest (`pii_mask` / `context_firewall`), which rewrites the result
*before* the model sees it. `dlp_block` is a distinct control: it stops confidential data
from leaving via an **outbound message channel** to another recipient — honest egress DLP,
not data-hiding.

**Home scenarios.** `customer` is the PII ingest beat (`pii_mask` / `context_firewall`).
`inject` is the untrusted-structure beat (`injection_guard`): whatever the tool returned
is forwarded as structured untrusted data. `leak` is the egress beat (`dlp_block`).
**One agent, one steering queue.** Operator `user_steer` and policy `Proceed` (e.g. `log_gate`)
both land on `drain_inputs`. There is no per-scenario steer unit and no second channel.
While a run is in flight the operator can enqueue a nudge (`POST /api/steer`); it admits
on the next model call. Stop is a button. Steer is a queue.

`charge` is a single irreversible effect (`approval`). `parallel` is the same gate on a
**batch**: three `charge_card` calls in one model turn, one suspend for the bundle.
`parallel_crash` kills the worker after the first committed call; recovery replays that
result and executes the remaining calls so all three effects finish exactly once.
`batch` is sequential so `rate_cap` can trip on the third.

**Stop is a button, not a unit.** While a run is in flight the operator can hit **중단**;
that trips Nexora's `aborted()` hook (`POST /api/abort`) and cancels the attempt, on any
scenario and any assembled plane. There is no locked "stop" task.

**The headline beat** — leak scenario, `approval` + `dlp_block` on, no ingest unit:
the agent reads raw SSN and tries to send it to a personal inbox; `send_email` is judged
by both `approval` (Suspend) and `dlp_block` (Deny) — and **Deny wins** (Permissions
precedence). Turn on `context_firewall` instead and the read is blanked, so nothing
confidential is ever in the body to leak. One screen, the composer's live precedence rule.

## Run locally

```bash
cp .env.example .env   # add your OPENROUTER_API_KEY
uv sync
uv run uvicorn console.server:app --reload --port 8850
```

Open <http://127.0.0.1:8850>. Verify the composition logic with no server or API key:

```bash
uv run python -m console.units   # prints "units self-check ok"
uv run pytest                    # composition + streaming-assembly tests
```

## Durable exactly-once

`approval` suspends a call and persists a continuation to the step ledger; resuming runs
the effect **exactly once** (`exec ×1` in the UI). Within one process this uses the
in-memory ledger. To prove it **survives a restart**, set `DATABASE_URL` (Postgres) and run
on one long-lived machine:

```
POST /api/run (approval + charge) → suspended
# kill and restart the process
POST /api/resume by run_id → the charge runs once (exec ×1), across the restart
```

**청구 중 장애** and **동시 청구 중 장애** arm a worker crash after the first tool
`finish_effect`. In the parallel case the committed result is replayed and the absent
siblings execute during **복원**: `POST /api/recover` → `AgentRuntime.recover`
(`retry_running=False`). Process restart still needs `DATABASE_URL`.

## Layout

| File | Role |
| --- | --- |
| `units.py` | The substance — units + `compose_controls()` + self-check. |
| `scenarios.py` | 9 locked scenarios (no free-text → no LLM-proxy abuse). |
| `dormancy.py` | Why a toggled-on unit stayed dormant in a scenario. |
| `tools.py` | Demo effects (`read_customer` returns PII, `charge_card`, `send_email`, `remember_note`). |
| `store.py` | In-memory ledger locally, Postgres when `DATABASE_URL` is set. |
| `server.py` | FastAPI: `/api/run`, `/api/resume`, `/api/recover`, `/api/abort`, `/api/steer`, `/api/units`, static UI. |
