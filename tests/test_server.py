import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from nexora import Agent

from console import server
from console.server import _crash_point, _is_aborted, _project_event, _stream, app


def test_suspend_does_not_mark_the_call_blocked():
    pending: dict = {}
    _project_event({"type": "tool_call", "id": "c1", "name": "charge_card", "input": {}}, pending)
    frames = _project_event(
        {
            "type": "tool_result",
            "id": "c1",
            "name": "charge_card",
            "executed": False,
            "result": {"type": "suspend", "unit": "approval", "reason": "승인 필요"},
        },
        pending,
    )
    assert [f["kind"] for f in frames] == ["unit"]
    assert frames[0]["verdict"] == "suspend"


def test_refused_call_emits_request_then_gate():
    pending: dict = {}
    req = _project_event({"type": "tool_call", "id": "c1", "name": "send_email", "input": {"body": "ssn"}}, pending)
    assert req[0]["event"]["pending"] is True
    frames = _project_event(
        {
            "type": "tool_result",
            "id": "c1",
            "name": "send_email",
            "executed": False,
            "result": {"type": "error", "unit": "dlp_block", "message": "거부"},
        },
        pending,
    )
    assert [f["kind"] for f in frames] == ["unit", "agent"]
    assert frames[0]["unit"] == "dlp_block" and frames[0]["verdict"] == "deny"
    assert frames[1]["event"]["blocked"] is True
    assert frames[1]["event"]["name"] == "send_email"


def test_executed_call_emits_request_then_result():
    pending: dict = {}
    req = _project_event({"type": "tool_call", "id": "c1", "name": "read_customer", "input": {}}, pending)
    assert req[0]["event"]["name"] == "read_customer"
    frames = _project_event(
        {
            "type": "tool_result",
            "id": "c1",
            "executed": True,
            "result": {"type": "text", "text": "ok", "redacted_by": "pii_mask", "control_note": "익명화"},
        },
        pending,
    )
    assert [f["kind"] for f in frames] == ["unit", "agent"]
    assert frames[0]["verdict"] == "rewrite"


def test_scenarios_endpoint():
    with TestClient(app) as c:
        r = c.get("/api/scenarios")
        assert r.status_code == 200
        assert [s["id"] for s in r.json()] == [
            "note", "customer", "leak", "inject", "charge", "crash", "batch", "parallel",
            "parallel_crash",
        ]


def test_run_uses_agent_definition(monkeypatch):
    """The server binds model, tools, and system prompt in one Agent definition."""
    model = object()
    captured: dict = {}

    monkeypatch.setattr(server, "openrouter_model", lambda: model)

    async def fake_run(self, run_id, agent, prompt, **kwargs):
        captured.update(run_id=run_id, agent=agent, prompt=prompt, kwargs=kwargs)
        return {"stop_reason": "completed"}

    monkeypatch.setattr(server.AgentRuntime, "run", fake_run)

    with TestClient(app) as c:
        response = c.post("/api/run", json={"scenario_id": "note", "units": []})

    frames = [json.loads(line) for line in response.text.splitlines()]
    run_id = next(frame["run_id"] for frame in frames if frame["kind"] == "meta")
    try:
        assert [frame for frame in frames if frame["kind"] == "error"] == []
        agent = captured["agent"]
        assert isinstance(agent, Agent)
        assert agent.name == "control-plane-console"
        assert agent.description
        assert agent.model is model
        assert isinstance(agent.tools, server.DemoTools)
        assert agent.system_prompt == server.SYSTEM_PROMPT
        assert captured["prompt"] == next(
            scenario["prompt"] for scenario in server.SCENARIOS if scenario["id"] == "note"
        )
        assert "system_prompt" not in captured["kwargs"]
        assert server._sessions[run_id]["agent"] is agent
    finally:
        server._sessions.pop(run_id, None)


def test_resume_reuses_session_agent(monkeypatch):
    """Continuation uses the original Agent definition instead of rebuilding its model."""
    model = object()
    agent = Agent(
        "control-plane-console",
        "Runs locked operator control-plane scenarios.",
        model,
        server.DemoTools(),
        server.SYSTEM_PROMPT,
    )
    captured: dict = {}

    def unexpected_model():
        raise AssertionError("resume must not create a new model")

    async def fake_resume(self, run_id, pending_id, answer, resumed_model, tools, **kwargs):
        captured.update(
            run_id=run_id,
            pending_id=pending_id,
            answer=answer,
            model=resumed_model,
            tools=tools,
            kwargs=kwargs,
        )
        return {"stop_reason": "completed"}

    monkeypatch.setattr(server, "openrouter_model", unexpected_model)
    monkeypatch.setattr(server.AgentRuntime, "resume", fake_resume)
    server._sessions["run-agent-resume"] = {
        "units": [],
        "agent": agent,
        "scenario_id": "charge",
        "aborted": False,
        "crash": False,
    }
    try:
        with TestClient(app) as c:
            response = c.post(
                "/api/resume",
                json={"run_id": "run-agent-resume", "pending_id": "pending-1", "approved": True},
            )
        frames = [json.loads(line) for line in response.text.splitlines()]
        assert [frame for frame in frames if frame["kind"] == "error"] == []
        assert captured["model"] is agent.model
        assert captured["tools"] is agent.tools
        assert captured["kwargs"]["system_prompt"] == agent.system_prompt
    finally:
        server._sessions.pop("run-agent-resume", None)


def test_recover_reuses_session_agent(monkeypatch):
    """Crash recovery keeps the original Agent definition and tool state."""
    model = object()
    agent = Agent(
        "control-plane-console",
        "Runs locked operator control-plane scenarios.",
        model,
        server.DemoTools(),
        server.SYSTEM_PROMPT,
    )
    history = [object()]
    captured: dict = {}

    def unexpected_model():
        raise AssertionError("recover must not create a new model")

    async def fake_recover(self, run_id, recovered_history, recovered_model, tools, **kwargs):
        captured.update(
            run_id=run_id,
            history=recovered_history,
            model=recovered_model,
            tools=tools,
            kwargs=kwargs,
        )
        return {"stop_reason": "completed"}

    monkeypatch.setattr(server, "openrouter_model", unexpected_model)
    monkeypatch.setattr(server.AgentRuntime, "recover", fake_recover)
    server._sessions["run-agent-recover"] = {
        "units": [],
        "agent": agent,
        "scenario_id": "crash",
        "aborted": False,
        "history": history,
    }
    try:
        with TestClient(app) as c:
            response = c.post("/api/recover", json={"run_id": "run-agent-recover"})
        frames = [json.loads(line) for line in response.text.splitlines()]
        assert [frame for frame in frames if frame["kind"] == "error"] == []
        assert captured["history"] is history
        assert captured["model"] is agent.model
        assert captured["tools"] is agent.tools
        assert captured["kwargs"]["system_prompt"] == agent.system_prompt
        assert captured["kwargs"]["retry_running"] is False
    finally:
        server._sessions.pop("run-agent-recover", None)


def test_units_endpoint_shape():
    with TestClient(app) as c:
        r = c.get("/api/units")
        assert r.status_code == 200
        body = r.json()
        assert body["model"].startswith("deepseek/")
        names = {u["name"] for u in body["units"]}
        assert names == {
            "approval", "dlp_block", "rate_cap", "pii_mask",
            "context_firewall", "injection_guard", "log_gate",
        }
        points = {u["point"] for u in body["units"]}
        assert {"pre_tool_use", "after_tool_call", "before_finish"} <= points
        assert "on_inputs" not in points
        assert "before_model" not in points


def test_steer_unknown_run_is_404():
    with TestClient(app) as c:
        r = c.post("/api/steer", json={"run_id": "run-missing", "text": "기록하라"})
        assert r.status_code == 404


def test_steer_without_inflight_is_409():
    """A complete agent has one queue, but nothing is admitted once the attempt has ended."""
    server._sessions["run-steer"] = {"units": [], "aborted": False, "scenario_id": "note"}
    try:
        with TestClient(app) as c:
            r = c.post("/api/steer", json={"run_id": "run-steer", "text": "기록하라"})
        assert r.status_code == 409
    finally:
        server._sessions.pop("run-steer", None)


def test_crash_with_approval_is_the_pre_park_seam():
    """crash + approval dies at pre_tool_use; crash alone still dies after commit."""
    assert _crash_point("crash", ["approval"]) == "gate"
    assert _crash_point("crash", ["approval", "rate_cap"]) == "gate"
    assert _crash_point("crash", []) == "commit"
    assert _crash_point("crash", ["dlp_block"]) == "commit"
    assert _crash_point("charge", ["approval"]) is None
    assert _crash_point("parallel", ["approval"]) is None
    assert _crash_point("parallel_crash", ["approval"]) == "gate"
    assert _crash_point("parallel_crash", []) == "commit"


def test_recover_without_crash_is_409():
    server._sessions["run-ok"] = {"units": [], "aborted": False, "scenario_id": "note", "history": None}
    try:
        with TestClient(app) as c:
            r = c.post("/api/recover", json={"run_id": "run-ok"})
        assert r.status_code == 409
    finally:
        server._sessions.pop("run-ok", None)


def test_abort_unknown_run_is_404():
    with TestClient(app) as c:
        r = c.post("/api/abort", json={"run_id": "run-missing"})
        assert r.status_code == 404


def test_abort_flags_existing_session():
    """POST /api/abort trips the loop's aborted() flag for that run."""
    server._sessions["run-test"] = {"units": [], "aborted": False, "scenario_id": "note"}
    try:
        with TestClient(app) as c:
            r = c.post("/api/abort", json={"run_id": "run-test"})
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert _is_aborted("run-test") is True
    finally:
        server._sessions.pop("run-test", None)


@pytest.mark.asyncio
async def test_abort_cancels_in_flight_stream():
    """An in-flight attempt ends as aborted once the operator cancels it."""
    run_id = "run-abort-live"
    server._sessions[run_id] = {"units": [], "aborted": False, "scenario_id": "note"}
    started = asyncio.Event()

    async def attempt(_runtime, _on_event):
        started.set()
        await asyncio.sleep(30)
        return {"stop_reason": "completed"}

    frames: list[dict] = []

    async def consume() -> None:
        async for chunk in _stream(run_id, attempt, selected=[], scenario_id="note"):
            frames.append(json.loads(chunk))

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        server._sessions[run_id]["aborted"] = True
        task = server._sessions[run_id]["task"]
        task.cancel()
        await asyncio.wait_for(consumer, timeout=2)
        outcome = next(f for f in frames if f["kind"] == "outcome")
        assert outcome["outcome"]["stop_reason"] == "aborted"
    finally:
        if not consumer.done():
            consumer.cancel()
        server._sessions.pop(run_id, None)


@pytest.mark.asyncio
async def test_new_run_opens_and_closes_a_session():
    """Each 실행 is a new host session — resume is the same one."""
    run_id = "run-session"
    server._sessions[run_id] = {"units": [], "aborted": False, "scenario_id": "note"}

    async def attempt(_runtime, _on_event):
        return {"stop_reason": "completed"}

    frames: list[dict] = []
    try:
        async for chunk in _stream(
            run_id, attempt, selected=[], scenario_id="note", open_session=True,
        ):
            frames.append(json.loads(chunk))
        life = [(f.get("type"), (f.get("payload") or {})) for f in frames if f["kind"] == "lifecycle"]
        assert any(t == "session_start" and p.get("source") == "console" for t, p in life)
        assert any(t == "session_end" and p.get("reason") == "completed" for t, p in life)
    finally:
        server._sessions.pop(run_id, None)
