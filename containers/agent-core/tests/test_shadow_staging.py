"""Unit tests for Shadow Worktree Staging & Atomic Promotion (Task 04)."""

import sys
import tempfile
import time
from pathlib import Path

core_dir = Path(__file__).resolve().parent.parent
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

import unittest
from src.utils.shadow_staging import (
    ShadowStagingManager,
    StagingSession,
    StagingState,
)


class TestShadowStaging(unittest.TestCase):
    def setUp(self):
        # Create isolated temporary workspace
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.staging_root = self.workspace / "data" / "shadow_staging"
        self.backup_root = self.workspace / "data" / "staging_backups"

        self.manager = ShadowStagingManager(
            workspace_root=self.workspace,
            staging_root=self.staging_root,
            backup_root=self.backup_root,
        )

        # Create a sample production file
        self.prod_rel_path = "src/modules/worker.py"
        self.prod_file = self.workspace / self.prod_rel_path
        self.prod_file.parent.mkdir(parents=True, exist_ok=True)
        self.initial_code = (
            "def worker_status():\n"
            "    \"\"\"Initial production version.\"\"\"\n"
            "    return {'active': True, 'version': '1.0.0'}\n"
        )
        self.prod_file.write_text(self.initial_code, encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_staging_creation_and_snapshot(self):
        proposed_code = (
            "def worker_status():\n"
            "    \"\"\"Updated version.\"\"\"\n"
            "    return {'active': True, 'version': '2.0.0'}\n"
        )
        session = self.manager.create_staging_session(
            proposal_id="prop-test-01",
            target_relative_path=self.prod_rel_path,
            proposed_content=proposed_code,
        )

        self.assertEqual(session.state, StagingState.STAGED)
        self.assertTrue(session.backup_path.exists())
        self.assertEqual(session.backup_path.read_text(encoding="utf-8"), self.initial_code)
        self.assertTrue(session.staged_file_path.exists())
        self.assertEqual(session.staged_file_path.read_text(encoding="utf-8"), proposed_code)

        # Live production file must be UNTOUCHED
        self.assertEqual(self.prod_file.read_text(encoding="utf-8"), self.initial_code)

    def test_staging_verification_success_and_atomic_promotion(self):
        proposed_code = (
            "def worker_status():\n"
            "    return {'active': True, 'version': '2.0.0'}\n"
        )
        session = self.manager.create_staging_session(
            proposal_id="prop-test-02",
            target_relative_path=self.prod_rel_path,
            proposed_content=proposed_code,
        )

        verification = self.manager.verify_staging(session)
        self.assertTrue(verification.is_valid)
        self.assertTrue(verification.ast_valid)
        self.assertTrue(verification.py_compile_valid)
        self.assertEqual(session.state, StagingState.VERIFIED)

        # Atomic Promotion
        success = self.manager.promote_staged_code(session)
        self.assertTrue(success)
        self.assertEqual(session.state, StagingState.PROMOTED)

        # Production file now has updated content
        self.assertEqual(self.prod_file.read_text(encoding="utf-8"), proposed_code)

    def test_syntax_error_blocked_from_promotion(self):
        broken_code = (
            "def worker_status(:\n"  # Syntax error
            "    return False\n"
        )
        session = self.manager.create_staging_session(
            proposal_id="prop-test-03",
            target_relative_path=self.prod_rel_path,
            proposed_content=broken_code,
        )

        verification = self.manager.verify_staging(session)
        self.assertFalse(verification.is_valid)
        self.assertFalse(verification.ast_valid)
        self.assertEqual(session.state, StagingState.FAILED_VERIFICATION)

        # Attempting promotion must be denied
        promoted = self.manager.promote_staged_code(session)
        self.assertFalse(promoted)

        # Production file remains safe and untouched
        self.assertEqual(self.prod_file.read_text(encoding="utf-8"), self.initial_code)

    def test_instant_rollback_under_500ms(self):
        proposed_code = (
            "def worker_status():\n"
            "    return {'active': True, 'version': '2.0.0'}\n"
        )
        session = self.manager.create_staging_session(
            proposal_id="prop-test-04",
            target_relative_path=self.prod_rel_path,
            proposed_content=proposed_code,
        )

        self.manager.verify_staging(session)
        self.manager.promote_staged_code(session)
        self.assertEqual(self.prod_file.read_text(encoding="utf-8"), proposed_code)

        # Trigger Instant Rollback
        t0 = time.monotonic()
        rollback_ok = self.manager.rollback_staged_code(session, reason="post_flight_check_failed")
        rollback_duration_ms = (time.monotonic() - t0) * 1000

        self.assertTrue(rollback_ok)
        self.assertEqual(session.state, StagingState.ROLLED_BACK)
        self.assertEqual(self.prod_file.read_text(encoding="utf-8"), self.initial_code)
        self.assertLess(rollback_duration_ms, 500, f"Rollback must complete under 500ms, took {rollback_duration_ms}ms")

    def test_path_traversal_protection(self):
        with self.assertRaises(ValueError):
            self.manager.create_staging_session(
                proposal_id="prop-test-05",
                target_relative_path="../../outside_file.py",
                proposed_content="malicious = True\n",
            )


if __name__ == "__main__":
    unittest.main()
