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
| `dlp_block` | `pre_tool_use` → Permissions | **Deny** | Scans an outbound send's **payload**; denies it if the body carries confidential data (email/SSN). Real egress DLP — it inspects what is leaving, not merely that a read happened. |
| `rate_cap` | `pre_tool_use` → Permissions | **Deny** | Denies effect calls past a per-run budget. |
| `pii_mask` | `after_tool_call` → Journal | **Rewrite** | Anonymizes email/SSN in a tool result in place — the model keeps a usable, masked record; raw PII never crosses to the model provider. |
| `context_firewall` | `after_tool_call` → Journal | **Block** | The strong form: replaces a confidential result **wholesale** with a policy notice, so the raw data never enters the model's context at all. |
| `log_gate` | `before_finish` → FinishPolicy | **Steer** | Vetoes completion until the outcome is logged, then lets the run finish. |

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

**The headline beat** — customer scenario, `approval` + `dlp_block` on, no ingest unit:
the agent reads raw PII and puts it in the email body; `send_email` is judged by both
`approval` (Suspend) and `dlp_block` (Deny) — and **Deny wins** (Permissions precedence).
Turn on `context_firewall` instead and the read is blanked, so nothing confidential is ever
in the body to leak. One screen, the composer's live precedence rule.

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
