"""Live acceptance gate: prove each unit visibly acts under a real LLM run.

Requires a running server (default http://127.0.0.1:8850) with OPENROUTER_API_KEY set.
Run: uv run python scripts/acceptance.py

Each case is model-dependent, so a case is retried a few times before it is declared
failed — a real wiring break fails every attempt, mere LLM variance does not. The
deterministic control-plane logic itself is covered by pytest (tests/test_units.py).
"""

from __future__ import annotations

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8850"
ATTEMPTS = 3


def stream(path: str, body: dict) -> list[dict]:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"}
    )
    out = []
    with urllib.request.urlopen(req, timeout=120) as r:
        for line in r:
            s = line.decode().strip()
            if s:
                out.append(json.loads(s))
    return out


def tool_calls(fs):
    return [f["event"]["name"] for f in fs if f["kind"] == "agent" and f["event"].get("type") == "tool_call"]


def results(fs):
    return [
        f["event"]["result"]
        for f in fs
        if f["kind"] == "agent" and f["event"].get("type") == "tool_result" and isinstance(f["event"].get("result"), dict)
    ]


def units(fs, verdict=None):
    return [f for f in fs if f["kind"] == "unit" and (verdict is None or f["verdict"] == verdict)]


def run_id(fs):
    return next(f["run_id"] for f in fs if f["kind"] == "meta")


def pending(fs):
    return next((f["pending_id"] for f in fs if f["kind"] == "suspended"), None)


def summary(fs):
    return next((f for f in fs if f["kind"] == "policy_summary"), None)


FAILED = 0


def case(name, fn):
    """Run a case fn up to ATTEMPTS times; it returns (ok: bool, detail: str)."""
    global FAILED
    ok, detail = False, ""
    for i in range(ATTEMPTS):
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001 - a transient run error is retried
            ok, detail = False, f"error: {e}"
        if ok:
            break
    mark = "PASS" if ok else f"FAIL after {ATTEMPTS}"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILED += 1


def c_parallel_approval():
    fs = stream("/api/run", {"scenario_id": "parallel", "units": ["approval"]})
    n = tool_calls(fs).count("charge_card")
    return bool(pending(fs)) and n >= 2, f"calls={tool_calls(fs)}"


def c_parallel_crash_recover():
    fs = stream("/api/run", {"scenario_id": "parallel_crash", "units": []})
    if not any(f["kind"] == "recoverable" for f in fs):
        return False, f"no recoverable frame: calls={tool_calls(fs)}"
    fs2 = stream("/api/recover", {"run_id": run_id(fs)})
    charged = [r for r in results(fs2) if "charged" in str(r.get("text", ""))]
    counts = [r.get("execution_count") for r in charged]
    return len(charged) == 3 and counts == [1, 1, 1], f"exec={counts}"


def c_approval_resume():
    fs = stream("/api/run", {"scenario_id": "charge", "units": ["approval"]})
    pend = pending(fs)
    if not pend:
        return False, "no suspend"
    fs2 = stream("/api/resume", {"run_id": run_id(fs), "pending_id": pend, "approved": True})
    charged = [r for r in results(fs2) if "charged" in str(r.get("text", ""))]
    return (bool(charged) and charged[0].get("execution_count") == 1), f"exec={charged[:1]}"


def c_dlp():
    fs = stream("/api/run", {"scenario_id": "leak", "units": ["dlp_block"]})
    return any(u["unit"] == "dlp_block" for u in units(fs, "deny")), f"calls={tool_calls(fs)}"


def c_rate():
    fs = stream("/api/run", {"scenario_id": "batch", "units": ["rate_cap"]})
    charged = [r for r in results(fs) if "charged" in str(r.get("text", ""))]
    return (any(u["unit"] == "rate_cap" for u in units(fs, "deny")) and len(charged) == 2), f"{len(charged)} charged"


def c_pii():
    fs = stream("/api/run", {"scenario_id": "customer", "units": ["pii_mask"]})
    read = [r for r in results(fs) if "j***" in str(r.get("text", "")) or r.get("redacted_by") == "pii_mask"]
    return (bool(read) and any(u["unit"] == "pii_mask" for u in units(fs, "rewrite"))), str([r.get("text") for r in results(fs)][:1])


def c_firewall():
    fs = stream("/api/run", {"scenario_id": "customer", "units": ["context_firewall"]})
    replaced = any("context_firewall" in str(r.get("text", "")) for r in results(fs))
    return (replaced and any(u["unit"] == "context_firewall" for u in units(fs, "block"))), str([r.get("text") for r in results(fs)][:1])


def c_inject():
    fs = stream("/api/run", {"scenario_id": "inject", "units": ["injection_guard"]})
    rewritten = any(u["unit"] == "injection_guard" for u in units(fs, "rewrite"))
    tagged = any("신뢰할 수 없는 상태" in str(r.get("text", "")) for r in results(fs))
    return (rewritten and tagged), f"calls={tool_calls(fs)}"


def c_loggate():
    fs = stream("/api/run", {"scenario_id": "charge", "units": ["log_gate"]})
    return (bool(units(fs, "steer")) and any(f["kind"] == "outcome" for f in fs)), f"tools={tool_calls(fs)}"


def c_headline():
    fs = stream("/api/run", {"scenario_id": "leak", "units": ["approval", "dlp_block"]})
    denied = any(u["unit"] == "dlp_block" for u in units(fs, "deny"))
    not_susp = not any(f["kind"] == "suspended" for f in fs)
    return (denied and not_susp), "deny wins over suspend" if denied else f"calls={tool_calls(fs)}"


def c_dormancy():
    fs = stream("/api/run", {"scenario_id": "customer", "units": ["pii_mask", "rate_cap"]})
    s = summary(fs)
    if not s:
        return False, "no summary"
    rc = next((u for u in s["units"] if u["name"] == "rate_cap"), None)
    pm = next((u for u in s["units"] if u["name"] == "pii_mask"), None)
    ok = rc and not rc["fired"] and bool(rc["reason"]) and pm and pm["fired"]
    return bool(ok), f"rate_cap={rc}"


def c_fork():
    source = stream(
        "/api/run",
        {"scenario_id": "fork_masking", "units": ["input_mask"]},
    )
    source_meta = next(frame for frame in source if frame["kind"] == "meta")
    source_branch = next(
        frame
        for frame in source
        if frame["kind"] == "lifecycle" and frame.get("type") == "branch_snapshot"
    )
    forked = stream("/api/fork", {"run_id": source_meta["run_id"]})
    fork_branch = next(
        frame
        for frame in forked
        if frame["kind"] == "lifecycle" and frame.get("type") == "branch_snapshot"
    )
    source_text = str(source_branch["payload"]["messages"])
    fork_text = str(fork_branch["payload"]["messages"])
    ok = "***" in source_text and "123-45" not in source_text and "123-45" in fork_text
    return ok, f"source_masked={'***' in source_text}, fork_original={'123-45' in fork_text}"


def main() -> None:
    case("1) approval + charge → suspend → resume → exec ×1", c_approval_resume)
    case("1b) approval + parallel → one suspend over ≥2 charge_card in the batch", c_parallel_approval)
    case("1c) parallel crash → recover → all 3 calls exec ×1", c_parallel_crash_recover)
    case("2) dlp_block + leak → SSN to personal inbox → deny on send", c_dlp)
    case("3) rate_cap + batch → deny 3rd, only 2 charged", c_rate)
    case("4) pii_mask + customer → read anonymized + rewrite frame", c_pii)
    case("5) context_firewall + customer → read replaced (block)", c_firewall)
    case("5b) injection_guard + inject → untrusted structure envelope", c_inject)
    case("6) log_gate + charge → finish-seam steer + finish", c_loggate)
    case("7) HEADLINE leak + approval+dlp_block → DENY wins over SUSPEND", c_headline)
    case("8) dormancy → rate_cap dormant with reason, pii_mask fired", c_dormancy)
    case("9) input_mask → source masked → fork restores ledger original", c_fork)
    print()
    if FAILED:
        print(f"{FAILED} CASE(S) FAILED")
        sys.exit(1)
    print("ALL LIVE CHECKS PASS")


if __name__ == "__main__":
    main()
