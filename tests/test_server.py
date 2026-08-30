import asyncio
import json
from html.parser import HTMLParser
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from semora import Agent
from semora.contracts.events import EventType
from semora.dispatch import Answer, Prompt, Recover
from semora_fork import EventCheckpoint, ForkCoordinate, read_event_checkpoint

from console import server
from console.provider import model_name
from console.server import (
    _session_id,
    _crash_point,
    _is_aborted,
    _project_event,
    _register_frame,
    _stream,
    app,
)


class BoundFakeMessagesListChatModel(FakeMessagesListChatModel):
    def bind_tools(self, _tools, **_kwargs):
        return self


class StreamingFakeChatModel(GenericFakeChatModel):
    """A fake that streams chunks, which the journal needs to be replayable.

    ``FakeMessagesListChatModel`` yields whole messages, so a durable model step recorded
    from it cannot be replayed — a shape only the recovery path ever reads.
    """

    def bind_tools(self, _tools, **_kwargs):
        return self

    def _stream(self, messages, *args, **kwargs):
        reply = next(self.messages)
        if reply.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(content=reply.content, tool_calls=reply.tool_calls)
            )
            return
        yield ChatGenerationChunk(message=AIMessageChunk(content=str(reply.content)))


def _approved_charge_frames(monkeypatch):
    model = BoundFakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "charge-approval-1",
                        "name": "charge_card",
                        "args": {"customer_id": "c-001", "amount": 49},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="청구가 완료됐습니다."),
        ]
    )
    monkeypatch.setattr(server, "openrouter_model", lambda: model)
    with TestClient(app) as client:
        first = client.post(
            "/api/run",
            json={"scenario_id": "charge", "units": ["approval"]},
        )
        initial = [json.loads(line) for line in first.text.splitlines()]
        run_id = next(frame["run_id"] for frame in initial if frame["kind"] == "meta")
        suspended = next(frame for frame in initial if frame["kind"] == "suspended")
        resumed_response = client.post(
            "/api/resume",
            json={
                "run_id": run_id,
                "pending_id": suspended["pending_id"],
                "approved": True,
            },
        )
        resumed = [json.loads(line) for line in resumed_response.text.splitlines()]
    return run_id, initial, resumed


def test_resume_stabilizes_the_gate_and_only_the_gate(monkeypatch):
    """A boundary has one coordinate, and for a tool call it is the gate.

    The model asking for the tool restores to the same place, so marking it too gave the
    trace two rows claiming one branch point — and the console then had to decide which
    of them the operator meant.
    """
    run_id, initial, resumed = _approved_charge_frames(monkeypatch)
    try:
        request = next(
            frame
            for frame in initial
            if frame.get("kind") == "agent"
            and frame.get("event", {}).get("type") == "tool_call"
        )
        original_pre = next(
            frame
            for frame in initial
            if frame.get("type") == "pre_tool_use"
        )
        restored = {
            update["event_id"]: update["restore_edge"]
            for frame in resumed
            for update in frame.get("restore_updates", [])
        }

        assert restored[original_pre["event_id"]] == "before"
        assert request["event_id"] not in restored
        assert not request.get("forkable")
    finally:
        server._sessions.pop(run_id, None)


def test_resume_result_stabilizes_the_completed_tool_boundary(monkeypatch):
    run_id, _initial, resumed = _approved_charge_frames(monkeypatch)
    try:
        post = next(frame for frame in resumed if frame.get("type") == "post_tool_use")
        result = next(
            frame
            for frame in resumed
            if frame.get("kind") == "agent"
            and frame.get("event", {}).get("type") == "tool_result"
        )
        injected = next(
            frame
            for frame in resumed
            if frame.get("type") == "context_injected"
            and frame.get("payload", {}).get("kind") == "resume_result"
        )
        restored = {
            update["event_id"]: update["restore_edge"]
            for frame in resumed
            for update in frame.get("restore_updates", [])
        }

        assert restored[post["event_id"]] == "after"
        assert restored[result["event_id"]] == "after"
        assert injected["forkable"] is True
        assert injected["restore_edge"] == "after"
        assert result["event"]["result"]["idempotency"] == {
            "key": "charge:c-001",
            "replayed": False,
        }
    finally:
        server._sessions.pop(run_id, None)


def test_resume_result_itself_restores_from_the_tool_message_leaf(monkeypatch):
    run_id, _initial, resumed = _approved_charge_frames(monkeypatch)
    try:
        injected = next(
            frame
            for frame in resumed
            if frame.get("type") == "context_injected"
            and frame.get("payload", {}).get("kind") == "resume_result"
        )
        conversation_id = server._sessions[run_id]["conversation_id"]
        checkpoint = asyncio.run(
            read_event_checkpoint(
                server._transcript,
                conversation_id,
                injected["event_id"],
            )
        )

        assert checkpoint.after.origin_id is None
        assert checkpoint.after.leaf_uuid is not None
    finally:
        server._sessions.pop(run_id, None)


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


def test_suspend_frame_names_the_call_it_gates():
    """A parallel batch suspends once per call; without the id every SUSPEND row
    looks identical and the operator cannot tell which charge is being approved."""
    pending: dict = {}
    for cid, customer in (("c1", "c-001"), ("c2", "c-002")):
        _project_event(
            {"type": "tool_call", "id": cid, "name": "charge_card",
             "input": {"customer_id": customer, "amount": "10"}},
            pending,
        )
    frames = [
        _project_event(
            {"type": "tool_result", "id": cid, "name": "charge_card", "executed": False,
             "result": {"type": "suspend", "unit": "approval", "reason": "승인 필요"}},
            pending,
        )[0]
        for cid in ("c1", "c2")
    ]
    assert [f["call_id"] for f in frames] == ["c1", "c2"]
    assert [f["input"]["customer_id"] for f in frames] == ["c-001", "c-002"]
    assert {f["name"] for f in frames} == {"charge_card"}


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
    assert frames[1]["checkpoint_phase"] == "tool_result"
    assert frames[1]["call_id"] == "c1"


def test_resumed_refusal_keeps_the_original_call_identity_without_pending_state():
    frames = _project_event(
        {
            "type": "tool_result",
            "id": "charge-2",
            "name": "charge_card",
            "executed": False,
            "result": {
                "type": "error",
                "unit": "control",
                "message": "denied by the human",
            },
        },
        {},
    )

    blocked = frames[-1]
    assert blocked["event"]["id"] == "charge-2"
    assert blocked["event"]["name"] == "charge_card"
    assert blocked["event"]["blocked"] is True


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


def test_restore_updates_register_earlier_tool_events_as_forkable():
    session = {"event_ids": {"event-pre"}, "forkable_events": {}}

    _register_frame(
        session,
        {
            "event_id": "event-result-context",
            "forkable": True,
            "restore_edge": "after",
            "restore_updates": [
                {"event_id": "event-pre", "restore_edge": "before"},
                {"event_id": "event-post", "restore_edge": "after"},
            ],
        },
    )

    assert session["event_ids"] == {
        "event-pre",
        "event-post",
        "event-result-context",
    }
    assert session["forkable_events"] == {
        "event-pre": "before",
        "event-post": "after",
        "event-result-context": "after",
    }


def test_scenarios_endpoint():
    with TestClient(app) as c:
        r = c.get("/api/scenarios")
        assert r.status_code == 200
        assert [s["id"] for s in r.json()] == [
            "note", "customer", "leak", "inject", "charge", "crash", "unknown_effect", "batch", "parallel",
            "parallel_crash", "fork_masking",
        ]

def test_run_uses_agent_definition(monkeypatch):
    """The server dispatches a Prompt with one bound Agent definition."""
    model = object()
    captured: dict = {}

    monkeypatch.setattr(server, "openrouter_model", lambda: model)

    async def fake_dispatch(self, run_id, agent, command, **kwargs):
        captured.update(run_id=run_id, agent=agent, command=command, kwargs=kwargs)
        return {"stop_reason": "completed"}

    monkeypatch.setattr(server.AgentRuntime, "dispatch", fake_dispatch)

    with TestClient(app) as c:
        response = c.post("/api/run", json={"scenario_id": "note", "units": []})

    frames = [json.loads(line) for line in response.text.splitlines()]
    run_id = next(frame["run_id"] for frame in frames if frame["kind"] == "meta")
    try:
        assert UUID(run_id.removeprefix("run-")).version == 7
        assert [frame for frame in frames if frame["kind"] == "error"] == []
        agent = captured["agent"]
        assert isinstance(agent, Agent)
        assert agent.name == "control-plane-console"
        assert agent.description
        assert agent.model is model
        assert isinstance(agent.tools, server.DemoTools)
        assert agent.system_prompt == server.SYSTEM_PROMPT
        command = captured["command"]
        assert isinstance(command, Prompt)
        assert command.text == next(
            scenario["prompt"] for scenario in server.SCENARIOS if scenario["id"] == "note"
        )
        assert command.prompt_id
        assert "system_prompt" not in captured["kwargs"]
        assert server._sessions[run_id]["agent"] is agent
        assert captured["kwargs"]["conversation_id"] == server._sessions[run_id][
            "conversation_id"
        ]
        assert server._sessions[run_id]["default_fork_origin"] == command.prompt_id
        assert server._sessions[run_id]["origin_runs"] == {command.prompt_id: run_id}
        assert all("event_id" in frame for frame in frames)
        assert all(frame["run_id"] == run_id for frame in frames)
        assert any(
            frame.get("type") == "branch_snapshot"
            and frame.get("payload", {}).get("branch") == "source"
            for frame in frames
        )
    finally:
        server._sessions.pop(run_id, None)


def _charge_result_frames(text: str) -> list[dict]:
    frames = []
    for line in text.splitlines():
        frame = json.loads(line)
        event = frame.get("event") or {}
        if frame.get("kind") == "agent" and event.get("type") == "tool_result":
            frames.append(frame)
    return frames


def test_rerun_reuses_the_scenario_payment_ledger(monkeypatch):
    model = BoundFakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "charge-run-1",
                        "name": "charge_card",
                        "args": {"customer_id": "c-001", "amount": "10"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="청구가 완료됐습니다."),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "charge-run-2",
                        "name": "charge_card",
                        "args": {"customer_id": "c-001", "amount": "10"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="청구가 완료됐습니다."),
        ]
    )
    monkeypatch.setattr(server, "openrouter_model", lambda: model)
    with TestClient(app) as client:
        first = client.post("/api/run", json={"scenario_id": "parallel", "units": []})
        second = client.post("/api/run", json={"scenario_id": "parallel", "units": []})

    first_results = _charge_result_frames(first.text)
    second_results = _charge_result_frames(second.text)
    assert first_results[0]["event"]["result"]["idempotency"] == {
        "key": "charge:c-001",
        "replayed": False,
    }
    assert second_results[0]["event"]["result"]["idempotency"] == {
        "key": "charge:c-001",
        "replayed": True,
    }
    assert second_results[0]["event"]["result"]["execution"]["replayed"] is False


def test_resume_reuses_session_agent(monkeypatch):
    """Continuation dispatches an Answer with the original Agent definition."""
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

    async def fake_dispatch(self, run_id, resumed_agent, command, **kwargs):
        captured.update(
            run_id=run_id,
            agent=resumed_agent,
            command=command,
            kwargs=kwargs,
        )
        return {"stop_reason": "completed"}

    monkeypatch.setattr(server, "openrouter_model", unexpected_model)
    monkeypatch.setattr(server.AgentRuntime, "dispatch", fake_dispatch)
    server._sessions["run-agent-resume"] = {
        "units": [],
        "agent": agent,
        "scenario_id": "charge",
        "conversation_id": "conv-agent-resume",
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
        assert captured["agent"] is agent
        command = captured["command"]
        assert isinstance(command, Answer)
        assert command.pending_id == "pending-1"
        assert command.payload == {"type": "text", "text": "approved by the human"}
        assert "system_prompt" not in captured["kwargs"]
        assert captured["kwargs"]["conversation_id"] == "conv-agent-resume"
    finally:
        server._sessions.pop("run-agent-resume", None)


def test_recover_reuses_session_agent(monkeypatch):
    """Crash recovery dispatches Recover with the original Agent definition."""
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
        raise AssertionError("recover must not create a new model")

    async def fake_dispatch(self, run_id, recovered_agent, command, **kwargs):
        captured.update(
            run_id=run_id,
            agent=recovered_agent,
            command=command,
            kwargs=kwargs,
        )
        return {"stop_reason": "completed"}

    monkeypatch.setattr(server, "openrouter_model", unexpected_model)
    monkeypatch.setattr(server.AgentRuntime, "dispatch", fake_dispatch)
    server._sessions["run-agent-recover"] = {
        "units": [],
        "agent": agent,
        "scenario_id": "crash",
        "conversation_id": "conv-agent-recover",
        "aborted": False,
    }
    try:
        with TestClient(app) as c:
            response = c.post("/api/recover", json={"run_id": "run-agent-recover"})
        frames = [json.loads(line) for line in response.text.splitlines()]
        assert response.status_code == 200
        assert [frame for frame in frames if frame["kind"] == "error"] == []
        assert captured["agent"] is agent
        assert isinstance(captured["command"], Recover)
        assert "system_prompt" not in captured["kwargs"]
        assert "retry_running" not in captured["kwargs"]
        assert captured["kwargs"]["conversation_id"] == "conv-agent-recover"
    finally:
        server._sessions.pop("run-agent-recover", None)


def test_units_endpoint_shape():
    with TestClient(app) as c:
        r = c.get("/api/units")
        assert r.status_code == 200
        body = r.json()
        assert body["model"] == model_name()
        names = {u["name"] for u in body["units"]}
        assert names == {
            "input_mask", "approval", "dlp_block", "rate_cap", "pii_mask",
            "context_firewall", "injection_guard", "log_gate",
        }
        points = {u["point"] for u in body["units"]}
        assert {"pre_tool_use", "after_tool_call", "before_finish"} <= points
        assert "on_inputs" in points
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
        "chat-panel", "chat-thread", "version-switcher",
        "event-panel", "event-count",
        "outcome-strip", "rerun", "retry-run",
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
    assert "fork-run" not in shell.ids
    assert "fork-warning" not in shell.ids


def test_stylesheet_has_run_inspector_drawers_and_accessibility_contracts():
    with TestClient(app) as c:
        css = c.get("/styles.css").text

    for selector in [
        ".launch",
        ".run-shell",
        ".chat-panel",
        ".chat-message",
        ".branch-group",
        ".trace-fork",
        ".version-switcher",
        ".version-option",
        ".trace-fork-origin",
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


def test_run_workspace_pairs_chat_with_trace_and_stacks_below_desktop():
    with TestClient(app) as c:
        html = c.get("/").text
        css = c.get("/styles.css").text

    class WorkspaceParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.panel_ids: list[str] = []

        def handle_starttag(self, _tag, attrs):
            attributes = dict(attrs)
            classes = (attributes.get("class") or "").split()
            if self.depth == 0 and "run-workspace" in classes:
                self.depth = 1
                return
            if self.depth:
                if self.depth == 1 and (panel_id := attributes.get("id")):
                    self.panel_ids.append(panel_id)
                self.depth += 1

        def handle_endtag(self, _tag):
            if self.depth:
                self.depth -= 1

    workspace = WorkspaceParser()
    workspace.feed(html)

    assert workspace.panel_ids == ["chat-panel", "event-panel"]

    desktop_rule = css[css.index(".run-workspace {"):]
    desktop_rule = desktop_rule[:desktop_rule.index("}")]
    assert "display: grid" in desktop_rule
    assert "minmax(420px" in desktop_rule
    assert "minmax(640px" in desktop_rule

    responsive_rules = css[css.index("@media (max-width: 1180px)"):]
    responsive_workspace = responsive_rules[responsive_rules.index(".run-workspace {"):]
    responsive_workspace = responsive_workspace[:responsive_workspace.index("}")]
    assert "grid-template-columns: 1fr" in responsive_workspace


def test_branch_switcher_sits_inside_the_event_panel_above_the_trace():
    with TestClient(app) as c:
        html = c.get("/").text

    class EventPanelParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.ids: list[str] = []

        def handle_starttag(self, _tag, attrs):
            attributes = dict(attrs)
            if self.depth == 0 and attributes.get("id") == "event-panel":
                self.depth = 1
                return
            if self.depth:
                if element_id := attributes.get("id"):
                    self.ids.append(element_id)
                self.depth += 1

        def handle_endtag(self, _tag):
            if self.depth:
                self.depth -= 1

    event_panel = EventPanelParser()
    event_panel.feed(html)

    assert event_panel.ids.index("version-switcher") < event_panel.ids.index("event-count")
    assert event_panel.ids.index("event-count") < event_panel.ids.index("trace")


def test_live_instruction_form_sits_in_the_chat_panel_after_the_thread():
    with TestClient(app) as c:
        html = c.get("/").text

    class ChatPanelParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.ids: list[str] = []

        def handle_starttag(self, _tag, attrs):
            attributes = dict(attrs)
            if self.depth == 0 and attributes.get("id") == "chat-panel":
                self.depth = 1
                return
            if self.depth:
                if element_id := attributes.get("id"):
                    self.ids.append(element_id)
                self.depth += 1

        def handle_endtag(self, _tag):
            if self.depth:
                self.depth -= 1

    chat_panel = ChatPanelParser()
    chat_panel.feed(html)

    assert chat_panel.ids.index("chat-thread") < chat_panel.ids.index("steer-form")
    assert chat_panel.ids.index("steer-form") < chat_panel.ids.index("steer-text")


def test_policy_button_sits_with_the_run_status_and_abort_control():
    with TestClient(app) as c:
        html = c.get("/").text

    class RunControlsParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.ids: list[str] = []

        def handle_starttag(self, _tag, attrs):
            attributes = dict(attrs)
            classes = (attributes.get("class") or "").split()
            if self.depth == 0 and "run-controls" in classes:
                self.depth = 1
                return
            if self.depth:
                if element_id := attributes.get("id"):
                    self.ids.append(element_id)
                self.depth += 1

        def handle_endtag(self, _tag):
            if self.depth:
                self.depth -= 1

    controls = RunControlsParser()
    controls.feed(html)

    assert controls.ids == ["run-status", "policy-open", "abort"]


def test_run_control_buttons_keep_a_compact_click_target():
    with TestClient(app) as c:
        css = c.get("/styles.css").text

    selector = ".run-controls button {"
    assert selector in css
    rule = css[css.index(selector):]
    rule = rule[:rule.index("}")]
    assert "min-height: 34px" in rule
    assert "padding: 0 13px" in rule
    assert "font-size: 13px" in rule


def test_launch_content_starts_nearer_the_header_across_viewports():
    with TestClient(app) as c:
        css = c.get("/styles.css").text

    launch_rule = css[css.index(".launch {"):]
    launch_rule = launch_rule[:launch_rule.index("}")]
    assert "margin: clamp(48px, 9vh, 96px) auto 48px" in launch_rule

    mobile = css[css.index("@media (max-width: 820px)"):]
    mobile_launch_rule = mobile[mobile.index(".launch {"):]
    mobile_launch_rule = mobile_launch_rule[:mobile_launch_rule.index("}")]
    assert "margin-top: 36px" in mobile_launch_rule


def test_desktop_workspace_scrolls_chat_and_events_inside_the_viewport():
    with TestClient(app) as c:
        css = c.get("/styles.css").text

    desktop_marker = "@media (min-width: 1181px)"
    assert desktop_marker in css
    desktop = css[css.index(desktop_marker):]

    def rule(selector: str) -> str:
        block = desktop[desktop.index(f"{selector} {{"):]
        return block[:block.index("}")]

    run_shell = rule(".run-shell")
    assert "height: calc(100dvh - 64px)" in run_shell
    assert "grid-template-rows: auto minmax(0, 1fr)" in run_shell
    assert "padding-bottom: 64px" in run_shell

    assert "min-height: 0" in rule(".run-workspace")
    assert "overflow: hidden" in rule(".chat-panel")
    assert "overflow: auto" in rule(".chat-thread")
    assert "overflow: hidden" in rule(".event-panel")
    assert "grid-template-rows: minmax(0, 1fr)" in rule(".inspector")
    assert "overflow: auto" in rule(".trace")


def test_event_details_overlay_does_not_widen_the_trace_column():
    with TestClient(app) as c:
        css = c.get("/styles.css").text

    inspector_rule = css[css.index(".inspector.has-details {"):]
    inspector_rule = inspector_rule[:inspector_rule.index("}")]
    assert "grid-template-columns: minmax(0, 1fr)" in inspector_rule

    drawer_rule = css[css.index(".details-drawer {"):]
    drawer_rule = drawer_rule[:drawer_rule.index("}")]
    assert "position: absolute" in drawer_rule
    assert "right: 0" in drawer_rule
    assert "height: 100%" in drawer_rule
    assert "max-height: 100%" in drawer_rule
    assert "min-height: 0" in drawer_rule


def test_steer_unknown_run_is_404():
    with TestClient(app) as c:
        r = c.post("/api/steer", json={"run_id": "run-missing", "text": "기록하라"})
        assert r.status_code == 404


def test_a_steer_waits_in_the_ledger_when_no_attempt_is_running_here():
    """The queue is the ledger, so the worker holding the run does not have to be this one.

    A parked or handed-off run still takes a steer; it lands on the next drain. Only a
    finished run refuses, because nothing will drain again.
    """
    server._sessions["run-steer"] = {
        "units": [], "aborted": False, "scenario_id": "note", "terminal": False,
    }
    try:
        with TestClient(app) as c:
            waiting = c.post("/api/steer", json={"run_id": "run-steer", "text": "기록하라"})
            assert waiting.status_code == 200, waiting.text
            assert waiting.json()["admits"] == "on_resume"

            server._sessions["run-steer"]["terminal"] = True
            done = c.post("/api/steer", json={"run_id": "run-steer", "text": "기록하라"})
            assert done.status_code == 409
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


def test_recover_unknown_run_is_404():
    with TestClient(app) as c:
        r = c.post("/api/recover", json={"run_id": "run-missing"})
    assert r.status_code == 404


def test_fork_rejects_unknown_source():
    with TestClient(app) as client:
        response = client.post(
            "/api/fork",
            json={"run_id": "missing", "event_id": "event-missing", "units": []},
        )
    assert response.status_code == 404


def test_fork_rejects_inflight_source():
    agent = object()
    cases = {
        "run-not-terminal": {
            "units": ["input_mask"],
            "agent": agent,
            "scenario_id": "fork_masking",
            "terminal": False,
        },
    }
    server._sessions.update(cases)
    try:
        with TestClient(app) as client:
            for run_id in cases:
                before = set(server._sessions)
                response = client.post(
                    "/api/fork",
                    json={"run_id": run_id, "event_id": "event-selected", "units": []},
                )
                assert response.status_code == 409
                assert set(server._sessions) == before
    finally:
        for run_id in cases:
            server._sessions.pop(run_id, None)


def test_fork_uses_selected_event_and_units(monkeypatch):
    captured: dict = {}
    source_id = "run-fork-source"
    server._sessions[source_id] = {
        "units": ["input_mask", "log_gate"],
        "agent": object(),
        "scenario_id": "fork_masking",
        "terminal": True,
        "conversation_id": "conv-fork",
        "origin_id": "p2",
        "source_run_id": source_id,
        "origin_runs": {"p2": source_id},
        "default_fork_origin": "p2",
            "event_ids": {"event-selected"},
            "forkable_events": {"event-selected": "before"},
    }

    async def fake_run_from_event(_runtime, _store, _transcript, **kwargs):
        captured.update(kwargs)
        return {"stop_reason": "completed"}

    async def fake_read_checkpoint(_transcript, conversation_id, event_id):
        coordinate = ForkCoordinate(source_id, "p2", None)
        return EventCheckpoint(event_id, conversation_id, coordinate, coordinate)

    monkeypatch.setattr(server, "run_from_event", fake_run_from_event)
    monkeypatch.setattr(server, "read_event_checkpoint", fake_read_checkpoint)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/fork",
                json={
                    "run_id": source_id,
                    "event_id": "event-selected",
                    "edge": "before",
                    "units": ["log_gate"],
                },
            )
        assert response.status_code == 200
        frames = [json.loads(line) for line in response.text.splitlines()]
        meta = next(frame for frame in frames if frame["kind"] == "meta")
        fork_id = meta["run_id"]
        assert meta["fork_parent"] == source_id
        assert meta["fork_event_id"] == "event-selected"
        assert meta["fork_edge"] == "before"
        assert captured["event_id"] == "event-selected"
        assert captured["edge"] == "before"
        assert captured["run_id"] == fork_id
        assert captured["conversation_id"] == "conv-fork"
        assert server._sessions[source_id]["forked_to"] == fork_id
        assert server._sessions[fork_id]["units"] == ["log_gate"]
        assert server._sessions[fork_id]["origin_runs"]["p2"] == fork_id
        assert server._sessions[fork_id]["terminal"] is True
    finally:
        fork_id = server._sessions.get(source_id, {}).get("forked_to")
        server._sessions.pop(source_id, None)
        if fork_id:
            server._sessions.pop(fork_id, None)


def test_fork_uses_modified_controls_for_other_scenarios(monkeypatch):
    """The fork executes the controls selected after the source run completed."""
    source_id = "run-generic-source"
    composed: list[list[str]] = []
    server._sessions[source_id] = {
        "units": ["approval", "dlp_block"],
        "agent": object(),
        "scenario_id": "leak",
        "terminal": True,
        "conversation_id": "conv-generic",
        "origin_id": "p1",
        "source_run_id": source_id,
        "origin_runs": {"p1": source_id},
        "default_fork_origin": "p1",
            "event_ids": {"event-selected"},
            "forkable_events": {"event-selected": "before"},
    }

    async def fake_run_from_event(_runtime, _store, _transcript, **_kwargs):
        return {"stop_reason": "completed"}

    async def fake_read_checkpoint(_transcript, conversation_id, event_id):
        coordinate = ForkCoordinate(source_id, "p1", None)
        return EventCheckpoint(event_id, conversation_id, coordinate, coordinate)

    def fake_compose_controls(names):
        composed.append(list(names))
        return object()

    monkeypatch.setattr(server, "run_from_event", fake_run_from_event)
    monkeypatch.setattr(server, "read_event_checkpoint", fake_read_checkpoint)
    monkeypatch.setattr(server, "compose_controls", fake_compose_controls)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/fork",
                json={
                    "run_id": source_id,
                    "event_id": "event-selected",
                    "units": ["rate_cap"],
                },
            )
        assert response.status_code == 200
        fork_id = server._sessions[source_id]["forked_to"]
        assert server._sessions[fork_id]["units"] == ["rate_cap"]
        assert composed == [["rate_cap"]]
    finally:
        fork_id = server._sessions.get(source_id, {}).get("forked_to")
        server._sessions.pop(source_id, None)
        if fork_id:
            server._sessions.pop(fork_id, None)


def test_tool_leaf_fork_keeps_the_child_on_leaf_coordinates(monkeypatch):
    source_id = "run-tool-source"
    server._sessions[source_id] = {
        "units": [],
        "agent": object(),
        "scenario_id": "customer",
        "terminal": True,
        "conversation_id": "conv-tool",
        "origin_id": "p1",
        "origin_runs": {"p1": source_id},
        "default_fork_origin": "p1",
        "event_ids": {"event-pre-tool"},
        "forkable_events": {"event-pre-tool": "before"},
    }

    async def fake_run_from_event(_runtime, _store, _transcript, **_kwargs):
        return {"stop_reason": "completed"}

    async def fake_read_checkpoint(_transcript, conversation_id, event_id):
        coordinate = ForkCoordinate(source_id, None, "assistant-tool-leaf")
        return EventCheckpoint(event_id, conversation_id, coordinate, coordinate)

    monkeypatch.setattr(server, "run_from_event", fake_run_from_event)
    monkeypatch.setattr(server, "read_event_checkpoint", fake_read_checkpoint)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/fork",
                json={
                    "run_id": source_id,
                    "event_id": "event-pre-tool",
                    "edge": "before",
                    "units": [],
                },
            )
        assert response.status_code == 200
        frames = [json.loads(line) for line in response.text.splitlines()]
        meta = next(frame for frame in frames if frame["kind"] == "meta")
        child = server._sessions[meta["run_id"]]
        assert meta["fork_mode"] == "leaf"
        assert child["default_fork_origin"] is None
    finally:
        child_ids = server._sessions.get(source_id, {}).get("fork_children", [])
        server._sessions.pop(source_id, None)
        for child_id in child_ids:
            server._sessions.pop(child_id, None)


def test_completed_version_can_create_another_fork(monkeypatch):
    """A prior child version must not lock a completed version against later forks."""
    source_id = "run-version-source"
    server._sessions[source_id] = {
        "units": [],
        "agent": object(),
        "scenario_id": "note",
        "terminal": True,
        "conversation_id": "conv-version",
        "origin_id": "p1",
        "origin_runs": {"p1": source_id},
        "default_fork_origin": "p1",
        "forked_to": "run-older-child",
            "event_ids": {"event-next"},
            "forkable_events": {"event-next": "before"},
    }

    async def fake_run_from_event(_runtime, _store, _transcript, **_kwargs):
        return {"stop_reason": "completed"}

    async def fake_read_checkpoint(_transcript, conversation_id, event_id):
        coordinate = ForkCoordinate(source_id, "p1", None)
        return EventCheckpoint(event_id, conversation_id, coordinate, coordinate)

    monkeypatch.setattr(server, "run_from_event", fake_run_from_event)
    monkeypatch.setattr(server, "read_event_checkpoint", fake_read_checkpoint)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/fork",
                json={"run_id": source_id, "event_id": "event-next", "units": []},
            )
        assert response.status_code == 200
        assert len(server._sessions[source_id]["fork_children"]) == 1
    finally:
        child_ids = server._sessions.get(source_id, {}).get("fork_children", [])
        server._sessions.pop(source_id, None)
        for child_id in child_ids:
            server._sessions.pop(child_id, None)


def test_fork_rejects_an_event_owned_by_another_version():
    source_id = "run-owner-source"
    server._sessions[source_id] = {
        "units": [],
        "agent": object(),
        "scenario_id": "note",
        "terminal": True,
        "conversation_id": "conv-owner",
        "origin_id": "p1",
        "origin_runs": {"p1": source_id},
        "default_fork_origin": "p1",
            "event_ids": {"event-owned"},
            "forkable_events": {"event-owned": "before"},
    }
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/fork",
                json={"run_id": source_id, "event_id": "event-sibling", "units": []},
            )
        assert response.status_code == 404
        assert "does not belong" in response.json()["detail"]
    finally:
        server._sessions.pop(source_id, None)


def test_fork_rejects_an_observation_only_event():
    source_id = "run-observation-source"
    server._sessions[source_id] = {
        "units": [],
        "agent": object(),
        "scenario_id": "note",
        "terminal": True,
        "conversation_id": "conv-observation",
        "origin_id": "p1",
        "origin_runs": {"p1": source_id},
        "default_fork_origin": "p1",
        "event_ids": {"event-tool"},
        "forkable_events": {},
    }
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/fork",
                json={"run_id": source_id, "event_id": "event-tool", "units": []},
            )
        assert response.status_code == 409
        assert "not an exact restore point" in response.json()["detail"]
    finally:
        server._sessions.pop(source_id, None)


def test_fork_rejects_the_wrong_edge_for_an_exact_restore_point():
    source_id = "run-edge-source"
    server._sessions[source_id] = {
        "units": [],
        "agent": object(),
        "scenario_id": "note",
        "terminal": True,
        "conversation_id": "conv-edge",
        "origin_id": "p1",
        "origin_runs": {"p1": source_id},
        "default_fork_origin": "p1",
        "event_ids": {"event-input"},
        "forkable_events": {"event-input": "before"},
    }
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/fork",
                json={
                    "run_id": source_id,
                    "event_id": "event-input",
                    "edge": "after",
                    "units": [],
                },
            )
        assert response.status_code == 409
        assert "before edge" in response.json()["detail"]
    finally:
        server._sessions.pop(source_id, None)


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
        assert r.status_code == 200 and r.json()["ok"] is True
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


def test_a_parked_run_resumes_in_a_process_that_never_saw_it(monkeypatch):
    """The README proves exactly-once across a restart, and that proof went through
    /api/resume — which used to 404 because the console's session dict is memory. The
    run record lives in the ledger now, so a worker that never saw the run can finish it.
    """
    model = BoundFakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "id": "charge-restart-1", "name": "charge_card",
                    "args": {"customer_id": "c-001", "amount": 49}, "type": "tool_call",
                }],
            ),
            AIMessage(content="청구가 완료됐습니다."),
        ]
    )
    monkeypatch.setattr(server, "openrouter_model", lambda: model)
    with TestClient(app) as client:
        started = client.post("/api/run", json={"scenario_id": "charge", "units": ["approval"]})
        frames = [json.loads(line) for line in started.text.splitlines()]
        run_id = next(f["run_id"] for f in frames if f["kind"] == "meta")
        pending = next(f["pending_id"] for f in frames if f["kind"] == "suspended")

        # The worker goes away. Only the ledger survives.
        server._sessions.clear()

        resumed = client.post("/api/resume", json={
            "run_id": run_id, "pending_id": pending, "approved": True,
        })
        assert resumed.status_code == 200, resumed.text
        after = [json.loads(line) for line in resumed.text.splitlines()]

    charged = [
        f["event"]["result"] for f in after
        if f.get("kind") == "agent"
        and f["event"].get("type") == "tool_result"
        and "charged" in str((f["event"].get("result") or {}).get("text", ""))
    ]
    assert charged, after
    assert charged[0]["execution_count"] == 1
    assert server._sessions[run_id]["units"] == ["approval"]
    assert server._sessions[run_id]["scenario_id"] == "charge"


def test_an_unknown_run_id_is_still_a_404(monkeypatch):
    """Rehydration must not turn a bad id into a run."""
    with TestClient(app) as client:
        refused = client.post("/api/resume", json={
            "run_id": "run-does-not-exist", "pending_id": "x", "approved": True,
        })
    assert refused.status_code == 404


def test_an_effect_that_started_and_never_reported_is_not_guessed(monkeypatch):
    """The crash seam that leaves a step running is the only one recovery cannot decide.

    Retrying repeats a charge that may already have gone out; reporting it stops and
    hands the call to a person. The runtime asks rather than picking, so the console has
    to ask too.
    """
    model = StreamingFakeChatModel(
        messages=iter([
            AIMessage(
                content="",
                tool_calls=[{
                    "id": "charge-unknown-1", "name": "charge_card",
                    "args": {"customer_id": "c-001", "amount": 49}, "type": "tool_call",
                }],
            ),
            AIMessage(content="청구했습니다."),
        ])
    )
    monkeypatch.setattr(server, "openrouter_model", lambda: model)
    with TestClient(app) as client:
        started = client.post("/api/run", json={"scenario_id": "unknown_effect", "units": []})
        frames = [json.loads(line) for line in started.text.splitlines()]
        run_id = next(f["run_id"] for f in frames if f["kind"] == "meta")
        assert any(f["kind"] == "recoverable" for f in frames), frames

        recovered = client.post("/api/recover", json={"run_id": run_id})
        after = [json.loads(line) for line in recovered.text.splitlines()]

    unknown = [f for f in after if f["kind"] == "indeterminate"]
    assert unknown, after
    assert unknown[0]["step"]
    assert not [
        f for f in after
        if f.get("kind") == "agent"
        and f["event"].get("type") == "tool_result"
        and "charged" in str((f["event"].get("result") or {}).get("text", ""))
    ], "the runtime refuses rather than repeating an effect it cannot vouch for"


@pytest.mark.asyncio
async def test_aborting_drops_what_is_still_queued_before_it_stops():
    """A steer queued for a killed run must not be waiting for whoever recovers next.

    The loop is stopped after the queue is emptied, not before, so nothing admitted on
    the way down is an instruction aimed at a run the operator already ended.
    """
    server._sessions["run-drop"] = {
        "units": [], "aborted": False, "scenario_id": "note", "terminal": False,
    }
    try:
        with TestClient(app) as c:
            c.post("/api/steer", json={"run_id": "run-drop", "text": "이건 버려져야 한다"})
            queued = await server._store.list_inputs("run-drop")
            assert [record.status for record in queued] == ["pending"]

            stopped = c.post("/api/abort", json={"run_id": "run-drop"})
            assert stopped.json()["dropped"] == 1

        after = await server._store.list_inputs("run-drop")
        assert [record.status for record in after] == ["discarded"]
        assert after[0].value, "a discarded input keeps its idempotency key"
    finally:
        server._sessions.pop("run-drop", None)


def test_steering_a_parked_run_adds_a_note_without_taking_the_decision(monkeypatch):
    """A steer joins the queue; it does not answer the approval that is waiting.

    submit() in interactive mode treats operator input as a replacement and cancels a
    parked request to switch the run to it. That is right for a new prompt and wrong for
    a steer, which is why this goes in headless. The synthetic sessions elsewhere in this
    file cannot reach any of it — a real suspension has to exist first.
    """
    model = StreamingFakeChatModel(
        messages=iter([
            AIMessage(
                content="",
                tool_calls=[{
                    "id": "charge-steered-1", "name": "charge_card",
                    "args": {"customer_id": "c-001", "amount": 49}, "type": "tool_call",
                }],
            ),
            AIMessage(content="청구했습니다."),
        ])
    )
    monkeypatch.setattr(server, "openrouter_model", lambda: model)
    with TestClient(app) as client:
        started = client.post("/api/run", json={"scenario_id": "charge", "units": ["approval"]})
        frames = [json.loads(line) for line in started.text.splitlines()]
        run_id = next(f["run_id"] for f in frames if f["kind"] == "meta")
        pending = next(f["pending_id"] for f in frames if f["kind"] == "suspended")

        steered = client.post(
            "/api/steer", json={"run_id": run_id, "text": "금액을 다시 확인해줘"}
        )
        assert steered.status_code == 200, steered.text
        assert steered.json()["admits"] == "on_resume"

        resumed = client.post("/api/resume", json={
            "run_id": run_id, "pending_id": pending, "approved": True, "units": ["approval"],
        })
        after = [json.loads(line) for line in resumed.text.splitlines()]

    charged = [
        f for f in after
        if f.get("kind") == "agent"
        and f["event"].get("type") == "tool_result"
        and "charged" in str((f["event"].get("result") or {}).get("text", ""))
    ]
    assert charged, "the steer must not have cancelled the call that was waiting"
    admitted = [f for f in after if f["kind"] == "steer"]
    assert [(f["source"], f["phase"]) for f in admitted] == [("user_steer", "admitted")]


def test_two_visitors_do_not_replay_each_others_charges():
    """Effects are steps of a session, and the session is whoever is driving.

    On a link two people can open at once, one session would mean the first visitor
    charges and the rest watch a replay of it.
    """
    assert _session_id("donggyun", "charge") == "session:donggyun:charge"
    assert _session_id("Dong Gyun!", "charge") == "session:donggyun:charge", "sanitised"
    assert _session_id("", "charge") == "session:charge", "unnamed share one session"
    assert _session_id("   ", "charge") == "session:charge"
    assert _session_id("a" * 80, "charge") == f"session:{'a' * 24}:charge", "bounded"
    assert _session_id("alice", "charge") != _session_id("bob", "charge")


@pytest.mark.asyncio
async def test_a_named_run_keeps_its_ledger_across_a_restart():
    """A resumed run has to rejoin the session its effects are steps of, or the charge
    it already made is invisible to it and goes out twice."""
    from console.store import make_store

    server._store, server._transcript, _ = await make_store()
    server._sessions["run-named"] = {
        "units": [], "scenario_id": "charge", "session_id": "session:alice:charge",
        "conversation_id": "conv-x", "aborted": False, "terminal": False,
    }
    try:
        await server._remember_session("run-named", server._sessions["run-named"])
        server._sessions.clear()
        rehydrated = await server._session("run-named")
        # The rebuilt tools rejoin the session their effects are steps of.
        recorded = await rehydrated["agent"].tools.execute(
            "charge_card", "after-restart", {"customer_id": "c-001", "amount": "49"}
        )
        assert recorded["idempotency"]["key"] == "charge:c-001"
    finally:
        server._sessions.pop("run-named", None)
