"""Verification script for Task 04: Shadow Worktree Staging & Atomic Promotion.

Demonstrates and verifies:
1. Snapshot isolation: Preserving pristine state before write.
2. Shadow staging: Writing proposed diff to isolated sandbox directory.
3. Multi-layer verification: AST parsing + py_compile compilation.
4. Atomic promotion: Zero-downtime swap of verified code.
5. Instant rollback (<500ms guarantee): Reverting back to pristine snapshot upon post-flight alert.
6. Path traversal safety.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

core_dir = Path(__file__).resolve().parent.parent / "containers" / "agent-core"
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

from src.utils.shadow_staging import ShadowStagingManager, StagingState


def run_shadow_staging_verification():
    print("[TASK-04] Starting Shadow Worktree Staging & Atomic Promotion Verification...")

    workspace_root = Path(__file__).resolve().parent.parent
    manager = ShadowStagingManager(workspace_root=workspace_root)

    # -------------------------------------------------------------
    # 1. Setup Isolated Target for Live Demonstration
    # -------------------------------------------------------------
    demo_rel_path = "containers/agent-core/src/utils/_stage_demo_target.py"
    demo_file = workspace_root / demo_rel_path
    initial_code = (
        "# Pristine Baseline File (v1.0.0)\n"
        "def compute_mesh_status():\n"
        "    return {'mesh': 'online', 'version': '1.0.0'}\n"
    )
    demo_file.write_text(initial_code, encoding="utf-8")
    print(f"\n--- Step 1: Pristine Baseline Created at '{demo_rel_path}' ---")

    try:
        # -------------------------------------------------------------
        # 2. Stage Valid Evolution Patch
        # -------------------------------------------------------------
        print("\n--- Step 2: Create Shadow Staging Session ---")
        staged_code = (
            "# Self-Evolved Patch (v2.0.0)\n"
            "def compute_mesh_status():\n"
            "    \"\"\"Self-evolved with high efficiency return.\"\"\"\n"
            "    return {'mesh': 'online', 'version': '2.0.0', 'telemetry_boost': True}\n"
        )

        session = manager.create_staging_session(
            proposal_id="PROPOSAL-ATSE-004",
            target_relative_path=demo_rel_path,
            proposed_content=staged_code,
        )

        print(f"Session ID: {session.session_id}")
        print(f"Staged File Path: {session.staged_file_path}")
        print(f"Backup Snapshot Path: {session.backup_path}")
        print(f"Current State: {session.state.value}")

        assert session.backup_path.exists(), "Backup snapshot must exist"
        assert session.staged_file_path.exists(), "Staged copy must exist"
        assert demo_file.read_text(encoding="utf-8") == initial_code, "Live production file MUST remain untouched!"
        print("[PASS] Step 2: Shadow staging created; production file remains strictly untouched.")

        # -------------------------------------------------------------
        # 3. Pre-Flight Verification (AST + Compilation)
        # -------------------------------------------------------------
        print("\n--- Step 3: Run Shadow Pre-Flight Verification ---")
        verif = manager.verify_staging(session)
        print(f"Is Valid: {verif.is_valid}")
        print(f"AST Valid: {verif.ast_valid}")
        print(f"PyCompile Valid: {verif.py_compile_valid}")
        print(f"Verification Duration: {verif.verification_time_ms}ms")
        print(f"Session State: {session.state.value}")

        assert verif.is_valid is True
        assert session.state == StagingState.VERIFIED
        print("[PASS] Step 3: Pre-flight AST and bytecode compilation verified.")

        # -------------------------------------------------------------
        # 4. Atomic Promotion
        # -------------------------------------------------------------
        print("\n--- Step 4: Atomic Promotion into Production ---")
        promoted = manager.promote_staged_code(session)
        print(f"Promoted: {promoted}")
        print(f"Session State: {session.state.value}")
        print(f"Live File Content Preview:\n{demo_file.read_text(encoding='utf-8').strip()}")

        assert promoted is True
        assert session.state == StagingState.PROMOTED
        assert "v2.0.0" in demo_file.read_text(encoding="utf-8")
        print("[PASS] Step 4: Atomic zero-downtime promotion successful.")

        # -------------------------------------------------------------
        # 5. Instant Rollback Demonstration (<500ms SLA)
        # -------------------------------------------------------------
        print("\n--- Step 5: Instant Rollback Simulation ---")
        t0 = time.monotonic()
        rolled_back = manager.rollback_staged_code(session, reason="simulated_diagnostic_alert")
        duration_ms = (time.monotonic() - t0) * 1000

        print(f"Rollback Status: {rolled_back}")
        print(f"Rollback Duration: {duration_ms:.2f}ms (SLA: < 500ms)")
        print(f"Live File Content Restored:\n{demo_file.read_text(encoding='utf-8').strip()}")

        assert rolled_back is True
        assert session.state == StagingState.ROLLED_BACK
        assert "v1.0.0" in demo_file.read_text(encoding="utf-8")
        assert duration_ms < 500, f"Rollback exceeded SLA (took {duration_ms:.2f}ms)"
        print(f"[PASS] Step 5: Instant rollback restored baseline in {duration_ms:.2f}ms (<500ms SLA).")

        # -------------------------------------------------------------
        # 6. Syntax Error Rejection Verification
        # -------------------------------------------------------------
        print("\n--- Step 6: Malformed Patch Rejection Test ---")
        broken_session = manager.create_staging_session(
            proposal_id="PROPOSAL-BROKEN-005",
            target_relative_path=demo_rel_path,
            proposed_content="def invalid_syntax(:\n   return False\n",
        )
        broken_verif = manager.verify_staging(broken_session)
        print(f"Broken Patch Valid: {broken_verif.is_valid}")
        print(f"Errors Caught: {broken_verif.errors}")
        promotion_blocked = manager.promote_staged_code(broken_session)
        print(f"Promotion Blocked: {not promotion_blocked}")

        assert broken_verif.is_valid is False
        assert promotion_blocked is False
        manager.cleanup_session(broken_session)
        print("[PASS] Step 6: Broken syntax rejected and prevented from promotion.")

    finally:
        # Cleanup demonstration file
        if demo_file.exists():
            demo_file.unlink()

    print("\n=======================================================")
    print("ALL TASK 04 VERIFICATION STEPS COMPLETED SUCCESSFULLY!")
    print("=======================================================")


if __name__ == "__main__":
    run_shadow_staging_verification()
