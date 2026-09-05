# Semora Control Plane Console

An operator console for Semora 0.3, the execution extension for Pydantic AI. Choose
policies, run a fixed scenario, approve a suspended tool call, recover a simulated
worker crash, or branch a completed run from a recorded input or tool boundary.

Pydantic AI owns the agent, model client, tools, messages, and model loop. Semora owns
effect execution, leases, suspension, approval revalidation, and durable dispatch.
The console adds business effect keys, scenario policies, and the browser frame projection.

## Run locally

Keep this checkout beside the canonical `semora` checkout. Semora 0.3 has not been
published: `pyproject.toml` and `uv.lock` resolve its three packages from `../semora`.

```sh
uv sync
export OPENROUTER_API_KEY=...
uv run uvicorn console.server:app --host 127.0.0.1 --port 8850
```

Open http://127.0.0.1:8850. `MODEL` selects the OpenRouter model; the default is
`~deepseek/deepseek-v4-flash-latest`. Pydantic AI's native OpenAI-compatible client
handles model responses and tool calls. No custom DSML repair is installed.
Model requests have a 60-second timeout; the console stops at its eighth model step.
Tool and control frames stream as their boundaries execute. Assistant text arrives as
one complete text event at completion; token and thinking chunks are not forwarded.
This keeps model calls on Semora's durable model-request path.

Without `DATABASE_URL`, steps and transcripts are process-local memory stores.
Set `DATABASE_URL` to a **fresh PostgreSQL database** for durable operation.
Semora 0.2/LangChain continuations and transcripts cannot be resumed by 0.3; this
transition does not migrate in-flight runs or reinterpret their stored messages.

```sh
docker compose up --build
```

Compose supplies `../semora` as an additional build context, so the image installs
the same local packages. Its new `console-ledger-v03` volume keeps 0.3 data separate
from the previous `console-ledger` volume. The old volume is retained. Use
`docker compose --profile two-workers up --build` for a second worker on port 8851.
`CONSOLE_LEASE_TTL` controls lease duration (default 60 seconds).

## Policies and scenarios

| Unit | Real control point | Behavior |
| --- | --- | --- |
| `input_mask` | `on_inputs` | Masks SSN-shaped input before the model sees it. |
| `approval` | `pre_tool_use` | Parks writes, charges, and sends for a person's answer. |
| `dlp_block` | `pre_tool_use` | Denies email payloads containing emails or SSNs. |
| `rate_cap` | `pre_tool_use` | Allows two charge/send requests; note writes are outside this budget. |
| `pii_mask` | `post_tool_use` | Masks email/SSN values in the model-visible result. |
| `context_firewall` | `post_tool_use` | Replaces a confidential result with a policy notice. |
| `injection_guard` | `post_tool_use` | Labels tool data untrusted while retaining its structure. |
| `result_drop` | `post_tool_use` | Discards the observation after its effect has happened. |
| `log_gate` | `before_finish` | Continues the agent with a native prompt part until it requests a note. |

The seven seams also include `before_model`, `on_resume`, and `on_suspend`.
`compose_controls` composes selected policies into one `ControlPlane`. Current
permissions run again on approval; a new denial outranks an earlier approval.
Operator steering enters Semora's durable input queue; finish-policy steering uses
Pydantic AI request parts in the next model request. The abort button drops queued
input and cancels the live attempt.

`customer` demonstrates PII handling; `inject` labels indirect instructions as data;
`leak` demonstrates outbound DLP. `charge`, `batch`, and `parallel` exercise single,
sequential, and batched effects. A parallel approval parks the whole batch until
every requested answer arrives, then executes in model order.

`crash` and `parallel_crash` stop after a recorded effect, or before approval is parked
when the approval unit is selected. Recovery reuses the recorded model round and
completed effects. `unknown_effect` stops after the payment leaves but before its
record lands: recovery reports **indeterminate** and never retries that effect.

## Effect identity and branching

The operator/scenario session owns business effects. Each call uses
`tool:{call_id}` within its Semora execution run; each payment also uses the session
business key `charge:{request}:{customer}`.
The request is the origin prompt ID: recoveries, resumes, and forks inherit it;
a new request gets a new ID. A changed amount conflicts with the recorded payment.
These session steps acquire and renew a lease, fence writes, and retain interrupted
intent. The tools keep no independent deduplication dictionary.

Fork checkpoints contain native Pydantic AI message snapshots in transcript metadata.
The browser receives event IDs and restore metadata, never the hidden checkpoint
payload. A child version keeps its source request identity and conversation while
using a new execution ID.

| Coordinate | Behavior |
| --- | --- |
| Before input | Screens the original input under the new ingress policy. |
| Before tool | Rechecks the gate and journals a copied immutable result, or executes an absent effect. |
| After result | Keeps the recorded observation and continues remaining sibling calls or the next model request. |
| Rejournal | Skips the gate only for copied completed effects and applies the new result policy. |

Semora retains raw effect records separately from model-visible journal projections.
Removing a masking policy in a rejournal branch therefore restores the original
result without repeating the effect. Earlier source messages and frames remain in
the append-only transcript. Completed child versions may be branched again.

For a partial batch, the checkpoint retains answered call/result pairs in the history
prefix and places only unanswered native calls in the final response that Pydantic AI
resumes. Tool order and tool names are preserved. `/api/units` supplies the console's
rerun table; per-event metadata names the exact next seam.

## Validation

```sh
uv run pytest
node --test tests/test_stream.mjs
```

Deterministic tests use Pydantic AI `FunctionModel`, native messages, real Semora
dispatch, and memory stores. They cover payment identity, policy composition,
approval, recovery, uncertainty, native forks, retained frames, and the existing UI.
The optional `tests/test_durable.py` needs both a PostgreSQL database and an
OpenRouter key; `scripts/acceptance.py` exercises a running server with a live model.
No live-model run is part of the deterministic test suite.
