"""Claude Code CLI adapter (`claude -p --output-format stream-json`).

Resumes sessions with `--resume <session_id>`. System prompt is applied fresh every run.
Never passes `--dangerously-skip-permissions` unless `run_input.allow_unsafe_bypass=True`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .base import AgentEvent, EventType, HarnessAdapter, ModelOption, RunInput, describe_tool_input


class ClaudeAdapter(HarnessAdapter):
    name = "claude"
    bin_name = "claude"

    def __init__(self) -> None:
        self._session_id: str = ""
        self._saw_turn: bool = False

    def build_args(self, run_input: RunInput, pre_events: list[AgentEvent]) -> list[str]:
        self._session_id = run_input.session_id or str(uuid4())
        self._saw_turn = False

        args: list[str] = [
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if run_input.allow_unsafe_bypass:
            args.append("--dangerously-skip-permissions")
        if run_input.system:
            args.extend(["--append-system-prompt", run_input.system])

        if run_input.session_id:
            args.extend(["--resume", self._session_id])
        else:
            args.extend(["--session-id", self._session_id])

        if run_input.model:
            args.extend(["--model", run_input.model])
        if run_input.effort:
            args.extend(["--effort", run_input.effort])

        args.extend(["--", run_input.prompt])
        return args

    def parse_event(self, payload: dict[str, Any], out: list[AgentEvent]) -> None:
        ev_type = str(payload.get("type") or "")

        if ev_type == "stream_event":
            ev = payload.get("event") or {}
            delta = ev.get("delta") or {}
            if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                chunk = str(delta.get("text") or "")
                if chunk:
                    out.append(AgentEvent(type=EventType.TEXT_DELTA, text=chunk))
            return

        if ev_type == "assistant":
            # Only commit the session ID once a real assistant turn occurs.
            if not self._saw_turn and self._session_id:
                self._saw_turn = True
                out.append(AgentEvent(type=EventType.SESSION, session_id=self._session_id))
            message = payload.get("message") or {}
            content = message.get("content") or []
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = str(item.get("type") or "")
                    if item_type == "text":
                        text = str(item.get("text") or "")
                        if text.strip():
                            out.append(AgentEvent(type=EventType.TEXT, text=text))
                    elif item_type == "tool_use":
                        out.append(
                            AgentEvent(
                                type=EventType.TOOL,
                                tool_name=str(item.get("name") or "tool"),
                                tool_detail=describe_tool_input(item.get("input")),
                            )
                        )
            return

        if ev_type == "result" and payload.get("is_error") is True:
            msg = str(payload.get("result") or "Claude Code failed")
            out.append(AgentEvent(type=EventType.ERROR, text=msg))

    def list_models(self) -> list[ModelOption]:
        efforts = ["low", "medium", "high", "xhigh", "max"]
        return [
            ModelOption(id="opus", label="Claude Opus (alias)", efforts=efforts, aliases=["claude-opus"]),
            ModelOption(id="sonnet", label="Claude Sonnet (alias)", efforts=efforts, aliases=["claude-sonnet"]),
            ModelOption(id="haiku", label="Claude Haiku (alias)", efforts=efforts, aliases=["claude-haiku"]),
        ]
