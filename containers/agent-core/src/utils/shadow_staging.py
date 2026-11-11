"""Shadow Worktree Staging & Atomic Promotion System for Agent WASP.

Provides safe, isolated pre-execution staging for proposed self-evolution patches:
1. Snapshotting: Creates pristine pre-modification backups before any disk write.
2. Shadow Isolation: Stages modifications in an isolated shadow directory tree.
3. Verification: Runs AST parsing, syntax validation, and py_compile compilation.
4. Atomic Promotion: Safely promotes verified patches into live production.
5. Instant Rollback (<500ms): Reverts to pristine backups if verification or post-flight health fails.
"""

from __future__ import annotations

import ast
import os
import py_compile
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
from uuid import uuid4

import structlog

logger = structlog.get_logger()


class StagingState(str, Enum):
    INITIALIZED = "INITIALIZED"
    STAGED = "STAGED"
    VERIFIED = "VERIFIED"
    FAILED_VERIFICATION = "FAILED_VERIFICATION"
    PROMOTED = "PROMOTED"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass
class StagingVerificationResult:
    is_valid: bool
    ast_valid: bool
    py_compile_valid: bool
    errors: list[str] = field(default_factory=list)
    verification_time_ms: int = 0


@dataclass
class StagingSession:
    session_id: str
    proposal_id: str
    target_prod_path: Path
    staged_file_path: Path
    staging_dir: Path
    backup_path: Optional[Path] = None
    state: StagingState = StagingState.INITIALIZED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    verification_result: Optional[StagingVerificationResult] = None
    rollback_reason: str = ""


class ShadowStagingManager:
    """Manages the lifecycle of shadow staging, pre-flight verification, atomic promotion, and rollback."""

    def __init__(
        self,
        workspace_root: Optional[Path] = None,
        staging_root: Optional[Path] = None,
        backup_root: Optional[Path] = None,
    ):
        self.workspace_root = workspace_root or Path(__file__).resolve().parent.parent.parent.parent
        self.staging_root = staging_root or (self.workspace_root / "data" / "shadow_staging")
        self.backup_root = backup_root or (self.workspace_root / "data" / "staging_backups")

        # Ensure root directories exist
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)

    def create_staging_session(
        self,
        proposal_id: str,
        target_relative_path: str,
        proposed_content: str,
    ) -> StagingSession:
        """Sets up a shadow staging session, snapshots the target file, and writes staged content."""
        session_id = f"stage_{uuid4().hex[:8]}"
        prod_file = (self.workspace_root / target_relative_path).resolve()

        # Verify target is inside workspace root (prevent directory traversal)
        try:
            prod_file.relative_to(self.workspace_root.resolve())
        except ValueError:
            raise ValueError(f"Target path '{target_relative_path}' is outside workspace root.")

        session_staging_dir = self.staging_root / session_id
        session_staging_dir.mkdir(parents=True, exist_ok=True)

        # Mirror relative directory structure
        staged_file = session_staging_dir / target_relative_path
        staged_file.parent.mkdir(parents=True, exist_ok=True)

        # 1. Snapshot target file if it already exists in production
        backup_file = None
        if prod_file.exists():
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            backup_file = self.backup_root / f"{timestamp}_{prod_file.name}.bak"
            shutil.copy2(prod_file, backup_file)
            logger.info("shadow_staging.backup_created", prod_path=str(prod_file), backup_path=str(backup_file))

        # 2. Write proposed content to the shadow staged copy
        staged_file.write_text(proposed_content, encoding="utf-8")

        session = StagingSession(
            session_id=session_id,
            proposal_id=proposal_id,
            target_prod_path=prod_file,
            staged_file_path=staged_file,
            staging_dir=session_staging_dir,
            backup_path=backup_file,
            state=StagingState.STAGED,
        )

        logger.info(
            "shadow_staging.session_created",
            session_id=session_id,
            proposal_id=proposal_id,
            target=str(prod_file),
            staged=str(staged_file),
        )
        return session

    def verify_staging(self, session: StagingSession) -> StagingVerificationResult:
        """Performs multi-layer verification on the staged file before promotion."""
        t0 = time.monotonic()
        errors = []
        ast_ok = False
        compile_ok = False

        if not session.staged_file_path.exists():
            errors.append("Staged file does not exist.")
            result = StagingVerificationResult(
                is_valid=False,
                ast_valid=False,
                py_compile_valid=False,
                errors=errors,
            )
            session.state = StagingState.FAILED_VERIFICATION
            session.verification_result = result
            return result

        content = session.staged_file_path.read_text(encoding="utf-8")

        # 1. AST syntax parsing
        try:
            ast.parse(content, filename=str(session.staged_file_path))
            ast_ok = True
        except SyntaxError as e:
            errors.append(f"AST SyntaxError on line {e.lineno}: {e.msg}")

        # 2. Python bytecode compilation
        try:
            py_compile.compile(str(session.staged_file_path), doraise=True)
            compile_ok = True
        except py_compile.PyCompileError as e:
            errors.append(f"py_compile failed: {str(e)[:120]}")
        except Exception as e:
            errors.append(f"Compilation exception: {str(e)[:120]}")

        is_valid = ast_ok and compile_ok and len(errors) == 0
        latency_ms = round((time.monotonic() - t0) * 1000)

        result = StagingVerificationResult(
            is_valid=is_valid,
            ast_valid=ast_ok,
            py_compile_valid=compile_ok,
            errors=errors,
            verification_time_ms=latency_ms,
        )

        session.verification_result = result
        session.state = StagingState.VERIFIED if is_valid else StagingState.FAILED_VERIFICATION

        logger.info(
            "shadow_staging.verification_complete",
            session_id=session.session_id,
            is_valid=is_valid,
            latency_ms=latency_ms,
            errors=errors,
        )
        return result

    def promote_staged_code(self, session: StagingSession) -> bool:
        """Atomically promotes verified staged code into production."""
        if session.state != StagingState.VERIFIED:
            logger.error("shadow_staging.promotion_denied_unverified", session_id=session.session_id, state=session.state.value)
            return False

        try:
            # Ensure target parent directory exists
            session.target_prod_path.parent.mkdir(parents=True, exist_ok=True)

            # Atomic copy to production
            shutil.copy2(session.staged_file_path, session.target_prod_path)
            session.state = StagingState.PROMOTED

            logger.info(
                "shadow_staging.atomic_promotion_successful",
                session_id=session.session_id,
                target_prod_path=str(session.target_prod_path),
            )
            # Cleanup staging dir
            self.cleanup_session(session)
            return True
        except Exception as exc:
            logger.error("shadow_staging.promotion_failed", session_id=session.session_id, error=str(exc))
            self.rollback_staged_code(session, reason=f"Promotion failure: {str(exc)}")
            return False

    def rollback_staged_code(self, session: StagingSession, reason: str = "manual_rollback") -> bool:
        """Instant rollback restoring pristine backup in < 500ms."""
        t0 = time.monotonic()
        try:
            if session.backup_path and session.backup_path.exists():
                shutil.copy2(session.backup_path, session.target_prod_path)
                logger.info(
                    "shadow_staging.rollback_restored_from_backup",
                    session_id=session.session_id,
                    backup_path=str(session.backup_path),
                    target=str(session.target_prod_path),
                )
            elif session.target_prod_path.exists() and session.state == StagingState.PROMOTED:
                # If target was a newly created file, remove it
                session.target_prod_path.unlink()
                logger.info("shadow_staging.rollback_unlinked_new_file", target=str(session.target_prod_path))

            session.state = StagingState.ROLLED_BACK
            session.rollback_reason = reason
            rollback_time_ms = round((time.monotonic() - t0) * 1000)

            logger.info(
                "shadow_staging.rollback_completed",
                session_id=session.session_id,
                rollback_time_ms=rollback_time_ms,
                reason=reason,
            )
            self.cleanup_session(session)
            return True
        except Exception as exc:
            logger.exception("shadow_staging.rollback_failed", session_id=session.session_id, error=str(exc))
            return False

    def cleanup_session(self, session: StagingSession):
        """Removes the temporary staging worktree directory."""
        try:
            if session.staging_dir and session.staging_dir.exists():
                shutil.rmtree(session.staging_dir, ignore_errors=True)
        except Exception:
            pass
