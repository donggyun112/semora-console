from console.dormancy import dormant_reason
from console.scenarios import SCENARIOS


def test_four_scenarios_well_formed():
    ids = [s["id"] for s in SCENARIOS]
    assert ids == ["note", "customer", "charge", "batch"]
    for s in SCENARIOS:
        assert s["prompt"] and s["title"] and s["risk"]


def test_dormant_reason_specific_then_default():
    assert "remember_note" in dormant_reason("log_gate", "note")  # scenario-specific
    assert dormant_reason("pii_mask", "charge")  # per-unit default, non-empty
    assert dormant_reason("unknown_unit", "charge")  # generic fallback, non-empty
