from console.dormancy import dormant_reason
from console.scenarios import SCENARIOS


def test_scenarios_well_formed():
    ids = [s["id"] for s in SCENARIOS]
    assert ids == [
        "note", "customer", "leak", "inject", "charge", "crash", "batch", "parallel",
        "parallel_crash", "fork_masking",
    ]
    for s in SCENARIOS:
        assert s["prompt"] and s["title"] and s["risk"]
        assert s["forkable"] is True


def test_fork_masking_scenario_starts_with_input_mask():
    scenario = next(item for item in SCENARIOS if item["id"] == "fork_masking")
    assert scenario["default_units"] == ["input_mask"]
    assert scenario["forkable"] is True


def test_inject_is_tool_result_directive():
    inject = next(s for s in SCENARIOS if s["id"] == "inject")
    assert "c-inj" in inject["prompt"]
    assert "적힌 대로" in inject["prompt"]


def test_parallel_asks_for_one_batch_not_serial():
    parallel = next(s for s in SCENARIOS if s["id"] == "parallel")
    assert "동시에" in parallel["prompt"]
    assert "나눠서" in parallel["prompt"]


def test_parallel_crash_asks_for_one_batch_not_serial():
    parallel = next(s for s in SCENARIOS if s["id"] == "parallel_crash")
    assert "동시에" in parallel["prompt"]
    assert "나눠서" in parallel["prompt"]


def test_leak_is_external_ssn_exfil():
    leak = next(s for s in SCENARIOS if s["id"] == "leak")
    assert "leaker@personal-mail.com" in leak["prompt"]
    assert "주민번호" in leak["prompt"] or "SSN" in leak["prompt"]


def test_dormant_reason_specific_then_default():
    assert "remember_note" in dormant_reason("log_gate", "note")  # scenario-specific
    assert "본문" in dormant_reason("dlp_block", "leak") or "메일" in dormant_reason("dlp_block", "leak")
    assert "구조화" in dormant_reason("injection_guard", "note")
    assert dormant_reason("pii_mask", "charge")  # per-unit default, non-empty
    assert dormant_reason("unknown_unit", "charge")  # generic fallback, non-empty
