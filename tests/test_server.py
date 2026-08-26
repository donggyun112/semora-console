from fastapi.testclient import TestClient

from console.server import app


def test_scenarios_endpoint():
    with TestClient(app) as c:
        r = c.get("/api/scenarios")
        assert r.status_code == 200
        assert [s["id"] for s in r.json()] == ["note", "customer", "charge", "batch"]


def test_units_endpoint_shape():
    with TestClient(app) as c:
        r = c.get("/api/units")
        assert r.status_code == 200
        body = r.json()
        assert body["model"].startswith("deepseek/")
        names = {u["name"] for u in body["units"]}
        assert names == {"approval", "dlp_block", "rate_cap", "pii_mask", "context_firewall", "log_gate"}
        points = {u["point"] for u in body["units"]}
        assert {"pre_tool_use", "after_tool_call", "before_finish"} <= points
