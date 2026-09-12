import asyncio

from pydantic_ai.messages import UserPromptPart
from semora import PendingInput

from console.dormancy import dormant_reason
from console.scenarios import SCENARIOS
from console.units import UNITS_BY_NAME, input_mask


def test_input_mask_has_a_prompt_it_actually_rewrites():
    """The ingress seam only shows up in a prompt that carries the number itself.

    Every other scenario keeps its SSN in a tool result, out of on_inputs' reach, so
    without this one the unit sits in the picker rewriting nothing — the console says a
    policy ran while the number sails past.
    """
    home = next(s for s in SCENARIOS if "input_mask" in s.get("default_units", []))
    for prompt in (home["prompt"], home["en"]["prompt"]):
        item = PendingInput("user_prompt", UserPromptPart(prompt), "origin")
        masked = str(asyncio.run(input_mask(None, [item]))[0].part.content)
        assert masked != prompt, prompt
        assert "123-45-6789" not in masked


def test_scenarios_well_formed():
    ids = [s["id"] for s in SCENARIOS]
    # crash leads: recovery is the differentiator, so it is what a visitor meets first.
    assert ids == [
        "crash", "note", "operator_pii", "customer", "leak", "inject", "charge", "unknown_effect",
        "batch", "parallel", "parallel_crash", "fork_masking",
    ]
    for s in SCENARIOS:
        assert s["prompt"] and s["title"] and s["risk"]
        assert s["forkable"] is True
        assert isinstance(s["default_units"], list)
        assert set(s["default_units"]) <= set(UNITS_BY_NAME)


def test_each_scenario_has_a_curated_default_policy():
    defaults = {s["id"]: s["default_units"] for s in SCENARIOS}
    assert defaults == {
        "crash": [],
        "note": [],
        "operator_pii": ["input_mask"],
        "customer": ["pii_mask"],
        "leak": ["approval", "dlp_block"],
        "inject": ["injection_guard"],
        "charge": ["approval"],
        "unknown_effect": [],
        "batch": ["rate_cap"],
        "parallel": ["approval"],
        "parallel_crash": [],
        "fork_masking": ["pii_mask"],
    }


def test_fork_masking_scenario_starts_with_pii_mask():
    scenario = next(item for item in SCENARIOS if item["id"] == "fork_masking")
    assert scenario["default_units"] == ["pii_mask"]
    assert "read_customer" in scenario["prompt"]
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
