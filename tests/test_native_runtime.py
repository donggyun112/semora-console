"""Console scenarios against real Semora and native Pydantic AI FunctionModel."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from console import server
from console.fork_demo import read_event_checkpoint


def install(monkeypatch, calls, *, final="done"):
    def model(messages, info):
        results = [
            p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
        ]
        if results:
            return ModelResponse([TextPart(final)])
        return ModelResponse(
            [ToolCallPart(name, args, call_id) for name, args, call_id in calls]
        )

    monkeypatch.setattr(server, "openrouter_model", lambda: FunctionModel(model))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    server._sessions.clear()


def frames(response):
    assert response.status_code == 200, response.text
    rows = [json.loads(line) for line in response.text.splitlines()]
    assert not any(row["kind"] == "error" for row in rows), [
        row.get("message") for row in rows if row["kind"] == "error"
    ]
    return rows


def start(client, scenario="charge", units=(), **kwargs):
    return frames(
        client.post(
            "/api/run", json={"scenario_id": scenario, "units": list(units), **kwargs}
        )
    )


def get(rows, kind):
    return next(row for row in rows if row["kind"] == kind)


def results(rows):
    return [
        row["event"]
        for row in rows
        if row["kind"] == "agent" and row["event"]["type"] == "tool_result"
    ]


CHARGE = [("charge_card", {"customer_id": "c-001", "amount": "49"}, "charge-1")]
READ = [("read_customer", {"customer_id": "c-001"}, "read-1")]


def test_native_run_emits_result_and_keeps_frames(monkeypatch):
    install(monkeypatch, CHARGE)
    with TestClient(server.app) as client:
        rows = start(client)
        assert get(rows, "outcome")["outcome"]["stop_reason"] == "completed"
        assert results(rows)[0]["result"]["execution_count"] == 1
        stored = client.get(f"/api/runs/{get(rows, 'meta')['run_id']}/frames").json()
        assert stored["frames"] == rows


def test_new_request_in_same_session_charges_again(monkeypatch):
    # Provider call ids can repeat across requests; request identity still separates effects.
    install(monkeypatch, CHARGE)
    with TestClient(server.app) as client:
        first = start(client, operator="alice")
        second = start(client, operator="alice")
        assert results(first)[0]["result"]["idempotency"]["replayed"] is False
        assert results(second)[0]["result"]["idempotency"]["replayed"] is False


def test_approval_survives_process_cache_loss(monkeypatch):
    install(monkeypatch, CHARGE)
    with TestClient(server.app) as client:
        initial = start(client, units=["approval"])
        run_id = get(initial, "meta")["run_id"]
        pending = get(initial, "suspended")["pending_id"]
        server._sessions.clear()
        resumed = frames(
            client.post(
                "/api/resume",
                json={"run_id": run_id, "pending_id": pending, "approved": True},
            )
        )
        assert get(resumed, "outcome")["outcome"]["stop_reason"] == "completed"
        assert len(results(resumed)) == 1
        assert results(resumed)[0]["result"]["execution_count"] == 1
        assert (
            client.get(f"/api/runs/{run_id}/frames").json()["frames"]
            == initial + resumed
        )


def test_approval_may_replace_the_arguments(monkeypatch):
    install(monkeypatch, CHARGE)
    with TestClient(server.app) as client:
        initial = start(client, units=["approval"])
        resumed = frames(
            client.post(
                "/api/resume",
                json={
                    "run_id": get(initial, "meta")["run_id"],
                    "pending_id": get(initial, "suspended")["pending_id"],
                    "approved": True,
                    "args": {"customer_id": "c-001", "amount": "5"},
                },
            )
        )
        gate = next(row for row in resumed if row.get("type") == "pre_tool_use")
        assert gate["payload"]["input"] == {"customer_id": "c-001", "amount": "5"}
        assert '"amount": "5"' in results(resumed)[0]["result"]["text"], "and they ran"
        assert results(resumed)[0]["result"]["execution_count"] == 1


def test_approval_revalidates_current_policy(monkeypatch):
    install(
        monkeypatch,
        [
            (
                "send_email",
                {"to": "external@example.com", "body": "123-45-6789"},
                "mail-1",
            )
        ],
    )
    with TestClient(server.app) as client:
        rows = start(client, units=["approval"])
        resumed = frames(
            client.post(
                "/api/resume",
                json={
                    "run_id": get(rows, "meta")["run_id"],
                    "pending_id": get(rows, "suspended")["pending_id"],
                    "approved": True,
                    "units": ["approval", "dlp_block"],
                },
            )
        )
        denied = results(resumed)[0]
        assert denied["executed"] is False
        assert denied["result"]["revalidated"] is True


def test_human_denial_does_not_execute(monkeypatch):
    install(monkeypatch, CHARGE)
    with TestClient(server.app) as client:
        rows = start(client, units=["approval"])
        resumed = frames(
            client.post(
                "/api/resume",
                json={
                    "run_id": get(rows, "meta")["run_id"],
                    "pending_id": get(rows, "suspended")["pending_id"],
                    "approved": False,
                },
            )
        )
        assert get(resumed, "outcome")["outcome"]["stop_reason"] == "completed"
        assert not any(result.get("executed") for result in results(resumed))
        assert len(results(resumed)) == 1


@pytest.mark.parametrize("scenario,units", [("crash", []), ("crash", ["approval"])])
def test_crash_recovers_recorded_round(monkeypatch, scenario, units):
    install(monkeypatch, CHARGE)
    with TestClient(server.app) as client:
        rows = start(client, scenario, units)
        get(rows, "recoverable")
        recovered = frames(
            client.post("/api/recover", json={"run_id": get(rows, "meta")["run_id"]})
        )
        get(recovered, "suspended" if units else "outcome")
        if not units:
            assert results(recovered)[0]["result"]["execution_count"] == 1


def test_unknown_effect_is_never_retried(monkeypatch):
    install(monkeypatch, CHARGE)
    with TestClient(server.app) as client:
        rows = start(client, "unknown_effect")
        get(rows, "recoverable")
        recovered = frames(
            client.post("/api/recover", json={"run_id": get(rows, "meta")["run_id"]})
        )
        get(recovered, "indeterminate")
        assert not results(recovered)


def test_parallel_batch_approval_waits_for_every_answer(monkeypatch):
    install(
        monkeypatch,
        [
            ("charge_card", {"customer_id": f"c-{i}", "amount": "10"}, f"charge-{i}")
            for i in range(3)
        ],
    )
    with TestClient(server.app) as client:
        rows = start(client, "parallel", ["approval"])
        run_id = get(rows, "meta")["run_id"]
        for index in range(3):
            pending = get(rows, "suspended")["pending_id"]
            rows = frames(
                client.post(
                    "/api/resume",
                    json={"run_id": run_id, "pending_id": pending, "approved": True},
                )
            )
            if index < 2:
                assert not results(rows)
        assert len(results(rows)) == 3


def test_tool_fork_rejournals_raw_result_without_reexecution(monkeypatch):
    install(monkeypatch, READ)
    with TestClient(server.app) as client:
        rows = start(client, "fork_masking", ["pii_mask"])
        assert "jane@doe.io" not in results(rows)[0]["result"]["text"]
        gate = next(row for row in rows if row.get("type") == "pre_tool_use")
        forked = frames(
            client.post(
                "/api/fork",
                json={
                    "run_id": get(rows, "meta")["run_id"],
                    "event_id": gate["event_id"],
                    "edge": "before",
                    "units": [],
                    "rejournal": True,
                },
            )
        )
        assert get(forked, "outcome")["outcome"]["stop_reason"] == "completed"
        assert "jane@doe.io" in results(forked)[0]["result"]["text"]
        assert results(forked)[0]["result"]["execution_count"] == 1


def test_fork_changed_gate_revalidates_recorded_effect(monkeypatch):
    install(monkeypatch, CHARGE)
    with TestClient(server.app) as client:
        rows = start(client)
        gate = next(row for row in rows if row.get("type") == "pre_tool_use")
        forked = frames(
            client.post(
                "/api/fork",
                json={
                    "run_id": get(rows, "meta")["run_id"],
                    "event_id": gate["event_id"],
                    "edge": "before",
                    "units": ["approval"],
                },
            )
        )
        get(forked, "suspended")


def test_after_result_fork_skips_effect(monkeypatch):
    install(monkeypatch, READ)
    with TestClient(server.app) as client:
        rows = start(client, "fork_masking", ["pii_mask"])
        point = next(row for row in rows if row.get("boundary") == "result")
        forked = frames(
            client.post(
                "/api/fork",
                json={
                    "run_id": get(rows, "meta")["run_id"],
                    "event_id": point["event_id"],
                    "edge": "after",
                    "units": [],
                },
            )
        )
        get(forked, "outcome")
        assert not results(forked)


def test_input_fork_reapplies_ingress(monkeypatch):
    install(monkeypatch, READ)
    with TestClient(server.app) as client:
        rows = start(client, "fork_masking")
        point = next(row for row in rows if row.get("boundary") == "input")
        forked = frames(
            client.post(
                "/api/fork",
                json={
                    "run_id": get(rows, "meta")["run_id"],
                    "event_id": point["event_id"],
                    "edge": "before",
                    "units": ["pii_mask"],
                },
            )
        )
        get(forked, "outcome")
        assert "jane@doe.io" not in results(forked)[0]["result"]["text"]


def test_parallel_crash_finishes_each_call_once(monkeypatch):
    install(
        monkeypatch,
        [
            ("charge_card", {"customer_id": f"c-{i}", "amount": "10"}, f"charge-{i}")
            for i in range(3)
        ],
    )
    with TestClient(server.app) as client:
        rows = start(client, "parallel_crash")
        get(rows, "recoverable")
        recovered = frames(
            client.post("/api/recover", json={"run_id": get(rows, "meta")["run_id"]})
        )
        assert len(results(recovered)) == 3
        assert all(
            event["result"]["execution_count"] == 1 for event in results(recovered)
        )


def test_parallel_result_coordinate_finishes_remaining_recorded_calls(monkeypatch):
    install(
        monkeypatch,
        [
            ("charge_card", {"customer_id": f"c-{i}", "amount": "10"}, f"charge-{i}")
            for i in range(3)
        ],
    )
    with TestClient(server.app) as client:
        rows = start(client, "parallel")
        point = next(row for row in rows if row.get("boundary") == "result")
        forked = frames(
            client.post(
                "/api/fork",
                json={
                    "run_id": get(rows, "meta")["run_id"],
                    "event_id": point["event_id"],
                    "edge": "after",
                    "units": [],
                },
            )
        )
        assert {event["id"] for event in results(forked)} == {"charge-1", "charge-2"}
        assert all(event["result"]["execution_count"] == 1 for event in results(forked))


def test_completed_child_can_fork_again_with_same_payment_intent(monkeypatch):
    install(monkeypatch, CHARGE)
    with TestClient(server.app) as client:
        rows = start(client)
        source = get(rows, "meta")["run_id"]
        origin = server._sessions[source]["origin_id"]
        for _ in range(2):
            point = next(row for row in rows if row.get("type") == "pre_tool_use")
            rows = frames(
                client.post(
                    "/api/fork",
                    json={
                        "run_id": get(rows, "meta")["run_id"],
                        "event_id": point["event_id"],
                        "edge": "before",
                        "units": [],
                    },
                )
            )
            assert server._sessions[get(rows, "meta")["run_id"]]["origin_id"] == origin
            assert results(rows)[0]["result"]["execution_count"] == 1


def test_mixed_batch_fork_preserves_tool_return_names(monkeypatch):
    install(monkeypatch, READ + CHARGE)
    with TestClient(server.app) as client:
        rows = start(client, "parallel")
        gate = next(
            row
            for row in rows
            if row.get("type") == "pre_tool_use"
            and row["payload"]["name"] == "charge_card"
        )
        forked = frames(
            client.post(
                "/api/fork",
                json={
                    "run_id": get(rows, "meta")["run_id"],
                    "event_id": gate["event_id"],
                    "edge": "before",
                    "units": [],
                },
            )
        )
        assert [event["name"] for event in results(forked)] == ["charge_card"]
        child = server._sessions[get(forked, "meta")["run_id"]]
        checkpoint = client.portal.call(
            read_event_checkpoint,
            server._transcript,
            child["conversation_id"],
            gate["event_id"],
        )
        pairs = [
            (part["tool_name"], part["tool_call_id"])
            for msg in checkpoint.before.history
            for part in msg["parts"]
            if part["part_kind"] == "tool-return"
        ]
        assert pairs == [("read_customer", "read-1")]


def test_denied_gate_can_be_forked_under_new_policy(monkeypatch):
    install(
        monkeypatch,
        [
            (
                "send_email",
                {"to": "external@example.com", "body": "123-45-6789"},
                "mail-1",
            )
        ],
    )
    with TestClient(server.app) as client:
        rows = start(client, units=["dlp_block"])
        gate = next(row for row in rows if row.get("type") == "pre_tool_use")
        forked = frames(
            client.post(
                "/api/fork",
                json={
                    "run_id": get(rows, "meta")["run_id"],
                    "event_id": gate["event_id"],
                    "edge": "before",
                    "units": [],
                },
            )
        )
        assert results(forked)[0]["executed"] is True


def test_steer_while_parked_preserves_the_approval(monkeypatch):
    install(monkeypatch, CHARGE)
    with TestClient(server.app) as client:
        rows = start(client, units=["approval"])
        run_id = get(rows, "meta")["run_id"]
        response = client.post(
            "/api/steer", json={"run_id": run_id, "text": "기록도 남겨주세요"}
        )
        assert response.json()["admits"] == "on_resume"
        resumed = frames(
            client.post(
                "/api/resume",
                json={
                    "run_id": run_id,
                    "pending_id": get(rows, "suspended")["pending_id"],
                    "approved": True,
                },
            )
        )
        admitted = [row for row in resumed if row["kind"] == "steer"]
        assert any(
            row["source"] == "user_steer" and row["phase"] == "admitted"
            for row in admitted
        )


def test_finish_policy_steer_is_admitted_once_as_control(monkeypatch):
    from pydantic_ai.messages import UserPromptPart

    from console.units import LOG_HINT

    def model(messages, info):
        returned = [
            p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
        ]
        prompted = any(
            isinstance(p, UserPromptPart) and p.content == LOG_HINT
            for m in messages
            for p in m.parts
        )
        if prompted and not returned:
            return ModelResponse(
                [
                    ToolCallPart(
                        "remember_note", {"key": "done", "value": "yes"}, "note-1"
                    )
                ]
            )
        return ModelResponse([TextPart("done")])

    install(monkeypatch, [])
    monkeypatch.setattr(server, "openrouter_model", lambda: FunctionModel(model))
    with TestClient(server.app) as client:
        rows = start(client, units=["log_gate"])
        assert get(rows, "outcome")["outcome"]["stop_reason"] == "completed"
        assert (
            len(
                [
                    row
                    for row in rows
                    if row["kind"] == "steer" and row["source"] == "control"
                ]
            )
            == 1
        )
        assert get(rows, "policy_summary")["units"][0]["count"] == 1


def test_input_fork_can_reuse_call_id_for_a_different_tool(monkeypatch):
    responses = iter(
        [
            ModelResponse(
                [ToolCallPart("read_customer", {"customer_id": "c-001"}, "same-id")]
            ),
            ModelResponse([TextPart("source done")]),
            ModelResponse(
                [
                    ToolCallPart(
                        "charge_card",
                        {"customer_id": "c-001", "amount": "49"},
                        "same-id",
                    )
                ]
            ),
            ModelResponse([TextPart("child done")]),
        ]
    )
    install(monkeypatch, [])
    model = FunctionModel(lambda messages, info: next(responses))
    monkeypatch.setattr(server, "openrouter_model", lambda: model)
    with TestClient(server.app) as client:
        rows = start(client)
        point = next(row for row in rows if row.get("boundary") == "input")
        child = frames(
            client.post(
                "/api/fork",
                json={
                    "run_id": get(rows, "meta")["run_id"],
                    "event_id": point["event_id"],
                    "edge": "before",
                    "units": [],
                },
            )
        )
        assert results(child)[0]["name"] == "charge_card"
        assert json.loads(results(child)[0]["result"]["text"])["status"] == "charged"


@pytest.mark.asyncio
async def test_abort_cancels_native_model_and_discards_queued_steer(monkeypatch):
    entered = asyncio.Event()

    async def model(messages, info):
        entered.set()
        await asyncio.Event().wait()
        return ModelResponse([TextPart("unreachable")])

    install(monkeypatch, [])
    monkeypatch.setattr(server, "openrouter_model", lambda: FunctionModel(model))
    async with server.lifespan(server.app):
        response = await server.run(server.RunRequest(scenario_id="charge"))
        rows = []

        async def consume():
            async for row in response.body_iterator:
                rows.append(json.loads(row))

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 2)
        run_id = next(iter(server._sessions))
        await server.steer(server.SteerRequest(run_id=run_id, text="queued note"))
        result = await server.abort(server.AbortRequest(run_id=run_id))
        assert result["dropped"] == 1
        await asyncio.wait_for(consumer, 2)
        assert get(rows, "outcome")["outcome"]["stop_reason"] == "aborted"


def test_console_turn_guard_stops_a_repeated_finish_veto(monkeypatch):
    install(monkeypatch, [])
    monkeypatch.setattr(
        server,
        "openrouter_model",
        lambda: FunctionModel(lambda messages, info: ModelResponse([TextPart("done")])),
    )
    with TestClient(server.app) as client:
        rows = start(client, units=["log_gate"])
        assert get(rows, "outcome")["outcome"]["stop_reason"] == "policy"
