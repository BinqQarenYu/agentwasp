"""Self-improve skill — agent can read, write, patch, and rebuild its own source code autonomously.

Actions:
  read(file)                       — Read a source file for analysis
  write(file, content)             — Directly overwrite a source file (auto-backup + persist)
  patch(file, old_text, new_text)  — Surgical text replacement in a file (no full rewrite needed)
  rebuild(file, content)           — Write file then restart agent-core via broker
  install(package)                 — Install a Python package at runtime (no rebuild needed)
  list_files(path)                 — Browse the source tree under /app/src
  diff(file)                       — Show diff between latest backup and current file
  propose(file, change, diff)      — Store a proposal for later review (legacy)
  list                             — List pending proposals
  apply(proposal_id)               — Apply a stored proposal (legacy)
  reject(proposal_id)              — Discard a stored proposal (legacy)

The agent operates with full autonomy — no confirmation gates.
All writes are backed up to BACKUP_ROOT before applying.
All writes are also persisted to PERSIST_ROOT so they survive full container rebuilds.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
from uuid import uuid4

import redis.asyncio as aioredis
import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()

PROPOSALS_KEY = "self_improve:proposals"
SOURCE_ROOT = "/app/src"
BACKUP_ROOT = "/data/self_improve_backups"
PERSIST_ROOT = "/data/src_patches"  # Survives container rebuilds
MAX_FILE_SIZE = 200_000  # 200KB max

# ── CRIT-3: Execution gate for high-blast-radius actions ──────────────────
# install  — pip installs persist to the live process; no restart needed
# rebuild  — writes source + triggers container restart
# These actions cannot execute on LLM decision alone.  They require:
#   1. Explicit confirm=True argument in the skill call  (per-step, non-inheritable)
#   2. A Redis daily cap (hard block, not warn-only)
#   3. Phase 6.3: Per-turn anti-replay slot (one use per action-type per 60s window)
_HIGH_RISK_ACTIONS = frozenset({"install", "rebuild"})
_DAILY_CAP_KEY_PREFIX = "self_improve:daily_cap:"
# CRIT-3 gate order: _slot_key (anti-replay) is checked BEFORE _DAILY_CAP_MAX (rate limit)
_DAILY_CAP_MAX = 3   # max 3 high-risk actions per calendar day (UTC)

# Phase 6.3 — Per-turn anti-replay slot
# Prevents same-turn multi-step bypass: a single confirm='true' cannot authorize
# two executions of the same high-risk action within the same 60-second window.
# Each call consumes one slot; a second call of the same action type in the
# same window is blocked regardless of whether it also supplies confirm='true'.
#
# Key schema: self_improve:confirm_slot:<action>:<utc_60s_bucket>
# TTL: 60 seconds (auto-expires at window boundary)
_CONFIRM_SLOT_PREFIX = "self_improve:confirm_slot:"
_CONFIRM_SLOT_TTL    = 60   # seconds — approximates one execution turn

# ── Soft Safety Gate for write / patch actions ────────────────────────────────
# write and patch are not in _HIGH_RISK_ACTIONS (they don't restart the container)
# but they can silently overwrite security-critical source files.  This gate adds
# a lightweight deterministic check before any file-modifying action executes.
# It does NOT require user confirmation — it is a system-internal layer only.

# Files whose modification warrants extra scrutiny.
_CRITICAL_SOURCE_PATHS: frozenset[str] = frozenset({
    "sandbox.py",
    "python_exec.py",
    "control_layer.py",
    "response_grounder.py",
    "self_improve.py",
    "behavioral_learner.py",
    "behavioral.py",
    "redaction.py",
    "security",
    "guard",
    "policy",
    "domain_lock",
})

# Patch content patterns that indicate safety-weakening intent.
_SAFETY_WEAKENING_RE = re.compile(
    r"disable\s+(?:validation|sandbox|guard|check|confirmation)"
    r"|skip\s+(?:confirmation|guard|validation|check)"
    r"|bypass\s+(?:guard|domain\s*lock|sandbox|security|validation)"
    r"|ignore\s+domain\s*lock"
    r"|remove\s+sandbox"
    r"|trust\s+llm\s+output"
    r"|allow\s+unrestricted\s+execution"
    r"|no\s+(?:confirmation|guard|sandbox|validation)\s+(?:needed|required)"
    r"|_HIGH_RISK_ACTIONS\s*=\s*frozenset\(\s*\)"   # clear the gate set
    r"|confirm\s*=\s*['\"]?true['\"]?.*always"       # always-confirm bypass
    r"|return\s+True\s*#.*bypass"                    # commented bypass
    r"|pass\s*#.*guard",                             # commented guard removal
    re.IGNORECASE,
)

# Gate decision constants
_GATE_ALLOW          = "allow"
_GATE_WARN           = "allow_with_warning"
_GATE_BLOCK          = "block"

# Write actions covered by the soft gate
_WRITE_ACTIONS: frozenset[str] = frozenset({"write", "patch", "apply_patch"})


def _is_high_risk_write_action(action: str, target_path: str, patch_text: str) -> bool:
    """Return True when a write/patch action touches a critical security path."""
    if action not in _WRITE_ACTIONS:
        return False
    path_lower = target_path.lower()
    return any(critical in path_lower for critical in _CRITICAL_SOURCE_PATHS)


def _self_improve_soft_gate(action: str, target_path: str, patch_text: str) -> tuple[str, str]:
    """Evaluate a write/patch action and return (decision, reason).

    decision: _GATE_ALLOW | _GATE_WARN | _GATE_BLOCK
    reason:   human-readable explanation

    Rules:
      - Non-write actions → allow (gate only covers write/patch)
      - Safety-weakening patterns in patch_text → block (regardless of path)
      - Critical path + large/dense patch + weakening → block (escalation)
      - Critical path + large/dense patch (no weakening) → warn
      - Critical path touched → allow_with_warning
      - Everything else → allow
    """
    if action not in _WRITE_ACTIONS:
        return _GATE_ALLOW, "non-write action"

    # ── Diff-awareness signals ────────────────────────────────────────────────
    patch_length   = len(patch_text or "")
    line_count     = (patch_text or "").count("\n") + 1
    avg_line_length = patch_length / max(line_count, 1)
    is_large_patch  = patch_length > 2000
    is_dense_patch  = avg_line_length > 120
    matches_weakening = bool(_SAFETY_WEAKENING_RE.search(patch_text or ""))
    is_critical    = _is_high_risk_write_action(action, target_path, patch_text)

    logger.info(
        "self_improve.soft_gate_analysis",
        patch_length=patch_length,
        line_count=line_count,
        is_large_patch=is_large_patch,
        is_dense_patch=is_dense_patch,
        critical_path=is_critical,
        weakening_detected=matches_weakening,
    )

    # Escalation: critical path + high-impact modification + weakening language → block
    if is_critical and (is_large_patch or is_dense_patch) and matches_weakening:
        return _GATE_BLOCK, "high_impact_safety_modification"

    # Safety-weakening language always blocks, regardless of path or size
    if matches_weakening:
        return _GATE_BLOCK, "patch content matches safety-weakening pattern"

    # Escalation: critical path + high-impact modification (no weakening) → warn
    if is_critical and (is_large_patch or is_dense_patch):
        return _GATE_WARN, "large_modification_critical_path"

    if is_critical:
        return _GATE_WARN, f"critical security path: {os.path.basename(target_path)}"

    return _GATE_ALLOW, "non-critical write"


# FIX 3: In-memory fallback cap — used when Redis is unavailable.
# Enforces _DAILY_CAP_MAX per UTC calendar day and per-turn anti-replay
# as a process-local best-effort guard (not a substitute for Redis).
_mem_cap_lock  = threading.Lock()
_mem_cap:   dict[str, int] = {}    # { "YYYYMMDD": count }
_mem_slots: set[str]       = set() # slot keys (per-turn anti-replay)


def _sha256_file(path: str) -> str:
    """Return hex SHA-256 digest of a file's contents."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


_PATCH_MAX_FAILURES = 3   # Skip patches that have failed this many times


def _patch_failure_count(patch_file: str) -> int:
    """Read the failure counter for a patch file. Returns 0 if sidecar absent."""
    sidecar = patch_file + ".failures"
    try:
        return int(open(sidecar).read().strip())
    except Exception:
        return 0


def _patch_increment_failure(patch_file: str) -> int:
    """Increment and persist the failure counter for a patch file. Returns new count."""
    count = _patch_failure_count(patch_file) + 1
    try:
        with open(patch_file + ".failures", "w") as f:
            f.write(str(count))
    except Exception:
        pass
    return count


def _patch_validate_python(patch_file: str) -> tuple[bool, str]:
    """Validate Python syntax of a patch file before applying.

    Returns (valid: bool, error_message: str).
    Non-Python files are always considered valid.
    """
    if not patch_file.endswith(".py"):
        return True, ""
    import py_compile
    import tempfile
    try:
        py_compile.compile(patch_file, doraise=True)
        return True, ""
    except py_compile.PyCompileError as e:
        return False, str(e)[:200]


def _dry_run_validate_content(file_path: str, new_content: str) -> tuple[bool, str]:
    """Dry-run validation: parse new content BEFORE writing it to disk.

    Catches syntax errors before they reach /app/src so the running process
    never sees a broken module.  Non-Python files always pass.

    Returns (valid: bool, error_message: str).
    """
    if not file_path.endswith(".py"):
        return True, ""
    import ast
    try:
        ast.parse(new_content)
        return True, ""
    except SyntaxError as e:
        return False, f"Syntax error at line {e.lineno}: {e.msg}"
    except Exception as e:
        return False, f"Parse failure: {str(e)[:160]}"


def _estimate_patch_confidence(action: str, file_path: str, patch_text: str, old_text: str = "") -> tuple[float, str]:
    """Heuristic confidence score for a proposed patch.

    Higher = safer to apply automatically.  Lower = more risk.
    Returns (score: 0.0-1.0, rationale: str).

    Signals:
    - Critical-path target → confidence drops
    - Very large patch / very dense lines → confidence drops
    - Patch action with empty old_text on critical file → confidence drops
    - Targeted patch (small old_text + small new_text) on non-critical → confidence rises
    """
    if action not in _WRITE_ACTIONS:
        return 1.0, "non-write action"

    path_lower = (file_path or "").lower()
    is_critical = any(c in path_lower for c in _CRITICAL_SOURCE_PATHS)
    patch_len = len(patch_text or "")
    line_count = (patch_text or "").count("\n") + 1
    avg_line = patch_len / max(line_count, 1)

    score = 0.85  # baseline
    notes: list[str] = []

    if is_critical:
        score -= 0.25
        notes.append("critical path")
    if patch_len > 3000:
        score -= 0.20
        notes.append("very large patch")
    elif patch_len > 1500:
        score -= 0.10
        notes.append("large patch")
    if avg_line > 140:
        score -= 0.10
        notes.append("dense lines")
    if action == "patch" and old_text and len(old_text) < 20:
        score -= 0.10
        notes.append("very short anchor")
    if action == "write" and is_critical:
        score -= 0.15
        notes.append("full-file overwrite on critical path")

    score = max(0.0, min(1.0, score))
    return score, "; ".join(notes) or "ok"


# ── Post-write rollback monitor ──────────────────────────────────────────────
# After a successful write/patch, schedule a delayed health check.  If errors
# spike sharply within the watch window, restore the most recent backup.
# Fail-open: any failure is logged but never blocks normal operation.

_ROLLBACK_WATCH_DELAY_S    = 90    # check 90s after the write
_ROLLBACK_ERROR_SPIKE      = 5     # post-write errors above baseline → revert


async def _sample_error_count() -> int:
    """Sample audit_log error count in the last 5 minutes. Returns 0 on any failure."""
    try:
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select, func, and_
        from ...db.models import AuditLog as _AL
        from ...db.session import async_session as _asess
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        async with _asess() as _s:
            res = await _s.execute(
                select(func.count(_AL.id)).where(
                    and_(_AL.created_at >= cutoff, _AL.success == False)  # noqa: E712
                )
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _delayed_rollback_check(file_path: str, full_path: str, backup_path: str,
                                   baseline_errors: int, redis_url: str) -> None:
    """Wait, then compare current error count to baseline.  If spike → revert.

    Fires once per write; never blocks the calling coroutine.
    """
    try:
        await asyncio.sleep(_ROLLBACK_WATCH_DELAY_S)
        post_errors = await _sample_error_count()
        delta = post_errors - baseline_errors
        if delta < _ROLLBACK_ERROR_SPIKE:
            logger.info(
                "self_improve.rollback_check_clean",
                file=file_path,
                baseline=baseline_errors,
                post=post_errors,
                delta=delta,
            )
            return
        # Spike detected — revert
        if not (backup_path and os.path.isfile(backup_path)):
            logger.warning("self_improve.rollback_no_backup", file=file_path)
            return
        try:
            shutil.copy2(backup_path, full_path)
            # Also revert the persisted copy to keep startup-replay consistent
            persist_path = os.path.join(PERSIST_ROOT, file_path.lstrip("/"))
            if os.path.isfile(persist_path):
                shutil.copy2(backup_path, persist_path)
                try:
                    digest = _sha256_file(persist_path)
                    with open(persist_path + ".sha256", "w") as sf:
                        sf.write(digest)
                except Exception:
                    pass
            logger.warning(
                "self_improve.auto_rolled_back",
                file=file_path,
                baseline=baseline_errors,
                post=post_errors,
                delta=delta,
                from_backup=backup_path,
            )
            # Append to change log so operator sees it
            try:
                import redis as _redis
                from datetime import datetime as _dt, timezone as _tz
                _r = _redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2)
                try:
                    entry = json.dumps({
                        "ts": _dt.now(_tz.utc).isoformat(),
                        "file": file_path,
                        "event": "auto_rollback",
                        "baseline_errors": baseline_errors,
                        "post_errors": post_errors,
                    })
                    _r.lpush("self_improve:change_log", entry)
                    _r.ltrim("self_improve:change_log", 0, 99)
                finally:
                    try:
                        _r.close()
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception as _re:
            logger.warning("self_improve.rollback_failed", file=file_path, error=str(_re)[:120])
    except Exception as _e:
        logger.warning("self_improve.rollback_check_error", error=str(_e)[:120])


def apply_persisted_patches(source_root: str = SOURCE_ROOT, patch_dir: str = PERSIST_ROOT) -> int:
    """Apply all persisted file patches from /data/src_patches to /app/src.

    Called at startup to re-apply agent self-modifications after a container rebuild.
    Returns the number of patches applied.

    Safety checks (all fail-open — never crash startup):
    1. SHA-256 integrity: If a <file>.sha256 sidecar exists, digest must match.
    2. Python syntax: .py patches are compiled before apply; syntax errors → skip + record failure.
    3. Failure cap: Patches that have failed _PATCH_MAX_FAILURES times are skipped permanently.
    """
    import structlog as _sl
    _log = _sl.get_logger()

    if not os.path.isdir(patch_dir):
        return 0
    applied = 0
    skipped_integrity = 0
    skipped_syntax = 0
    skipped_failures = 0

    for root, dirs, files in os.walk(patch_dir):
        dirs[:] = [d for d in sorted(dirs) if d not in ("__pycache__", ".git")]
        for fname in files:
            # Skip sidecar files — they are not patches themselves
            if fname.endswith((".sha256", ".failures")):
                continue
            patch_file = os.path.join(root, fname)
            rel_path = os.path.relpath(patch_file, patch_dir)
            target = os.path.join(source_root, rel_path)

            try:
                # Guard 1: failure cap — skip patches that have crashed N times
                failures = _patch_failure_count(patch_file)
                if failures >= _PATCH_MAX_FAILURES:
                    skipped_failures += 1
                    _log.warning(
                        "self_improve.patch_skipped_max_failures",
                        file=rel_path,
                        failures=failures,
                        cap=_PATCH_MAX_FAILURES,
                    )
                    continue

                # Guard 2: SHA-256 integrity sidecar
                sidecar = patch_file + ".sha256"
                if os.path.isfile(sidecar):
                    try:
                        expected = open(sidecar).read().strip()
                        actual = _sha256_file(patch_file)
                        if actual != expected:
                            skipped_integrity += 1
                            _patch_increment_failure(patch_file)
                            _log.warning(
                                "self_improve.patch_integrity_failed",
                                file=rel_path,
                                expected=expected[:16] + "…",
                                actual=actual[:16] + "…",
                            )
                            continue  # skip tampered patch — fail-open
                    except Exception as chk_err:
                        _log.warning("self_improve.patch_checksum_read_failed",
                                     file=rel_path, error=str(chk_err))
                else:
                    _log.info("self_improve.patch_no_checksum", file=rel_path,
                              note="legacy patch — applying without integrity check")

                # Guard 3: Python syntax check
                valid, syntax_err = _patch_validate_python(patch_file)
                if not valid:
                    skipped_syntax += 1
                    new_failures = _patch_increment_failure(patch_file)
                    _log.warning(
                        "self_improve.patch_syntax_invalid",
                        file=rel_path,
                        error=syntax_err,
                        failure_count=new_failures,
                        cap=_PATCH_MAX_FAILURES,
                    )
                    continue

                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(patch_file, target)
                applied += 1
                _log.info("self_improve.patch_applied", file=rel_path)

                # Trust gap fix: append patch event to a Redis change log so the
                # operator can see what was applied at boot.  Bounded list (last
                # 100 events).  Best-effort — never blocks startup.
                try:
                    import redis
                    import os as _os
                    redis_url = _os.environ.get("REDIS_URL") or "redis://agent-redis:6379/0"
                    _r = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2)
                    try:
                        sha = _sha256_file(target)
                        from datetime import datetime as _dt, timezone as _tz
                        entry = json.dumps({
                            "ts": _dt.now(_tz.utc).isoformat(),
                            "file": rel_path,
                            "target": target,
                            "sha256": sha[:16],
                            "size": _os.path.getsize(target),
                            "event": "patch_applied_at_boot",
                        })
                        _r.lpush("self_improve:change_log", entry)
                        _r.ltrim("self_improve:change_log", 0, 99)
                    finally:
                        try:
                            _r.close()
                        except Exception:
                            pass
                except Exception:
                    pass

            except Exception as e:
                _patch_increment_failure(patch_file)
                _log.warning("self_improve.patch_apply_failed", file=rel_path, error=str(e))

    _log.info(
        "self_improve.apply_persisted_patches_done",
        applied=applied,
        skipped_integrity=skipped_integrity,
        skipped_syntax=skipped_syntax,
        skipped_max_failures=skipped_failures,
    )
    return applied


class SelfImproveSkill(SkillBase):
    def __init__(self, redis_url: str = "redis://agent-redis:6379/0", broker_client=None):
        self.redis_url = redis_url
        self._broker = broker_client

    def set_broker(self, broker_client) -> None:
        """Inject broker client for auto-restart capability."""
        self._broker = broker_client

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="self_improve",
            description=(
                "Read, write, patch, and rebuild the agent's own source code autonomously. "
                "Actions: read(file), write(file, content), patch(file, old_text, new_text), "
                "rebuild(file, content), install(package), list_files(path), diff(file), "
                "propose(file, change, diff), list, apply(proposal_id), reject(proposal_id). "
                "Use write() to directly overwrite source files (full rewrite). "
                "Use patch() for surgical text replacements without rewriting the whole file. "
                "Use rebuild() to modify + auto-restart. "
                "Use install() to add new Python packages at runtime without rebuilding. "
                "Use list_files() to explore /app/src. "
                "All writes auto-persist to /data/src_patches so they survive container rebuilds. "
                "No confirmation required — operates with full autonomy."
            ),
            params=[
                SkillParam(
                    name="action",
                    param_type=ParamType.STRING,
                    description=(
                        "Action: read, write, patch, rebuild, install, list_files, diff, "
                        "propose, list, apply, reject"
                    ),
                ),
                SkillParam(
                    name="file",
                    param_type=ParamType.STRING,
                    description=(
                        "Path to source file (relative to /app/src, "
                        "e.g. 'skills/builtin/browser.py')"
                    ),
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="content",
                    param_type=ParamType.STRING,
                    description="New file content to write (for write/rebuild actions)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="old_text",
                    param_type=ParamType.STRING,
                    description="Exact text to find and replace (for patch action)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="new_text",
                    param_type=ParamType.STRING,
                    description="Replacement text (for patch action)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="package",
                    param_type=ParamType.STRING,
                    description="Package name (and optional version) to install (for install action, e.g. 'httpx>=0.27')",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="change",
                    param_type=ParamType.STRING,
                    description="Description of the proposed change (for propose)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="diff",
                    param_type=ParamType.STRING,
                    description="New file content or unified diff (for propose/apply)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="proposal_id",
                    param_type=ParamType.STRING,
                    description="ID of a pending proposal (for apply/reject)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="path",
                    param_type=ParamType.STRING,
                    description="Sub-path under /app/src to list (for list_files, default='')",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="confirm",
                    param_type=ParamType.STRING,
                    description=(
                        "REQUIRED for high-risk actions (install, rebuild). "
                        "Set to 'true' to confirm you understand the risk and authorize execution."
                    ),
                    required=False,
                    default="",
                ),
            ],
            category="system",
            requires_confirmation=False,
            timeout_seconds=120.0,
            capability_level="privileged",
        )

    async def execute(
        self,
        action: str = "list",
        file: str = "",
        content: str = "",
        old_text: str = "",
        new_text: str = "",
        package: str = "",
        change: str = "",
        diff: str = "",
        proposal_id: str = "",
        path: str = "",
        confirm: str = "",          # CRIT-3: must be "true" for high-risk actions
        human_approved: str = "",   # required=true for protected-path patches
        dry_run: str = "",          # if "true": preview write/patch as a unified diff, do NOT apply
        **kwargs,
    ) -> SkillResult:
        action = action.lower().strip()
        _dry_run = str(dry_run).lower().strip() in ("true", "1", "yes")

        # ── Protected-path approval gate ─────────────────────────────────
        # The agent CAN read protected files freely. Modifying them requires
        # explicit human approval — these are the load-bearing trust files
        # whose edits could weaken the central policy.
        _PROTECTED_PATH_PREFIXES = (
            "src/policy/", "src/policy",
            "src/events/handlers.py",
            "src/agent/context.py",
            "src/goal_orchestrator/executor.py",
        )
        if action in ("write", "patch", "rebuild") and file:
            _normalized = file.lstrip("./").replace("\\", "/")
            for _prefix in _PROTECTED_PATH_PREFIXES:
                if _normalized.startswith(_prefix):
                    _approved = str(human_approved).lower().strip() in ("true", "1", "yes")
                    if not _approved:
                        logger.warning(
                            "self_improve.protected_path_blocked",
                            action=action, file=file, path_prefix=_prefix,
                        )
                        return SkillResult(
                            skill_name="self_improve",
                            success=False,
                            output="",
                            error=(
                                f"⛔ Patch to protected path '{file}' requires explicit human "
                                f"approval. Call again with human_approved='true' to authorize. "
                                f"Protected paths hold the central trust policy — edits without "
                                f"review can weaken safety invariants."
                            ),
                        )
                    break

        # ── Self-repair audit log (every write/patch logged with diff) ────
        # Writes to Redis self_repair:audit (capped, JSON list) so operators
        # can review what the agent has been changing on its own code.
        if action in ("write", "patch", "rebuild") and file:
            try:
                import json as _json_audit
                import time as _t_audit
                import redis as _redis_sync_audit  # type: ignore
                from ...config import settings as _s_audit
                _entry = {
                    "ts": _t_audit.time(),
                    "action": action,
                    "file": file,
                    "old_text_len": len(old_text or ""),
                    "new_text_len": len(new_text or ""),
                    "content_len": len(content or ""),
                    "old_text_preview": (old_text or "")[:120],
                    "new_text_preview": (new_text or "")[:120],
                    "human_approved": str(human_approved).lower() in ("true", "1", "yes"),
                    "is_protected": any(
                        file.lstrip("./").replace("\\", "/").startswith(p)
                        for p in _PROTECTED_PATH_PREFIXES
                    ),
                }
                _r_audit = _redis_sync_audit.from_url(
                    _s_audit.redis_url, decode_responses=True, socket_connect_timeout=1,
                )
                try:
                    _r_audit.lpush("self_repair:audit", _json_audit.dumps(_entry))
                    _r_audit.ltrim("self_repair:audit", 0, 499)
                finally:
                    try: _r_audit.close()
                    except Exception: pass
            except Exception:
                pass

        # ── CRIT-3: Hard gate for high-blast-radius actions ───────────────────
        if action in _HIGH_RISK_ACTIONS:
            _confirmed = str(confirm).lower().strip() in ("true", "1", "yes")
            if not _confirmed:
                return SkillResult(
                    skill_name="self_improve",
                    success=False,
                    output="",
                    error=(
                        f"⛔ High-risk action '{action}' requires explicit confirmation. "
                        f"Call again with confirm='true' to authorize. "
                        f"Example: self_improve(action='{action}', "
                        + (f"package='{package}'" if action == "install" else f"file='{file}', content=...")
                        + f", confirm='true')"
                    ),
                )
            # Hard daily cap + Phase 6.3 per-turn anti-replay slot (Redis)
            # Slot key first — slot check must precede daily-cap check
            _bucket   = int(time.time() // _CONFIRM_SLOT_TTL)
            _slot_key = f"{_CONFIRM_SLOT_PREFIX}{action}:{_bucket}"
            _today    = time.strftime("%Y%m%d", time.gmtime())
            _cap_key  = f"{_DAILY_CAP_KEY_PREFIX}{_today}"
            try:
                _r = aioredis.from_url(self.redis_url, decode_responses=True)
                try:
                    # Phase 6.3: Per-turn anti-replay — consume one-time slot
                    # set(nx=True) returns True if the key was newly created (slot free),
                    # False/None if key already existed (slot consumed this window).
                    _slot_free = await _r.set(_slot_key, "1", nx=True, ex=_CONFIRM_SLOT_TTL)
                    if not _slot_free:
                        logger.warning(
                            "self_improve.confirm_slot_consumed",
                            action=action,
                            slot_key=_slot_key,
                        )
                        return SkillResult(
                            skill_name="self_improve",
                            success=False,
                            output="",
                            error=(
                                f"⛔ Confirmation for '{action}' already consumed in this turn window. "
                                "Each high-risk action requires an independent, explicit confirmation. "
                                "Confirmations are NOT reusable across steps in the same turn. "
                                "Wait for the next turn window or submit actions in separate turns."
                            ),
                        )

                    # Daily cap check
                    _count = await _r.incr(_cap_key)
                    await _r.expire(_cap_key, 86400)  # TTL = 24h
                    if _count > _DAILY_CAP_MAX:
                        await _r.decr(_cap_key)
                        # Also release the slot so it can be retried tomorrow
                        await _r.delete(_slot_key)
                        return SkillResult(
                            skill_name="self_improve",
                            success=False,
                            output="",
                            error=(
                                f"⛔ Daily limit for high-risk self_improve actions reached "
                                f"({_DAILY_CAP_MAX}/day). Action '{action}' blocked. "
                                f"Limit resets at midnight UTC."
                            ),
                        )
                finally:
                    await _r.aclose()
            except Exception as _gate_err:
                # Redis unavailable — use in-memory fallback cap (best effort)
                logger.warning(
                    "self_improve.gate_redis_unavailable",
                    action=action,
                    error=str(_gate_err)[:80],
                )
                _today_fb = time.strftime("%Y%m%d", time.gmtime())
                _slot_key_fb = f"{_CONFIRM_SLOT_PREFIX}{action}:{int(time.time() // _CONFIRM_SLOT_TTL)}"
                with _mem_cap_lock:
                    # Per-turn anti-replay (in-memory slot)
                    if _slot_key_fb in _mem_slots:
                        logger.warning(
                            "self_improve.cap_fallback_used",
                            action=action,
                            reason="slot_consumed",
                            blocked=True,
                        )
                        return SkillResult(
                            skill_name="self_improve",
                            success=False,
                            output="",
                            error=(
                                f"⛔ Confirmation for '{action}' already consumed (in-memory fallback). "
                                "Wait for the next turn window."
                            ),
                        )
                    # Daily cap (in-memory)
                    _mem_count = _mem_cap.get(_today_fb, 0) + 1
                    if _mem_count > _DAILY_CAP_MAX:
                        logger.warning(
                            "self_improve.cap_fallback_used",
                            action=action,
                            reason="daily_cap_exceeded",
                            mem_count=_mem_count,
                            cap=_DAILY_CAP_MAX,
                            blocked=True,
                        )
                        return SkillResult(
                            skill_name="self_improve",
                            success=False,
                            output="",
                            error=(
                                f"⛔ Daily limit reached (in-memory fallback, {_DAILY_CAP_MAX}/day). "
                                f"Action '{action}' blocked. Resets at midnight UTC."
                            ),
                        )
                    # Consume slot + increment cap
                    _mem_slots.add(_slot_key_fb)
                    _mem_cap[_today_fb] = _mem_count
                logger.warning(
                    "self_improve.cap_fallback_used",
                    action=action,
                    reason="redis_unavailable",
                    mem_count=_mem_count,
                    cap=_DAILY_CAP_MAX,
                    blocked=False,
                )
                # Allow execution to proceed under in-memory protection
                # (slot + daily cap enforced above; _count unused in this path)
                _count = _mem_count
            logger.info(
                "self_improve.high_risk_authorized",
                action=action,
                daily_count=_count,
                cap=_DAILY_CAP_MAX,
            )
        # ── End CRIT-3 gate ───────────────────────────────────────────────────

        # ── Soft Safety Gate for write / patch ───────────────────────────────
        # Deterministic, pattern-based check — no user interaction required.
        if action in _WRITE_ACTIONS:
            _patch_text_for_gate = (content or diff or new_text or "")
            _gate_decision, _gate_reason = _self_improve_soft_gate(
                action, file, _patch_text_for_gate
            )
            _is_critical     = _is_high_risk_write_action(action, file, _patch_text_for_gate)
            _patch_len       = len(_patch_text_for_gate)
            _line_count      = _patch_text_for_gate.count("\n") + 1
            _is_large        = _patch_len > 2000
            _is_dense        = (_patch_len / max(_line_count, 1)) > 120

            # ── Pre-write Python dry-run validation ──────────────────────────
            # For write/rebuild we have full content; for patch we synthesize
            # the post-patch content by running the replacement in memory.
            _dry_content: str | None = None
            if action == "write" and content:
                _dry_content = content
            elif action == "patch" and old_text and new_text:
                try:
                    _full_path_dry, _err_dry = self._resolve_and_validate(file)
                    if not _err_dry and os.path.isfile(_full_path_dry):
                        with open(_full_path_dry, "r", encoding="utf-8") as _fdr:
                            _orig_dry = _fdr.read()
                        if old_text in _orig_dry:
                            _dry_content = _orig_dry.replace(old_text, new_text, 1)
                except Exception:
                    _dry_content = None

            if _dry_content is not None:
                _dry_ok, _dry_err = _dry_run_validate_content(file, _dry_content)
                if not _dry_ok:
                    logger.warning(
                        "self_improve.dry_run_failed",
                        action=action, file=file, error=_dry_err,
                    )
                    return SkillResult(
                        skill_name="self_improve",
                        success=False,
                        output="",
                        error=(
                            f"⛔ Dry-run validation failed: {_dry_err}. "
                            "The patch would produce a file that does not parse — refusing to write."
                        ),
                    )

            # ── Confidence threshold for critical-path writes ────────────────
            _conf_score, _conf_notes = _estimate_patch_confidence(
                action, file, _patch_text_for_gate, old_text or ""
            )
            if _is_critical and _conf_score < 0.4:
                logger.warning(
                    "self_improve.low_confidence_block",
                    action=action, file=file,
                    confidence=round(_conf_score, 2), notes=_conf_notes,
                )
                return SkillResult(
                    skill_name="self_improve",
                    success=False,
                    output="",
                    error=(
                        f"⛔ Patch rejected: confidence {_conf_score:.2f} too low for critical "
                        f"path ({_conf_notes}). Narrow the change scope and try again."
                    ),
                )

            if _gate_decision == _GATE_BLOCK:
                logger.warning(
                    "self_improve.soft_gate_blocked",
                    action=action,
                    target_path=file,
                    reason=_gate_reason,
                    critical_path=_is_critical,
                    patch_length=_patch_len,
                    line_count=_line_count,
                    is_large_patch=_is_large,
                    is_dense_patch=_is_dense,
                )
                return SkillResult(
                    skill_name="self_improve",
                    success=False,
                    output="",
                    error=(
                        f"⛔ Write blocked by safety gate: {_gate_reason}. "
                        "The patch content matches a safety-weakening pattern. "
                        "If this is a legitimate hardening change, review the patch "
                        "manually and apply it outside the agent skill."
                    ),
                )
            elif _gate_decision == _GATE_WARN:
                logger.warning(
                    "self_improve.soft_gate_warned",
                    action=action,
                    target_path=file,
                    reason=_gate_reason,
                    critical_path=_is_critical,
                    patch_length=_patch_len,
                    line_count=_line_count,
                    is_large_patch=_is_large,
                    is_dense_patch=_is_dense,
                )
                # Allow — but the warning is persisted to logs for auditability
            else:
                logger.info(
                    "self_improve.soft_gate_allowed",
                    action=action,
                    target_path=file,
                    reason=_gate_reason,
                    critical_path=_is_critical,
                    patch_length=_patch_len,
                    line_count=_line_count,
                    is_large_patch=_is_large,
                    is_dense_patch=_is_dense,
                )
        # ── End Soft Safety Gate ──────────────────────────────────────────────

        try:
            if action == "read":
                return await self._read_file(file)

            elif action == "write":
                if _dry_run:
                    return self._dry_run_write(file, content or diff)
                return await self._write_file(file, content or diff)

            elif action == "patch":
                if _dry_run:
                    return self._dry_run_patch(file, old_text, new_text)
                return await self._patch_file(file, old_text, new_text)

            elif action == "rebuild":
                return await self._rebuild(file, content or diff)

            elif action == "install":
                return await self._install_package(package)

            elif action == "list_files":
                return self._list_files(path)

            elif action == "diff":
                return self._show_diff(file)

            elif action == "propose":
                r = aioredis.from_url(self.redis_url, decode_responses=True)
                try:
                    return await self._propose(r, file, change, diff or content)
                finally:
                    await r.aclose()

            elif action == "list":
                r = aioredis.from_url(self.redis_url, decode_responses=True)
                try:
                    return await self._list_proposals(r)
                finally:
                    await r.aclose()

            elif action == "apply":
                r = aioredis.from_url(self.redis_url, decode_responses=True)
                try:
                    return await self._apply(r, proposal_id)
                finally:
                    await r.aclose()

            elif action == "reject":
                r = aioredis.from_url(self.redis_url, decode_responses=True)
                try:
                    return await self._reject(r, proposal_id)
                finally:
                    await r.aclose()

            else:
                return SkillResult(
                    skill_name="self_improve",
                    success=False,
                    output="",
                    error=(
                        f"Unknown action: {action}. "
                        "Use: read, write, patch, rebuild, install, list_files, diff, "
                        "propose, list, apply, reject"
                    ),
                )
        except Exception as e:
            logger.exception("self_improve.error", action=action)
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error=str(e),
            )

    # ------------------------------------------------------------------
    # Core: read / write / patch / rebuild / install
    # ------------------------------------------------------------------

    async def _read_file(self, file_path: str) -> SkillResult:
        if not file_path:
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error=(
                    "Provide a file path relative to /app/src "
                    "(e.g. 'skills/builtin/browser.py')"
                ),
            )
        full_path = os.path.realpath(os.path.join(SOURCE_ROOT, file_path.lstrip("/")))
        source_root_real = os.path.realpath(SOURCE_ROOT)
        if not (full_path.startswith(source_root_real + os.sep) or full_path == source_root_real):
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error="Access denied: path outside source root",
            )
        if not os.path.exists(full_path):
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error=f"File not found: {full_path}",
            )
        size = os.path.getsize(full_path)
        if size > MAX_FILE_SIZE:
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error=f"File too large ({size} bytes). Max {MAX_FILE_SIZE} bytes.",
            )
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
        return SkillResult(
            skill_name="self_improve",
            success=True,
            output=f"File: {full_path} ({len(content)} chars)\n\n{content}",
        )

    def _resolve_and_validate(self, file_path: str) -> tuple[str, str | None]:
        """Return (full_path, error_msg). error_msg is None if valid."""
        if not file_path:
            return "", "Provide file path (relative to /app/src)"
        full_path = os.path.realpath(os.path.join(SOURCE_ROOT, file_path.lstrip("/")))
        source_root_real = os.path.realpath(SOURCE_ROOT)
        if not (full_path.startswith(source_root_real + os.sep) or full_path == source_root_real):
            return full_path, f"Path escape attempt blocked: {file_path}"
        return full_path, None

    def _backup_file(self, full_path: str, file_path: str) -> str | None:
        """Create a timestamped backup. Returns backup path or None."""
        if not os.path.exists(full_path):
            return None
        os.makedirs(BACKUP_ROOT, exist_ok=True)
        ts = int(time.time())
        safe_name = file_path.lstrip("/").replace("/", "_")
        backup_path = os.path.join(BACKUP_ROOT, f"{safe_name}.{ts}.bak")
        shutil.copy2(full_path, backup_path)
        logger.info("self_improve.backed_up", src=full_path, dst=backup_path)
        return backup_path

    async def _schedule_rollback_check(self, file_path: str, full_path: str, backup_path: str | None) -> None:
        """Sample baseline error count and schedule a delayed rollback check.

        Fail-open: any error here is logged and ignored — never blocks the write.
        """
        try:
            if not backup_path:
                return  # nothing to roll back to
            baseline = await _sample_error_count()
            asyncio.ensure_future(_delayed_rollback_check(
                file_path, full_path, backup_path, baseline, self.redis_url
            ))
            logger.info(
                "self_improve.rollback_armed",
                file=file_path,
                baseline_errors=baseline,
                delay_s=_ROLLBACK_WATCH_DELAY_S,
            )
        except Exception as _e:
            logger.warning("self_improve.rollback_arm_failed", error=str(_e)[:120])

    def _persist_file(self, full_path: str, file_path: str) -> None:
        """Copy file to /data/src_patches so it survives container rebuilds.

        Also writes a <file>.sha256 sidecar so apply_persisted_patches()
        can verify integrity before re-applying after a rebuild.
        """
        try:
            persist_path = os.path.join(PERSIST_ROOT, file_path.lstrip("/"))
            os.makedirs(os.path.dirname(persist_path), exist_ok=True)
            shutil.copy2(full_path, persist_path)
            # Write SHA-256 sidecar for integrity verification at startup
            digest = _sha256_file(persist_path)
            with open(persist_path + ".sha256", "w") as sf:
                sf.write(digest)
            logger.info("self_improve.persisted", dst=persist_path, sha256=digest[:16] + "…")
        except Exception as e:
            logger.warning("self_improve.persist_failed", error=str(e))

    async def _write_file(self, file_path: str, new_content: str) -> SkillResult:
        """Directly overwrite a source file with automatic backup + persistence."""
        full_path, err = self._resolve_and_validate(file_path)
        if err:
            return SkillResult(skill_name="self_improve", success=False, output="", error=err)
        if not new_content:
            return SkillResult(
                skill_name="self_improve", success=False, output="", error="Provide content to write"
            )

        backup_path = self._backup_file(full_path, file_path)

        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        self._persist_file(full_path, file_path)
        logger.info("self_improve.written", path=full_path, size=len(new_content))

        # Schedule post-write rollback monitor
        await self._schedule_rollback_check(file_path, full_path, backup_path)

        msg = f"✅ Written {len(new_content)} chars to {full_path}"
        if backup_path:
            msg += f"\n   Backup: {backup_path}"
        msg += f"\n   Persisted to: {os.path.join(PERSIST_ROOT, file_path.lstrip('/'))}"
        msg += "\n\nI applied a small internal fix to improve reliability."
        return SkillResult(skill_name="self_improve", success=True, output=msg)

    async def _patch_file(self, file_path: str, old_text: str, new_text: str) -> SkillResult:
        """Apply a surgical text replacement to a source file.

        Replaces the first occurrence of old_text with new_text.
        Much safer than full rewrites for targeted edits.
        """
        full_path, err = self._resolve_and_validate(file_path)
        if err:
            return SkillResult(skill_name="self_improve", success=False, output="", error=err)
        if not old_text:
            return SkillResult(
                skill_name="self_improve", success=False, output="", error="Provide old_text to find"
            )
        if not os.path.exists(full_path):
            return SkillResult(
                skill_name="self_improve", success=False, output="", error=f"File not found: {full_path}"
            )

        size = os.path.getsize(full_path)
        if size > MAX_FILE_SIZE:
            return SkillResult(
                skill_name="self_improve", success=False, output="", error=f"File too large: {size} bytes"
            )

        with open(full_path, "r", encoding="utf-8") as f:
            original = f.read()

        if old_text not in original:
            # Show context around what we searched for to help debug
            preview = repr(old_text[:100])
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error=(
                    f"Text not found in {file_path}.\n"
                    f"Searched for: {preview}\n"
                    "Tip: read() the file first to verify exact whitespace/indentation."
                ),
            )

        count = original.count(old_text)
        patched = original.replace(old_text, new_text, 1)  # Replace first occurrence only

        backup_path = self._backup_file(full_path, file_path)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(patched)

        self._persist_file(full_path, file_path)
        logger.info("self_improve.patched", path=full_path, occurrences=count)

        # Schedule post-write rollback monitor
        await self._schedule_rollback_check(file_path, full_path, backup_path)

        msg = f"✅ Patched {file_path}"
        if count > 1:
            msg += f" (replaced 1 of {count} occurrences — use patch() again for others)"
        if backup_path:
            msg += f"\n   Backup: {backup_path}"
        msg += f"\n   Persisted to: {os.path.join(PERSIST_ROOT, file_path.lstrip('/'))}"
        msg += "\n\nI applied a small internal fix to improve reliability."
        return SkillResult(skill_name="self_improve", success=True, output=msg)

    async def _rebuild(self, file_path: str, new_content: str) -> SkillResult:
        """Write file then restart agent-core via broker."""
        write_result = await self._write_file(file_path, new_content)
        if not write_result.success:
            return write_result

        restart_msg = await self._restart_agent_core()
        return SkillResult(
            skill_name="self_improve",
            success=True,
            output=write_result.output + "\n\n" + restart_msg,
        )

    async def _install_package(self, package: str) -> SkillResult:
        """Install a Python package at runtime using pip (no container rebuild needed)."""
        if not package:
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error="Provide package name (e.g. 'httpx>=0.27' or 'pandas')",
            )

        logger.info("self_improve.install_start", package=package)
        try:
            proc = await asyncio.create_subprocess_exec(
                "pip", "install", "--quiet", package,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            out = stdout.decode(errors="replace").strip()
            err = stderr.decode(errors="replace").strip()

            if proc.returncode == 0:
                logger.info("self_improve.installed", package=package)
                msg = f"✅ Installed: {package}"
                if out:
                    msg += f"\n{out}"
                msg += "\n\nPackage is now importable in this process. No restart needed."
                return SkillResult(skill_name="self_improve", success=True, output=msg)
            else:
                return SkillResult(
                    skill_name="self_improve",
                    success=False,
                    output="",
                    error=f"pip install failed (exit {proc.returncode}):\n{err or out}",
                )
        except asyncio.TimeoutError:
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error="pip install timed out after 120s",
            )

    async def _restart_agent_core(self) -> str:
        """Restart agent-core via broker if available, else return instructions."""
        if self._broker is not None:
            try:
                result = await self._broker.restart_container(
                    "agent-core", requested_by="self_improve"
                )
                logger.info("self_improve.restart_requested", result=result)
                return "🔄 Restart signal sent to agent-core via broker."
            except Exception as e:
                logger.warning("self_improve.broker_restart_failed", error=str(e))
                return f"⚠️ Broker restart failed ({e}). Restart manually: docker restart agent-core"
        return "ℹ️ Broker not available. Restart manually: docker restart agent-core"

    # ------------------------------------------------------------------
    # Exploration: list_files / diff
    # ------------------------------------------------------------------

    def _list_files(self, search_path: str = "") -> SkillResult:
        """Walk /app/src and return a file tree."""
        base = os.path.join(SOURCE_ROOT, search_path.lstrip("/")) if search_path else SOURCE_ROOT
        # Containment check: resolved path must stay inside SOURCE_ROOT
        source_root_real = os.path.realpath(SOURCE_ROOT)
        base_real = os.path.realpath(base)
        if not (base_real.startswith(source_root_real + os.sep) or base_real == source_root_real):
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error="Access denied: path outside source root",
            )
        if not os.path.exists(base_real):
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error=f"Path not found: {base}",
            )

        lines = [f"Files under {base_real}:"]
        count = 0
        for root, dirs, files in os.walk(base_real):
            dirs[:] = [d for d in sorted(dirs) if d not in ("__pycache__", ".git", ".mypy_cache")]
            rel_root = os.path.relpath(root, base_real)
            prefix = "" if rel_root == "." else rel_root + "/"
            for fname in sorted(files):
                if fname.endswith((".pyc", ".pyo")):
                    continue
                fpath = os.path.join(root, fname)
                size = os.path.getsize(fpath)
                lines.append(f"  {prefix}{fname}  ({size:,} bytes)")
                count += 1
                if count >= 500:
                    lines.append("  ... (truncated at 500 files)")
                    break
            if count >= 500:
                break

        lines.append(f"\nTotal: {count} files")
        return SkillResult(
            skill_name="self_improve", success=True, output="\n".join(lines)
        )

    def _dry_run_write(self, file_path: str, new_content: str) -> SkillResult:
        """Preview a full-file write as a unified diff against the current file.

        Does NOT touch the file, create a backup, or persist anything.
        Runs the same AST validator the real write uses so the agent learns
        whether the write would have been rejected without taking the risk.
        """
        full_path, err = self._resolve_and_validate(file_path)
        if err:
            return SkillResult(skill_name="self_improve", success=False, output="", error=err)
        if not new_content:
            return SkillResult(
                skill_name="self_improve", success=False, output="",
                error="Provide content to preview",
            )
        try:
            current = open(full_path, "r", encoding="utf-8").read() if os.path.exists(full_path) else ""
        except Exception as e:
            return SkillResult(skill_name="self_improve", success=False, output="", error=f"Read failed: {e}")

        import difflib as _difflib
        diff_text = "".join(_difflib.unified_diff(
            current.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            n=3,
        ))
        if not diff_text:
            diff_text = "(no changes — new content is identical to current file)"

        ast_ok, ast_err = _dry_run_validate_content(full_path, new_content)
        ast_line = "AST validation: PASS" if ast_ok else f"AST validation: FAIL — {ast_err}"

        return SkillResult(
            skill_name="self_improve",
            success=True,
            output=(
                f"[DRY RUN] write({file_path}) — no file was modified.\n"
                f"{ast_line}\n"
                f"--- unified diff ---\n{diff_text}"
            ),
        )

    def _dry_run_patch(self, file_path: str, old_text: str, new_text: str) -> SkillResult:
        """Preview a surgical patch as a unified diff. No file write."""
        full_path, err = self._resolve_and_validate(file_path)
        if err:
            return SkillResult(skill_name="self_improve", success=False, output="", error=err)
        if not old_text:
            return SkillResult(
                skill_name="self_improve", success=False, output="",
                error="Provide old_text to find",
            )
        if not os.path.exists(full_path):
            return SkillResult(
                skill_name="self_improve", success=False, output="",
                error=f"File not found: {full_path}",
            )
        try:
            original = open(full_path, "r", encoding="utf-8").read()
        except Exception as e:
            return SkillResult(skill_name="self_improve", success=False, output="", error=f"Read failed: {e}")

        if old_text not in original:
            return SkillResult(
                skill_name="self_improve", success=False, output="",
                error=(
                    f"[DRY RUN] Text not found in {file_path}.\n"
                    f"Searched for: {old_text[:120]!r}\n"
                    "Tip: read() the file first to verify exact whitespace."
                ),
            )
        count = original.count(old_text)
        patched = original.replace(old_text, new_text, 1)

        import difflib as _difflib
        diff_text = "".join(_difflib.unified_diff(
            original.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            n=3,
        ))

        ast_ok, ast_err = _dry_run_validate_content(full_path, patched)
        ast_line = "AST validation: PASS" if ast_ok else f"AST validation: FAIL — {ast_err}"

        notes = []
        if count > 1:
            notes.append(f"NOTE: {count} occurrences would match; only the first would be patched.")

        return SkillResult(
            skill_name="self_improve",
            success=True,
            output=(
                f"[DRY RUN] patch({file_path}) — no file was modified.\n"
                f"{ast_line}\n"
                + ("\n".join(notes) + "\n" if notes else "")
                + f"--- unified diff ---\n{diff_text}"
            ),
        )

    def _show_diff(self, file_path: str) -> SkillResult:
        """Show diff between latest backup and current file."""
        if not file_path:
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error="Provide a file path",
            )

        full_path = os.path.join(SOURCE_ROOT, file_path.lstrip("/"))
        safe_name = file_path.lstrip("/").replace("/", "_")

        if not os.path.isdir(BACKUP_ROOT):
            return SkillResult(
                skill_name="self_improve",
                success=True,
                output="No backups found — no writes have been made yet.",
            )

        backups = sorted(
            [f for f in os.listdir(BACKUP_ROOT) if f.startswith(safe_name + ".")],
            reverse=True,
        )
        if not backups:
            return SkillResult(
                skill_name="self_improve",
                success=True,
                output=f"No backup found for {file_path}",
            )

        backup_path = os.path.join(BACKUP_ROOT, backups[0])
        try:
            result = subprocess.run(
                ["diff", "-u", backup_path, full_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            diff_output = result.stdout or "(no differences)"
            return SkillResult(
                skill_name="self_improve",
                success=True,
                output=f"Diff (backup vs current) for {file_path}:\n\n{diff_output}",
            )
        except Exception as e:
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error=f"diff failed: {e}",
            )

    # ------------------------------------------------------------------
    # Legacy: propose / list / apply / reject
    # ------------------------------------------------------------------

    async def _propose(self, r, file_path: str, change: str, diff: str) -> SkillResult:
        if not file_path or not diff:
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error="Provide file and diff/new content for the proposal",
            )
        proposal_id = str(uuid4())[:8]
        proposal = {
            "id": proposal_id,
            "file": file_path,
            "change": change,
            "diff": diff,
            "created_at": time.time(),
        }
        await r.hset(PROPOSALS_KEY, proposal_id, json.dumps(proposal))
        return SkillResult(
            skill_name="self_improve",
            success=True,
            output=(
                f"Proposal stored (ID: {proposal_id})\n"
                f"File: {file_path}\n"
                f"Change: {change}\n\n"
                f"Tip: Use write(file='{file_path}', content=...) or patch() to apply directly."
            ),
        )

    async def _list_proposals(self, r) -> SkillResult:
        proposals = await r.hgetall(PROPOSALS_KEY)
        if not proposals:
            return SkillResult(
                skill_name="self_improve",
                success=True,
                output="No pending proposals.",
            )
        lines = [f"Pending proposals ({len(proposals)}):"]
        for pid, raw in proposals.items():
            try:
                p = json.loads(raw)
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.get("created_at", 0)))
                lines.append(f"  [{p['id']}] {p['file']} — {p['change'][:80]} ({ts})")
            except Exception:
                lines.append(f"  [{pid}] (corrupted)")
        return SkillResult(
            skill_name="self_improve", success=True, output="\n".join(lines)
        )

    async def _apply(self, r, proposal_id: str) -> SkillResult:
        if not proposal_id:
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error="Provide proposal_id to apply",
            )
        raw = await r.hget(PROPOSALS_KEY, proposal_id)
        if not raw:
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error=f"Proposal '{proposal_id}' not found",
            )
        proposal = json.loads(raw)
        file_path = proposal["file"]
        diff_content = proposal["diff"]

        write_result = await self._write_file(file_path, diff_content)
        if write_result.success:
            await r.hdel(PROPOSALS_KEY, proposal_id)

        return SkillResult(
            skill_name="self_improve",
            success=write_result.success,
            output=write_result.output,
            error=write_result.error,
        )

    async def _reject(self, r, proposal_id: str) -> SkillResult:
        if not proposal_id:
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error="Provide proposal_id to reject",
            )
        deleted = await r.hdel(PROPOSALS_KEY, proposal_id)
        if not deleted:
            return SkillResult(
                skill_name="self_improve",
                success=False,
                output="",
                error=f"Proposal '{proposal_id}' not found",
            )
        return SkillResult(
            skill_name="self_improve",
            success=True,
            output=f"Proposal {proposal_id} rejected and removed.",
        )
