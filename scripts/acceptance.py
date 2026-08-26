"""Live acceptance gate: prove each unit visibly acts under a real LLM run.

Requires a running server (default http://127.0.0.1:8850) with OPENROUTER_API_KEY set.
Run: uv run python scripts/acceptance.py
"""

from __future__ import annotations

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8850"


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


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        check.failed += 1


check.failed = 0


def main() -> None:
    print("1) approval + charge → suspend → resume → exec ×1")
    fs = stream("/api/run", {"scenario_id": "charge", "units": ["approval"]})
    pend = pending(fs)
    check("suspended", pend is not None)
    if pend:
        fs2 = stream("/api/resume", {"run_id": run_id(fs), "pending_id": pend, "approved": True})
        charged = [r for r in results(fs2) if "charged" in str(r.get("text", ""))]
        check("charge ran exactly once", bool(charged) and charged[0].get("execution_count") == 1, str(charged[:1]))

    print("2) dlp_block + customer → deny on send")
    fs = stream("/api/run", {"scenario_id": "customer", "units": ["dlp_block"]})
    check("dlp_block denied", any(u["unit"] == "dlp_block" for u in units(fs, "deny")))

    print("3) rate_cap + batch → deny 3rd, only 2 charged")
    fs = stream("/api/run", {"scenario_id": "batch", "units": ["rate_cap"]})
    charged = [r for r in results(fs) if "charged" in str(r.get("text", ""))]
    check("rate_cap denied", any(u["unit"] == "rate_cap" for u in units(fs, "deny")))
    check("only 2 effects executed", len(charged) == 2, f"{len(charged)} charged")

    print("4) pii_mask + customer → read result masked + rewrite frame")
    fs = stream("/api/run", {"scenario_id": "customer", "units": ["pii_mask"]})
    read = [r for r in results(fs) if "customer" in str(r.get("text", ""))]
    masked = read and "jane@doe.io" not in read[0]["text"] and "123-45-6789" not in read[0]["text"]
    check("read result masked", bool(masked), str(read[:1]))
    check("rewrite frame present", any(u["unit"] == "pii_mask" for u in units(fs, "rewrite")))

    print("5) log_gate + charge → steer + finish")
    fs = stream("/api/run", {"scenario_id": "charge", "units": ["log_gate"]})
    check("steer frame present", bool(units(fs, "steer")))
    check("run finished", any(f["kind"] == "outcome" for f in fs))
    print(f"      (soft) tools called: {tool_calls(fs)}")

    print("6) HEADLINE customer + [approval, dlp_block, pii_mask] → mask on read, DENY (not suspend) on send")
    fs = stream("/api/run", {"scenario_id": "customer", "units": ["approval", "dlp_block", "pii_mask"]})
    check("read masked", any(u["unit"] == "pii_mask" for u in units(fs, "rewrite")))
    check("send denied (deny wins over suspend)", any(u["unit"] == "dlp_block" for u in units(fs, "deny")))

    print("7) dormancy — customer + [pii_mask, rate_cap] → rate_cap dormant with reason")
    fs = stream("/api/run", {"scenario_id": "customer", "units": ["pii_mask", "rate_cap"]})
    s = summary(fs)
    check("policy_summary present", s is not None)
    if s:
        rc = next((u for u in s["units"] if u["name"] == "rate_cap"), None)
        pm = next((u for u in s["units"] if u["name"] == "pii_mask"), None)
        check("rate_cap dormant with reason", rc is not None and not rc["fired"] and bool(rc["reason"]), str(rc))
        check("pii_mask fired", pm is not None and pm["fired"], str(pm))

    print()
    if check.failed:
        print(f"{check.failed} CHECK(S) FAILED")
        sys.exit(1)
    print("ALL LIVE CHECKS PASS")


if __name__ == "__main__":
    main()
