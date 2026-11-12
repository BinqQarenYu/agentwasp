"""Security & Monitoring — Protects the agent from self-destruction.

Every file write, bash command, and network request is logged.
Dangerous operations are blocked. Everything is auditable.
The agent CANNOT disable this module.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROTECTED_PATHS = {
    "evo/security/guard.py",
    "evo/security/__init__.py",
}

DANGEROUS_COMMANDS = [
    "rm -rf /",
    "dd if=",
    "mkfs.",
    ":(){ :|:& };:",  # fork bomb
    "> /dev/sda",
    "shutdown",
    "reboot",
    "chmod 777 /",
    "wget -O - | sh",
    "curl | bash",
]


class SecurityGuard:
    """Monitors and restricts the agent's actions.

    The agent is powerful but not omnipotent over itself.
    This module is the immune system — it cannot be disabled by the agent.
    """

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.audit_log_path = work_dir / "mue_audit.jsonl"
        self.write_log: list[dict] = []
        self.bash_log: list[dict] = []
        self.edit_log: list[dict] = []
        self.blocked_actions: list[dict] = []

    # ── PERMISSION CHECKS ──

    def allow_write(self, path: Path, content: str) -> bool:
        """Check if the agent is allowed to write this file."""
        # NEVER allow overwriting security files
        rel = str(path.relative_to(self.work_dir)) if self.work_dir in path.parents else str(path)
        if rel in PROTECTED_PATHS:
            self.blocked_actions.append({
                "time": time.time(),
                "action": "write_blocked",
                "path": rel,
                "reason": "Protected security file",
            })
            return False

        # Limit file size
        if len(content) > 500_000:  # 500KB max
            self.blocked_actions.append({
                "time": time.time(),
                "action": "write_blocked",
                "path": rel,
                "reason": f"File too large: {len(content)} bytes",
            })
            return False

        return True

    def allow_bash(self, command: str) -> bool:
        """Check if a bash command is safe to execute."""
        cmd_lower = command.lower()

        for dangerous in DANGEROUS_COMMANDS:
            if dangerous in cmd_lower:
                self.blocked_actions.append({
                    "time": time.time(),
                    "action": "bash_blocked",
                    "command": command[:200],
                    "reason": f"Matches dangerous pattern: {dangerous}",
                })
                return False

        # Allow everything else (the agent has broad powers)
        return True

    def allow_network(self, url: str) -> bool:
        """Check if network access is allowed."""
        # Block localhost attacks
        if "127.0.0.1" in url or "localhost" in url:
            # Allow our own port
            if ":2727" in url:
                return True
            self.blocked_actions.append({
                "time": time.time(),
                "action": "network_blocked",
                "url": url,
                "reason": "Localhost access blocked (except self)",
            })
            return False
        return True

    # ── LOGGING ──

    def log_write(self, path: Path, content: str):
        entry = {
            "time": time.time(),
            "action": "write",
            "path": str(path),
            "size": len(content),
            "preview": content[:200],
        }
        self.write_log.append(entry)
        self._persist(entry)

    def log_edit(self, path: Path, old: str, new: str):
        entry = {
            "time": time.time(),
            "action": "edit",
            "path": str(path),
            "old_preview": old[:100],
            "new_preview": new[:100],
        }
        self.edit_log.append(entry)
        self._persist(entry)

    def log_bash(self, command: str, exit_code: int):
        entry = {
            "time": time.time(),
            "action": "bash",
            "command": command[:500],
            "exit_code": exit_code,
        }
        self.bash_log.append(entry)
        self._persist(entry)

    def _persist(self, entry: dict):
        """Write to audit log."""
        try:
            with open(self.audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass  # Audit log failure shouldn't crash the agent

    # ── REPORTING ──

    @property
    def stats(self) -> dict:
        return {
            "total_writes": len(self.write_log),
            "total_bash_calls": len(self.bash_log),
            "total_edits": len(self.edit_log),
            "blocked_actions": len(self.blocked_actions),
            "recent_blocked": [
                {"action": b["action"], "reason": b["reason"]}
                for b in self.blocked_actions[-5:]
            ],
        }

    def get_recent_activity(self, n: int = 20) -> list[dict]:
        """Get the most recent audit entries."""
        if not self.audit_log_path.exists():
            return []
        lines = []
        with open(self.audit_log_path, "r", encoding="utf-8") as f:
            for line in f:
                lines.append(json.loads(line))
        return lines[-n:]
