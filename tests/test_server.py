import asyncio
import json
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from nexora import Agent
from nexora.contracts.events import EventType

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


def test_static_shell_is_run_inspector_with_contextual_drawers():
    with TestClient(app) as c:
        html = c.get("/").text

    class ShellParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids: list[str] = []
            self.elements: dict[str, dict[str, str | None]] = {}

        def handle_starttag(self, _tag, attrs):
            attributes = dict(attrs)
            if element_id := attributes.get("id"):
                self.ids.append(element_id)
                self.elements[element_id] = attributes

    shell = ShellParser()
    shell.feed(html)

    assert len(shell.ids) == len(set(shell.ids))
    required = {
        "scenario-trigger", "scenario-menu", "launch-title", "launch-prompt",
        "launch-policies", "run", "policy-open", "launch-policy-open",
        "run-shell", "run-title", "run-status", "run-policies", "abort",
        "chat-panel", "chat-thread", "event-panel", "event-count",
        "outcome-strip", "rerun-plain", "rerun-same", "retry-run",
        "return-draft", "trace", "details-drawer", "details-close",
        "details-copy", "details-title", "details-body", "steer-form",
        "steer-text", "policy-drawer", "policy-close", "scenarios", "units",
        "compose-summary", "approval", "approve", "deny", "recovery",
        "recover", "run-error", "boot-error", "boot-retry",
    }
    assert required <= set(shell.ids)
    assert {"mode-demo", "mode-composer", "demo-panel", "composer-panel"}.isdisjoint(
        shell.ids
    )
    assert "hidden" in shell.elements["run-shell"]["class"].split()
    assert "hidden" in shell.elements["details-drawer"]["class"].split()
    assert "hidden" in shell.elements["policy-drawer"]["class"].split()
    assert shell.elements["trace"]["role"] == "log"
    assert shell.ids.index("chat-panel") < shell.ids.index("trace")
    assert shell.elements["run-error"]["aria-live"] == "assertive"


def test_stylesheet_has_run_inspector_drawers_and_accessibility_contracts():
    with TestClient(app) as c:
        css = c.get("/styles.css").text

    for selector in [
        ".launch",
        ".run-shell",
        ".chat-panel",
        ".chat-message",
        ".event-panel",
        ".trace-row",
        ".details-drawer",
        ".policy-drawer",
        ".outcome-strip",
        ":focus-visible",
        "@media (max-width: 820px)",
        "@media (prefers-reduced-motion: reduce)",
    ]:
        assert selector in css
    assert ".workspace.mode-demo" not in css
    assert ".composer-panel" not in css


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


@pytest.mark.asyncio
async def test_denial_projects_policy_before_permission_lifecycle_once():
    """The deciding policy precedes its permission consequence and is not duplicated."""
    run_id = "run-denial-order"
    server._sessions[run_id] = {"units": ["dlp_block"], "aborted": False, "scenario_id": "leak"}

    async def attempt(runtime, on_event):
        call = {
            "type": "tool_call",
            "id": "call-send",
            "name": "send_email",
            "input": {"body": "ssn"},
        }
        await on_event(call)
        await runtime.events.publish(
            EventType.PRE_TOOL_USE,
            turn=1,
            call_id="call-send",
            name="send_email",
        )
        denied = {
            "type": "error",
            "unit": "dlp_block",
            "message": "거부",
        }
        await runtime.events.publish(
            EventType.PERMISSION_DENIED,
            turn=1,
            call_id="call-send",
            name="send_email",
            reason=denied,
            source="pre_tool_use",
        )
        await on_event({**call, "type": "tool_result", "executed": False, "result": denied})
        return {"stop_reason": "completed"}

    frames: list[dict] = []
    try:
        async for chunk in _stream(run_id, attempt, selected=["dlp_block"], scenario_id="leak"):
            frames.append(json.loads(chunk))
    finally:
        server._sessions.pop(run_id, None)

    visible = [
        (frame["kind"], frame.get("unit") or frame.get("type"))
        for frame in frames
        if frame["kind"] in {"unit", "lifecycle"}
        and (frame.get("unit") == "dlp_block" or frame.get("type") == "permission_denied")
    ]
    assert visible == [("unit", "dlp_block"), ("lifecycle", "permission_denied")]
