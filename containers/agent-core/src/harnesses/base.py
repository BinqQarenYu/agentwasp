"""Core contract and subprocess streaming engine for external CLI harnesses.

Inspired by SuperBot's harness-agnostic adapter design, hardened for Agent WASP:
- Unified `AgentEvent` stream across Claude Code, Codex, Pi, and Gemini CLIs.
- Safe-by-default permissions (`allow_unsafe_bypass=False` unless explicitly opted in
  inside an isolated sandbox container).
- Cross-platform binary resolution (Windows `.exe`/`.cmd`/`.ps1` + POSIX binaries)
  and reliable process-tree termination on abort or timeout.
"""

from __future__ import annotations

import abc
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable


class EventType(str, Enum):
    SESSION = "session"
    TEXT_DELTA = "text_delta"
    TEXT = "text"
    TOOL = "tool"
    ERROR = "error"
    DONE = "done"


@dataclass(slots=True)
class AgentEvent:
    type: EventType
    text: str = ""
    session_id: str = ""
    tool_name: str = ""
    tool_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type.value}
        if self.text:
            payload["text"] = self.text
        if self.session_id:
            payload["session_id"] = self.session_id
        if self.tool_name:
            payload["tool_name"] = self.tool_name
        if self.tool_detail:
            payload["tool_detail"] = self.tool_detail
        return payload


@dataclass(slots=True)
class ModelOption:
    id: str
    label: str
    efforts: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "efforts": list(self.efforts),
        }
        if self.aliases:
            data["aliases"] = list(self.aliases)
        return data


@dataclass(slots=True)
class RunInput:
    prompt: str
    system: str
    cwd: Path
    state_dir: Path
    session_id: str | None = None
    model: str | None = None
    effort: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 600.0
    # Security default: NEVER bypass harness approvals/sandboxes unless explicitly
    # opted-in for a strictly isolated container workspace.
    allow_unsafe_bypass: bool = False


@dataclass(slots=True)
class RunResult:
    ok: bool
    text: str = ""
    session_id: str | None = None
    events: list[AgentEvent] = field(default_factory=list)
    error: str | None = None
    aborted_reason: str | None = None
    exit_code: int | None = None


def short_text(text: str, limit: int = 80) -> str:
    """Compact single-line preview for live tool status."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(limit - 1, 1)].rstrip() + "…"


def describe_tool_input(tool_input: Any) -> str:
    """Extract a concise human-readable summary from structured tool arguments."""
    if isinstance(tool_input, str):
        return short_text(tool_input)
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "cmd", "path", "file_path", "url", "query", "pattern", "prompt", "description"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return short_text(val)
    for val in tool_input.values():
        if isinstance(val, str) and val.strip():
            return short_text(val)
    return ""


def resolve_binary(bin_name: str) -> str | None:
    """Locate a CLI executable on PATH across Windows and POSIX."""
    direct = shutil.which(bin_name)
    if direct:
        return direct
    if os.name == "nt":
        for ext in (".exe", ".cmd", ".bat", ".ps1"):
            found = shutil.which(f"{bin_name}{ext}")
            if found:
                return found
    return None


def run_command_stdout(bin_name: str, args: list[str], timeout: float = 10.0) -> str:
    """Best-effort blocking helper for model discovery commands."""
    resolved = resolve_binary(bin_name)
    if not resolved:
        return ""
    cmd = _wrap_command(resolved, args)
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return proc.stdout if proc.returncode == 0 else ""
    except Exception:
        return ""


def _wrap_command(resolved_bin: str, args: list[str]) -> list[str]:
    """Handle Windows .ps1/.cmd shims cleanly without shell injection."""
    lower = resolved_bin.lower()
    if os.name == "nt" and lower.endswith(".ps1"):
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            resolved_bin,
            *args,
        ]
    if os.name == "nt" and (lower.endswith(".cmd") or lower.endswith(".bat")):
        return ["cmd.exe", "/c", resolved_bin, *args]
    return [resolved_bin, *args]


class HarnessAdapter(abc.ABC):
    """Minimal contract every external CLI harness implements."""

    name: str = ""
    bin_name: str = ""

    @abc.abstractmethod
    def build_args(self, run_input: RunInput, pre_events: list[AgentEvent]) -> list[str]:
        """Build CLI arguments and optionally emit upfront events (e.g., session id)."""

    def extra_env(self, run_input: RunInput) -> dict[str, str]:
        """Optional adapter-specific environment variables."""
        return {}

    @abc.abstractmethod
    def parse_event(self, payload: dict[str, Any], out: list[AgentEvent]) -> None:
        """Map one parsed JSON object from stdout into zero or more AgentEvents."""

    @abc.abstractmethod
    def list_models(self) -> list[ModelOption]:
        """Return selectable models and supported reasoning efforts."""


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the harness subprocess and any children cleanly across platforms."""
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            # taskkill /T /F terminates the full process tree on Windows
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5.0)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except Exception:
        pass


EmitCallback = Callable[[AgentEvent], Awaitable[None] | None]


async def execute_harness(
    adapter: HarnessAdapter,
    run_input: RunInput,
    abort_event: asyncio.Event | None = None,
    emit: EmitCallback | None = None,
) -> RunResult:
    """Spawn a CLI harness, stream its JSONL stdout as AgentEvents, and return RunResult."""
    events: list[AgentEvent] = []
    text_blocks: list[str] = []
    text_deltas: list[str] = []
    session_id: str | None = run_input.session_id
    errors: list[str] = []

    async def _dispatch(ev: AgentEvent) -> None:
        nonlocal session_id
        events.append(ev)
        if ev.type == EventType.SESSION and ev.session_id:
            session_id = ev.session_id
        elif ev.type == EventType.TEXT and ev.text:
            text_blocks.append(ev.text)
        elif ev.type == EventType.TEXT_DELTA and ev.text:
            text_deltas.append(ev.text)
        elif ev.type == EventType.ERROR and ev.text:
            errors.append(ev.text)
        if emit is not None:
            res = emit(ev)
            if asyncio.iscoroutine(res):
                await res

    resolved = resolve_binary(adapter.bin_name)
    if not resolved:
        msg = f"Harness CLI '{adapter.bin_name}' is not installed or not on PATH."
        await _dispatch(AgentEvent(type=EventType.ERROR, text=msg))
        return RunResult(ok=False, error=msg, events=events, session_id=session_id)

    run_input.state_dir.mkdir(parents=True, exist_ok=True)
    run_input.cwd.mkdir(parents=True, exist_ok=True)

    pre_events: list[AgentEvent] = []
    args = adapter.build_args(run_input, pre_events)
    for ev in pre_events:
        await _dispatch(ev)

    cmd = _wrap_command(resolved, args)
    env = os.environ.copy()
    env.update(adapter.extra_env(run_input))
    env.update(run_input.env)

    spawn_kwargs: dict[str, Any] = {
        "cwd": str(run_input.cwd),
        "env": env,
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "nt":
        spawn_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        spawn_kwargs["start_new_session"] = True

    try:
        proc = await asyncio.create_subprocess_exec(*cmd, **spawn_kwargs)
    except Exception as exc:
        msg = f"Failed to spawn '{adapter.bin_name}': {exc}"
        await _dispatch(AgentEvent(type=EventType.ERROR, text=msg))
        return RunResult(ok=False, error=msg, events=events, session_id=session_id)

    stderr_chunks: list[bytes] = []

    async def _read_stderr() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    async def _read_stdout() -> None:
        assert proc.stdout is not None
        while True:
            raw_line = await proc.stdout.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                batch: list[AgentEvent] = []
                adapter.parse_event(parsed, batch)
                for ev in batch:
                    await _dispatch(ev)

    stderr_task = asyncio.create_task(_read_stderr())
    stdout_task = asyncio.create_task(_read_stdout())
    wait_task = asyncio.create_task(proc.wait())

    aborted_reason: str | None = None
    abort_task: asyncio.Task[bool] | None = None
    if abort_event is not None:
        abort_task = asyncio.create_task(abort_event.wait())

    try:
        pending: set[asyncio.Task[Any]] = {wait_task}
        if abort_task is not None:
            pending.add(abort_task)

        done, _ = await asyncio.wait(
            pending,
            timeout=run_input.timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            aborted_reason = "timeout"
            await _terminate_process_tree(proc)
        elif abort_task is not None and abort_task in done:
            aborted_reason = "cancelled"
            await _terminate_process_tree(proc)
    finally:
        if abort_task is not None and not abort_task.done():
            abort_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, wait_task, return_exceptions=True)

    exit_code = proc.returncode
    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()

    if aborted_reason == "timeout":
        msg = f"Harness '{adapter.name}' timed out after {int(run_input.timeout_seconds)}s."
        await _dispatch(AgentEvent(type=EventType.ERROR, text=msg))
    elif aborted_reason == "cancelled":
        msg = f"Harness '{adapter.name}' run was cancelled."
        await _dispatch(AgentEvent(type=EventType.ERROR, text=msg))
    elif exit_code not in (0, None) and not errors:
        detail = short_text(stderr_text, 240) if stderr_text else f"exit code {exit_code}"
        await _dispatch(AgentEvent(type=EventType.ERROR, text=f"{adapter.name} failed: {detail}"))

    final_text = "\n\n".join(text_blocks).strip()
    if not final_text and text_deltas:
        final_text = "".join(text_deltas).strip()

    await _dispatch(AgentEvent(type=EventType.DONE, text=final_text, session_id=session_id or ""))

    ok = aborted_reason is None and exit_code == 0 and not errors
    return RunResult(
        ok=ok,
        text=final_text,
        session_id=session_id,
        events=events,
        error=errors[-1] if errors else None,
        aborted_reason=aborted_reason,
        exit_code=exit_code,
    )
