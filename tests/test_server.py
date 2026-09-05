from html.parser import HTMLParser

from fastapi.testclient import TestClient

from console import server
from console.fork_demo import RERUNS
from console.provider import model_name
from console.server import (
    _crash_point,
    _is_aborted,
    _project_event,
    _register_frame,
    _session_id,
    app,
)


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
    assert [f["kind"] for f in frames] == ["unit", "agent", "agent"]
    assert frames[0]["unit"] == "dlp_block" and frames[0]["verdict"] == "deny"
    assert frames[1]["event"]["blocked"] is True
    assert frames[1]["event"]["name"] == "send_email"
    assert frames[1]["checkpoint_phase"] == "tool_result"
    assert frames[1]["call_id"] == "c1"
    # The refusal is the result the model got, so the call ends in a line naming it.
    assert frames[2]["event"]["type"] == "tool_result"
    assert frames[2]["event"]["executed"] is False

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

    blocked = next(f for f in frames if f.get("event", {}).get("blocked"))
    assert blocked["event"]["id"] == "charge-2"
    assert blocked["event"]["name"] == "charge_card"
    assert frames[-1]["event"]["type"] == "tool_result"
    assert frames[-1]["event"]["executed"] is False

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

def test_units_endpoint_shape():
    with TestClient(app) as c:
        r = c.get("/api/units")
        assert r.status_code == 200
        body = r.json()
        assert body["model"] == model_name()
        names = {u["name"] for u in body["units"]}
        assert names == {
            "input_mask", "approval", "dlp_block", "rate_cap", "pii_mask",
            "context_firewall", "injection_guard", "result_drop", "log_gate",
        }
        points = {u["point"] for u in body["units"]}
        assert {"pre_tool_use", "post_tool_use", "before_finish"} <= points
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
        "steer-text", "policy-drawer", "policy-close", "units",
        "compose-summary", "approval", "approval-args", "approve", "deny", "recovery",
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

def test_a_run_nobody_kept_is_not_offered_back():
    """A wiped ledger has to say so, or the console restores a run that does not exist."""
    with TestClient(app) as client:
        assert client.get("/api/runs/run-nothing-here/frames").status_code == 404

def test_units_carry_the_framework_rerun_table():
    """The page learns which control points a branch re-runs from the framework, not from a
    list of policy names it keeps for itself."""
    with TestClient(app) as client:
        payload = client.get("/api/units").json()
    assert payload["reruns"] == {name: list(points) for name, points in RERUNS.items()}
    assert {u["point"] for u in payload["units"]} <= {
        point for points in payload["reruns"].values() for point in points
    }, "every unit lives at a point some branch can reach"
