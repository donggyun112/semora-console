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
Each run stores one `Agent` definition that owns its model, tools, and system prompt.
The host sends `Prompt`, `Answer`, or `Recover` to `AgentRuntime.dispatch`; the runtime's
ordered transition table selects start, steering, resume, or journal replay.

## The units

| Unit | Hook → Composer | Verdict | What it does |
| --- | --- | --- | --- |
| `input_mask` | `on_inputs` → Ingress | **Rewrite** | Masks an SSN-shaped user input before it enters model context while keeping its ledger `origin_id`. |
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

`fork_masking` runs `ssn is 123-45` through `input_mask`, so the source transcript shows
`ssn is ***` while the source ledger keeps the submitted original. **원문으로 분기 실행**
calls `fork_run` without that masker: the conversation head moves from the shared prefix,
and the original becomes durable in the fork ledger, transcript, and `CONTEXT_INJECTED` event.

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

## 효과 경계

랭그래프는 노드 단위로 체크포인트를 찍는다. 그런데 ReAct 에이전트에서 툴 콜은 세 건이든
열 건이든 노드 하나에 들어간다. 두 번째에서 프로세스가 죽었다고 하자. 재개하면 그 노드를
처음부터 다시 탄다. 첫 번째 청구가 또 나간다. 대화 기록은 멀쩡히 돌아오는데, 돈이 나갔다는
사실만 어디에도 없다.

interrupt도 마찬가지. 노드를 통째로 다시 타니 승인 앞에 있던 코드가 또 돌고, 과거로
되감으면 사람에게 같은 질문을 다시 묻는다.

nexora는 툴을 부르기 전에 "부르겠다"를 쓰고, 부른 뒤에 "이렇게 됐다"를 쓴다. 키는 툴 콜
id다. 그래서 스텝 상태가 둘이 아니라 셋이 된다.

| | |
|---|---|
| `Indeterminate` | 시작만 기록되고 결과가 없는 스텝. 효과가 나갔는지 알 수 없으니 런타임도 답을 고르지 않는다. |
| `Fenced` | 지나간 토큰을 들고 온 쓰기. 뒤늦게 되살아난 워커가 그사이 진행된 런을 건드리지 못한다. |
| `Contended` | 다른 워커가 런을 잡고 있는 상태. 입력은 경쟁 대신 큐로 간다. |

셋 다 툴이 실패한 게 아니라 원장이 보내는 신호다. 툴에서 난 예외는 그 콜만 error로 마감하고
라운드는 계속 간다. 이 셋은 다르다. 툴 경계를 그대로 통과해야 하고, 위에서 한 번 더
except로 감싸는 순간 아래에서 일부러 흘려보낸 것만 골라 삼키게 된다.

재개는 게이트를 다시 태우는 게 아니다. park된 콜은 `on_resume`으로 들어간다. 사람이 준 답,
중단 당시의 정책 버전, 지금의 정책 버전이 같이 온다. `Continue`가 나와야 효과 경계를 넘는다.
그사이 규칙이 바뀌었다면 지난주에 받아둔 승인만으로는 안 된다.

순서도 보장 범위 안에 있다. 같은 파일에 멱등한 쓰기 두 개를 순서만 바꿔 넣어도 결과가 갈린다.
콜 전부가 동시 실행해도 안전하다고 선언되지 않는 한, 배치는 한 건씩 순서대로 돈다.

`parallel_crash`에 `approval`을 얹고 승인 대기 중에 워커를 죽여 보면 위 내용이 화면에서
그대로 돈다.

## Docker

The durable proof needs a ledger that outlives the worker, so the compose stack is the
honest way to run this. `.env` beside `compose.yaml` supplies `OPENROUTER_API_KEY`.

```bash
docker compose up --build      # → http://localhost:8850
```

`nexora` is resolved by path from the neighbouring checkout, so it arrives as a named
build context (`additional_contexts: nexora: ../nexora-python`) rather than by widening
the build context to the parent directory. The ledger lives in a named volume, which is
why `restart` and even `down` keep a parked run and `down -v` is the one that throws it
away.

Prove that a parked run survives the worker that parked it:

```bash
curl -sN localhost:8850/api/run -H 'content-type: application/json' \
     -d '{"scenario_id":"charge","units":["approval"]}' | grep suspended
docker compose restart console          # the process that took the approval is gone
curl -sN localhost:8850/api/resume -H 'content-type: application/json' \
     -d '{"run_id":"<run_id>","pending_id":"<pending_id>","approved":true}'
```

The charge runs once, in a process that never saw the suspension. `_sessions` is a cache
of a record the ledger holds, so a worker rebuilds what it needs from `run_id` alone.
Without `DATABASE_URL` the ledger is memory too, and the restart loses the run exactly
as the runtime does.

## Durable exactly-once

`approval` suspends a call and persists both the continuation and conversation transcript;
resuming runs the effect **exactly once** (`exec ×1` in the UI). Run without
`DATABASE_URL` and both stores are memory, which is enough to watch the gate work but not
to watch it survive anything — [Docker](#docker) has the restart proof.

**청구 중 장애** and **동시 청구 중 장애** arm a worker crash after the first tool
`finish_effect`. In the parallel case the committed result is replayed and the absent
siblings execute during **복원**: `POST /api/recover` dispatches `Recover()`, and the
runtime selects journal replay from durable state. Process restart still needs `DATABASE_URL`.

## Layout

| File | Role |
| --- | --- |
| `units.py` | The substance — units + `compose_controls()` + self-check. |
| `scenarios.py` | 10 locked scenarios (no free-text → no LLM-proxy abuse). |
| `dormancy.py` | Why a toggled-on unit stayed dormant in a scenario. |
| `tools.py` | Demo effects (`read_customer` returns PII, `charge_card`, `send_email`, `remember_note`). |
| `store.py` | In-memory ledger locally, Postgres when `DATABASE_URL` is set. |
| `Dockerfile` · `compose.yaml` | The stack with a Postgres ledger, where a parked run outlives its worker. |
| `server.py` | FastAPI: `/api/run`, `/api/fork`, `/api/resume`, `/api/recover`, `/api/abort`, `/api/steer`, `/api/units`, static UI. |
