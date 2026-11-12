"""Standalone verification script for Task 06: Cockpit Telemetry UI & End-to-End Verification."""

import json
import time
import urllib.request
import urllib.error

BASE_URL = "http://127.0.0.1:1434"

def test_cockpit_telemetry():
    print("=" * 70)
    print("  TASK 06: COCKPIT TELEMETRY UI & END-TO-END VERIFICATION")
    print("=" * 70)

    # 1. Probe /mesh/api/atse/status
    print("\n[Phase 1] Probing Live /mesh/api/atse/status Endpoint...")
    t0 = time.monotonic()
    req = urllib.request.Request(f"{BASE_URL}/mesh/api/atse/status")
    with urllib.request.urlopen(req) as resp:
        dur_ms = round((time.monotonic() - t0) * 1000, 2)
        assert resp.status == 200, f"Expected 200, got {resp.status}"
        data = json.loads(resp.read().decode("utf-8"))

        print(f"  HTTP Status:       {resp.status} OK ({dur_ms} ms)")
        print(f"  Architecture:      {data.get('architecture')}")
        print(f"  System Status:     {data.get('status')}")
        print(f"  Codebase Entropy:  {data.get('codebase_ast', {}).get('entropy_h')} (Nodes: {data.get('codebase_ast', {}).get('total_nodes')}, Edges: {data.get('codebase_ast', {}).get('total_edges')})")
        print(f"  Common-Sense Gate: {data.get('common_sense_gate', {}).get('status')} (&Omega;_sense: {data.get('common_sense_gate', {}).get('current_omega_sense')}, Max Drift: {data.get('common_sense_gate', {}).get('max_allowed_drift')})")
        print(f"  Subagent Genesis:  Depth-{data.get('subagent_genesis', {}).get('max_depth')} ({data.get('subagent_genesis', {}).get('state')}) - {data.get('subagent_genesis', {}).get('hardware_model')}")
        print(f"  Triad Consensus:   {data.get('triad_consensus', {}).get('majority_threshold')} - {data.get('triad_consensus', {}).get('security_veto')}")
        print(f"  Shadow Staging:    {data.get('shadow_staging', {}).get('status')} - SLA {data.get('shadow_staging', {}).get('rollback_sla')}")

        assert data["ok"] is True
        assert data["codebase_ast"]["entropy_h"] == 0.8451
        assert data["subagent_genesis"]["max_depth"] == 1
        assert data["common_sense_gate"]["max_allowed_drift"] == 0.45
        print("  -> Phase 1 Passed: Real-time ATSE telemetry API fully operational.")

    # 2. Probe /mesh/api/state
    print("\n[Phase 2] Probing Mesh State ATSE Enrichment...")
    req_state = urllib.request.Request(f"{BASE_URL}/mesh/api/state")
    with urllib.request.urlopen(req_state) as resp_state:
        state_data = json.loads(resp_state.read().decode("utf-8"))
        atse = state_data.get("atse", {})
        print(f"  Mesh State ATSE:   {atse}")
        assert atse.get("enabled") is True
        assert atse.get("codebase_entropy") == 0.8451
        assert atse.get("subagent_max_depth") == 1
        assert atse.get("status") == "OPERATIONAL_24_7"
        print("  -> Phase 2 Passed: Mesh state enriched with live ATSE invariants.")

    # 3. Interactive Diagnostic Trigger
    print("\n[Phase 3] Triggering Interactive Diagnostic ATSE Cycle...")
    t0 = time.monotonic()
    req_trigger = urllib.request.Request(
        f"{BASE_URL}/mesh/api/atse/trigger-cycle",
        data=b"{}",
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req_trigger) as resp_trig:
        dur_trig = round((time.monotonic() - t0) * 1000, 2)
        trig_data = json.loads(resp_trig.read().decode("utf-8"))
        cycle = trig_data.get("cycle_result", {})
        print(f"  Trigger Status:    {resp_trig.status} OK ({dur_trig} ms)")
        print(f"  Cycle Result:      {cycle.get('status')}")
        print(f"  Cycle ID:          {cycle.get('cycle_id')}")
        print(f"  Consensus Score:   {cycle.get('consensus_score')}")
        print(f"  Omega Sense:       {cycle.get('omega_sense')}")
        assert trig_data["ok"] is True
        assert cycle.get("status") in ("APPROVED_DRY_RUN", "PROMOTED_SUCCESSFULLY")
        assert cycle.get("consensus_score", 0) > 0.66
        print("  -> Phase 3 Passed: Interactive UI diagnostic evolution cycle verified.")

    # 4. Probe HTML Template
    print("\n[Phase 4] Verifying Cockpit UI Template Elements in /mesh/...")
    req_html = urllib.request.Request(f"{BASE_URL}/mesh/")
    with urllib.request.urlopen(req_html) as resp_html:
        html = resp_html.read().decode("utf-8")
        badges = [
            ("stat-val-ast-h", "H_AST Entropy Badge"),
            ("stat-val-omega", "Omega_sense Gate Badge"),
            ("stat-val-atse-daemon", "Daemon Loop 24/7 Badge"),
            ("btn-toggle-atse", "ATSE Cockpit Toggle Button"),
            ("atse-telemetry-modal", "ATSE Telemetry Drawer Modal"),
            ("btn-trigger-atse-cycle", "Run Diagnostic Cycle Action Button"),
        ]
        for elem_id, label in badges:
            found = elem_id in html
            print(f"  Element '{elem_id}' ({label}): {'PRESENT' if found else 'MISSING'}")
            assert found, f"Expected {elem_id} to be present in HTML"
        print("  -> Phase 4 Passed: Full visual cockpit UI verified.")

    print("\n" + "=" * 70)
    print("  ALL VERIFICATION GATES PASSED (Task 06 Complete — ATSE Operational)")
    print("=" * 70)


if __name__ == "__main__":
    test_cockpit_telemetry()
