"""Pi coding agent CLI adapter (`pi --mode json`).

Stores sessions inside the bot's private `state_dir/pi-sessions` directory and pads
prompts starting with `@` so Pi doesn't treat them as file includes.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .base import (
    AgentEvent,
    EventType,
    HarnessAdapter,
    ModelOption,
    RunInput,
    describe_tool_input,
    run_command_stdout,
)


class PiAdapter(HarnessAdapter):
    name = "pi"
    bin_name = "pi"

    def build_args(self, run_input: RunInput, pre_events: list[AgentEvent]) -> list[str]:
        session_id = run_input.session_id or f"s-{uuid4()}"
        pre_events.append(AgentEvent(type=EventType.SESSION, session_id=session_id))

        session_dir = run_input.state_dir / "pi-sessions"
        session_dir.mkdir(parents=True, exist_ok=True)

        args: list[str] = [
            "--mode",
            "json",
            "--session-dir",
            str(session_dir),
            "--session-id",
            session_id,
        ]
        if run_input.system:
            args.extend(["--append-system-prompt", run_input.system])
        if run_input.model:
            args.extend(["--model", run_input.model])
        if run_input.effort:
            args.extend(["--thinking", run_input.effort])

        prompt = f" {run_input.prompt}" if run_input.prompt.startswith("@") else run_input.prompt
        args.extend(["--", prompt])
        return args

    def parse_event(self, payload: dict[str, Any], out: list[AgentEvent]) -> None:
        ev_type = str(payload.get("type") or "")

        if ev_type == "message_update":
            ev = payload.get("assistantMessageEvent") or {}
            if ev.get("type") == "text_delta":
                delta = str(ev.get("delta") or "")
                if delta:
                    out.append(AgentEvent(type=EventType.TEXT_DELTA, text=delta))
            return

        if ev_type == "message_end":
            msg = payload.get("message") or {}
            if msg.get("role") != "assistant":
                return
            for item in msg.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text") or "")
                    if text.strip():
                        out.append(AgentEvent(type=EventType.TEXT, text=text))
            if msg.get("stopReason") == "error":
                out.append(AgentEvent(type=EventType.ERROR, text=str(msg.get("errorMessage") or "Pi failed")))
            return

        if ev_type == "tool_execution_start":
            out.append(
                AgentEvent(
                    type=EventType.TOOL,
                    tool_name=str(payload.get("toolName") or "tool"),
                    tool_detail=describe_tool_input(payload.get("args")),
                )
            )

    def list_models(self) -> list[ModelOption]:
        efforts = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
        raw = run_command_stdout(self.bin_name, ["--list-models"])
        if not raw:
            return []
        models: list[ModelOption] = []
        for line in raw.splitlines()[1:]:
            cols = line.split()
            if len(cols) < 2:
                continue
            model_id = f"{cols[0]}/{cols[1]}"
            thinks = len(cols) > 4 and cols[4].lower() == "yes"
            models.append(ModelOption(id=model_id, label=model_id, efforts=efforts if thinks else []))
        return models
