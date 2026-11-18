"""Codex CLI adapter (`codex exec --json` / `codex exec resume <thread_id>`).

Never passes `--dangerously-bypass-approvals-and-sandbox` unless
`run_input.allow_unsafe_bypass=True`.
"""

from __future__ import annotations

import json
from typing import Any

from .base import (
    AgentEvent,
    EventType,
    HarnessAdapter,
    ModelOption,
    RunInput,
    run_command_stdout,
    short_text,
)


def _unwrap_shell_command(cmd: str) -> str:
    for marker in (" -lc '", " -c '"):
        if marker in cmd:
            _, rest = cmd.split(marker, 1)
            if rest.endswith("'"):
                rest = rest[:-1]
            return short_text(rest)
    return short_text(cmd)


class CodexAdapter(HarnessAdapter):
    name = "codex"
    bin_name = "codex"

    def build_args(self, run_input: RunInput, pre_events: list[AgentEvent]) -> list[str]:
        args: list[str] = ["exec"]
        if run_input.session_id:
            args.append("resume")
        args.extend(["--json", "--skip-git-repo-check"])
        if run_input.allow_unsafe_bypass:
            args.append("--dangerously-bypass-approvals-and-sandbox")

        if run_input.system:
            args.extend(["-c", f"developer_instructions={json.dumps(run_input.system)}"])
        if run_input.model:
            args.extend(["-m", run_input.model])
        if run_input.effort:
            args.extend(["-c", f"model_reasoning_effort={json.dumps(run_input.effort)}"])

        args.append("--")
        if run_input.session_id:
            args.append(run_input.session_id)
        args.append(run_input.prompt)
        return args

    def parse_event(self, payload: dict[str, Any], out: list[AgentEvent]) -> None:
        ev_type = str(payload.get("type") or "")
        item = payload.get("item") or {}
        kind = str(item.get("type") or "") if isinstance(item, dict) else ""

        if ev_type == "thread.started":
            tid = str(payload.get("thread_id") or "")
            if tid:
                out.append(AgentEvent(type=EventType.SESSION, session_id=tid))
            return

        if ev_type == "item.completed" and kind == "agent_message":
            text = str(item.get("text") or "")
            if text.strip():
                out.append(AgentEvent(type=EventType.TEXT, text=text))
            return

        if ev_type == "item.started" and kind == "command_execution":
            cmd = str(item.get("command") or "")
            out.append(AgentEvent(type=EventType.TOOL, tool_name="shell", tool_detail=_unwrap_shell_command(cmd)))
            return

        if ev_type == "item.started" and kind and kind != "reasoning":
            server = item.get("server")
            tool = item.get("tool")
            if isinstance(server, str) and isinstance(tool, str):
                detail = f"{server}.{tool}"
            else:
                detail = short_text(str(item.get("query") or ""))
            out.append(AgentEvent(type=EventType.TOOL, tool_name=kind, tool_detail=detail))
            return

        if ev_type in ("turn.failed", "error"):
            err = payload.get("error")
            msg = ""
            if isinstance(err, dict):
                msg = str(err.get("message") or "")
            if not msg:
                msg = str(payload.get("message") or "Codex failed")
            out.append(AgentEvent(type=EventType.ERROR, text=msg))

    def list_models(self) -> list[ModelOption]:
        raw = run_command_stdout(self.bin_name, ["debug", "models"])
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        models: list[ModelOption] = []
        for entry in parsed.get("models") or []:
            if not isinstance(entry, dict) or entry.get("visibility") != "list":
                continue
            slug = str(entry.get("slug") or "")
            if not slug:
                continue
            label = str(entry.get("display_name") or slug)
            levels = entry.get("supported_reasoning_levels") or []
            efforts = [str(l.get("effort")) for l in levels if isinstance(l, dict) and l.get("effort")]
            models.append(ModelOption(id=slug, label=label, efforts=efforts))
        return models
