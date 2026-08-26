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

## The units

| Unit | Hook → Composer | Verdict | What it does |
| --- | --- | --- | --- |
| `approval` | `pre_tool_use` → Permissions | **Suspend** | Halts the whole loop for human sign-off on any effect (write/charge/send); a pure read passes. |
| `dlp_block` | `pre_tool_use` → Permissions | **Deny** | Refuses an outbound send once customer data was read — the **outbound** boundary (a block). |
| `rate_cap` | `pre_tool_use` → Permissions | **Deny** | Denies effect calls past a per-run budget. |
| `pii_mask` | `after_tool_call` → Journal | **Rewrite** | Masks email/SSN in a tool result in place — the **ingest** boundary; raw PII never reaches the model or the UI. |
| `log_gate` | `before_finish` → FinishPolicy | **Steer** | Vetoes completion until the outcome is logged, then lets the run finish. |

**Two boundaries, never conflated** (the lesson of `examples/04_control_plane.py`):
`pii_mask` guards **ingest** (what enters the model); `dlp_block` guards **egress** (what
leaves the org). `pii_mask` runs at `after_tool_call`, so it reaches the model's view and
the UI stream but **not** the durable ledger copy — masking that is a different seam (a
Tools wrapper), out of this unit's scope by design. We don't claim otherwise.

**The headline beat** — customer scenario with `approval` + `dlp_block` + `pii_mask` on:
`read_customer` is masked by `pii_mask`; then `send_email` is judged by both `approval`
(Suspend) and `dlp_block` (Deny) — and **Deny wins** (Permissions precedence). One screen,
the composer's live precedence rule.

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

An in-UI "kill the server" button is deliberately omitted — a fake crash would undercut the
coherence; the real durability proof is this deploy path (and `tests/`).

## Layout

| File | Role |
| --- | --- |
| `units.py` | The substance — 5 units + `compose_controls()` + self-check. |
| `scenarios.py` | 4 locked scenarios (no free-text → no LLM-proxy abuse). |
| `dormancy.py` | Why a toggled-on unit stayed dormant in a scenario. |
| `tools.py` | Demo effects (`read_customer` returns PII, `charge_card`, `send_email`, `remember_note`). |
| `store.py` | In-memory ledger locally, Postgres when `DATABASE_URL` is set. |
| `server.py` | FastAPI: `/api/run`, `/api/resume`, `/api/units`, static UI, policy summary. |
