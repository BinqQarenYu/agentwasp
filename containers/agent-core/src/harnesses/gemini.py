"""Gemini CLI adapter (`gemini -p <prompt> --output-format stream-json`).

Persists the bot's dynamic system prompt into `state_dir/GEMINI_SYSTEM.md` and passes
it via `GEMINI_SYSTEM_MD` so memory and instructions refresh cleanly on every run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import AgentEvent, EventType, HarnessAdapter, ModelOption, RunInput, describe_tool_input


class GeminiAdapter(HarnessAdapter):
    name = "gemini"
    bin_name = "gemini"

    def __init__(self) -> None:
        self._system_file: Path | None = None

    def build_args(self, run_input: RunInput, pre_events: list[AgentEvent]) -> list[str]:
        if run_input.system:
            run_input.state_dir.mkdir(parents=True, exist_ok=True)
            self._system_file = run_input.state_dir / "GEMINI_SYSTEM.md"
            self._system_file.write_text(run_input.system, encoding="utf-8")
        else:
            self._system_file = None

        args: list[str] = ["--output-format", "stream-json"]
        if run_input.allow_unsafe_bypass:
            args.append("--yolo")
        if run_input.session_id:
            args.extend(["--resume", run_input.session_id])
        if run_input.model:
            args.extend(["-m", run_input.model])
        args.extend(["-p", run_input.prompt])
        return args

    def extra_env(self, run_input: RunInput) -> dict[str, str]:
        if self._system_file is not None:
            return {"GEMINI_SYSTEM_MD": str(self._system_file)}
        return {}

    def parse_event(self, payload: dict[str, Any], out: list[AgentEvent]) -> None:
        ev_type = str(payload.get("type") or "")

        if ev_type == "init":
            sid = str(payload.get("session_id") or "")
            if sid:
                out.append(AgentEvent(type=EventType.SESSION, session_id=sid))
            return

        if ev_type == "message" and payload.get("role") == "assistant":
            content = str(payload.get("content") or "")
            if payload.get("delta") is True:
                if content:
                    out.append(AgentEvent(type=EventType.TEXT_DELTA, text=content))
            elif content.strip():
                out.append(AgentEvent(type=EventType.TEXT, text=content))
            return

        if ev_type == "tool_use":
            out.append(
                AgentEvent(
                    type=EventType.TOOL,
                    tool_name=str(payload.get("tool_name") or payload.get("name") or "tool"),
                    tool_detail=describe_tool_input(payload.get("parameters") or payload.get("args")),
                )
            )
            return

        if ev_type == "error":
            msg = str(payload.get("message") or payload.get("error") or "Gemini CLI failed")
            out.append(AgentEvent(type=EventType.ERROR, text=msg))
            return

        if ev_type == "result" and payload.get("status") == "error":
            err = payload.get("error") or {}
            msg = str(err.get("message") if isinstance(err, dict) else err) or "Gemini CLI failed"
            out.append(AgentEvent(type=EventType.ERROR, text=msg))

    def list_models(self) -> list[ModelOption]:
        return [
            ModelOption(id="gemini-2.5-pro", label="Gemini 2.5 Pro"),
            ModelOption(id="gemini-2.5-flash", label="Gemini 2.5 Flash"),
            ModelOption(id="gemini-2.0-flash", label="Gemini 2.0 Flash"),
        ]
