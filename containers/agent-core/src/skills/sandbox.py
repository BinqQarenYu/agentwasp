"""Subprocess sandbox for skill execution.

Only generated skills (those in /data/skills/generated/) are sandboxed.
Builtin skills run in-process as before — they require full network/DB access.

Isolation model:
  - Separate subprocess (different memory space, crash isolation)
  - Stripped environment (no REDIS_URL, DATABASE_URL, or API keys)
  - Restricted working directory (/tmp)
  - Hard wall-clock timeout enforced via asyncio.wait_for + proc.kill()
  - stdin/stdout JSON protocol — no shared file descriptors

Usage:
    from .sandbox import is_sandboxable, execute_sandboxed
    if is_sandboxable(skill_name):
        result = await execute_sandboxed(skill_name, arguments, timeout)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import structlog

from .types import SkillResult

logger = structlog.get_logger()

# Generated skill directory — must match capability_evolution_engine._GEN_DIR
_GEN_DIR = "/data/skills/generated"

# Secret env vars that must NEVER be passed into the subprocess sandbox.
_SECRET_VARS = frozenset([
    "REDIS_URL", "DATABASE_URL", "POSTGRES_PASSWORD", "POSTGRES_USER",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN",
    "MOONSHOT_API_KEY", "DEEPSEEK_API_KEY", "GOOGLE_API_KEY",
    "SENDGRID_API_KEY", "STRIPE_SECRET_KEY", "SLACK_BOT_TOKEN",
    "EMAIL_PASSWORD", "SMTP_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
])

# Absolute path to the runner script inside the container
_RUNNER_PATH = "/app/src/skills/sandbox_runner.py"


def _make_sandbox_env() -> dict:
    """Build subprocess environment: inherit system vars, strip all secrets.

    Inheriting the parent env is required so Python's stdlib (asyncio, ssl, etc.)
    can initialise correctly. We strip all known secret variables by name.
    """
    env = {k: v for k, v in os.environ.items() if k not in _SECRET_VARS}
    # Force safe paths regardless of parent settings
    env["HOME"] = "/tmp"
    env["TMPDIR"] = "/tmp"
    env["PYTHONPATH"] = "/app"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def is_sandboxable(skill_name: str) -> bool:
    """Return True if this skill should be executed in a subprocess sandbox.

    Only generated skills (files under _GEN_DIR) are sandboxed.
    Builtin skills always run in-process.
    """
    skill_path = os.path.join(_GEN_DIR, skill_name, "skill.py")
    return os.path.isfile(skill_path)


def _skill_path(skill_name: str) -> str:
    return os.path.join(_GEN_DIR, skill_name, "skill.py")


async def execute_sandboxed(
    skill_name: str,
    arguments: dict,
    timeout: float = 30.0,
) -> SkillResult:
    """Execute a generated skill in an isolated subprocess.

    Returns a SkillResult. Never raises — all exceptions produce a failure result.

    Logs:
      sandbox.start   — subprocess launched
      sandbox.success — completed within timeout
      sandbox.timeout — killed after timeout
      sandbox.failure — subprocess error or bad output
      sandbox.reject  — skill_path not found (should not normally happen)
    """
    skill_path = _skill_path(skill_name)

    if not os.path.isfile(skill_path):
        logger.warning("sandbox.reject", skill=skill_name, path=skill_path)
        return SkillResult(
            skill_name=skill_name,
            success=False,
            output="",
            error=f"Sandbox: skill file not found at {skill_path}",
        )

    payload = json.dumps({
        "skill_path": skill_path,
        "skill_name": skill_name,
        "arguments": {k: str(v) for k, v in arguments.items()},
    }).encode()

    logger.info("sandbox.start", skill=skill_name, timeout=timeout)
    t0 = time.monotonic()

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, _RUNNER_PATH,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",
            env=_make_sandbox_env(),
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=payload + b"\n"),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            ms = int((time.monotonic() - t0) * 1000)
            logger.warning("sandbox.timeout", skill=skill_name, timeout=timeout, ms=ms)
            return SkillResult(
                skill_name=skill_name,
                success=False,
                output="",
                error=f"Sandbox: skill '{skill_name}' timed out after {timeout}s",
                execution_ms=ms,
            )

        ms = int((time.monotonic() - t0) * 1000)
        stdout_text = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
        stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()

        if not stdout_text:
            logger.warning("sandbox.failure", skill=skill_name, ms=ms,
                           stderr=stderr_text[:200], reason="empty_stdout")
            return SkillResult(
                skill_name=skill_name,
                success=False,
                output="",
                error=f"Sandbox: no output from runner. stderr={stderr_text[:200]}",
                execution_ms=ms,
            )

        try:
            data = json.loads(stdout_text)
        except json.JSONDecodeError as e:
            logger.warning("sandbox.failure", skill=skill_name, ms=ms,
                           reason="json_parse", error=str(e))
            return SkillResult(
                skill_name=skill_name,
                success=False,
                output="",
                error=f"Sandbox: invalid JSON output: {stdout_text[:200]}",
                execution_ms=ms,
            )

        logger.info("sandbox.success", skill=skill_name, ms=ms,
                    success=data.get("success"), returncode=proc.returncode)
        return SkillResult(
            skill_name=skill_name,
            success=bool(data.get("success", False)),
            output=str(data.get("output", "")),
            error=str(data.get("error", "")),
            execution_ms=data.get("execution_ms", ms),
        )

    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        logger.exception("sandbox.failure", skill=skill_name, ms=ms, error=str(exc)[:200])
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        return SkillResult(
            skill_name=skill_name,
            success=False,
            output="",
            error=f"Sandbox error: {exc}",
            execution_ms=ms,
        )
