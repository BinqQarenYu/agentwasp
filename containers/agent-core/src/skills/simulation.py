"""Risk Assessor — pre-execution safety analysis for RESTRICTED/PRIVILEGED skills.

This is a WARN-only layer; it never blocks execution by default.
The capability policy (memory/policy/) can escalate it to block.

Risk levels:
  LOW    — standard operation
  MEDIUM — potentially destructive / irreversible, but recoverable
  HIGH   — likely destructive, data loss risk, or system integrity impact

Assessment is pattern-based, not LLM-based (fast, deterministic, no cost).
"""

import re
from dataclasses import dataclass
from enum import Enum


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class RiskAssessment:
    level: RiskLevel
    reasons: list[str]
    skill_name: str
    input_summary: str

    def is_high(self) -> bool:
        return self.level == RiskLevel.HIGH

    def summary(self) -> str:
        if not self.reasons:
            return f"[{self.level.value}] No risk factors detected."
        return f"[{self.level.value}] " + "; ".join(self.reasons)


# --- Shell command risk patterns ---

_SHELL_HIGH = [
    (r"\brm\s+-[rRf]{1,3}\b", "recursive/force delete"),
    (r"\bdd\s+if=", "raw disk write (dd)"),
    (r"\bmkfs\b", "filesystem format"),
    (r">\s*/dev/[sh]d[a-z]", "raw device overwrite"),
    (r"\bshred\b", "secure file deletion"),
    (r":\(\)\{.*\}", "fork bomb pattern"),
    (r"\bchmod\s+[0-7]*7[0-7]{2}\b", "world-writable chmod"),
    (r"\bdrop\s+table\b", "SQL table drop"),
    (r"\btruncate\b.*table", "SQL table truncate"),
    (r"\bkillall\b", "mass process kill"),
    (r"\biptables\s+-F\b", "flush all firewall rules"),
    (r">\s*/etc/(passwd|shadow|sudoers)", "overwrite system auth files"),
]

_SHELL_MEDIUM = [
    (r"\bcurl\b.*\|\s*(bash|sh|python)\b", "pipe remote code to shell"),
    (r"\bwget\b.*-O\s*-\b.*\|\s*(bash|sh)\b", "pipe remote code to shell"),
    (r"\bapt(-get)?\s+remove\b", "package removal"),
    (r"\bpip\s+uninstall\b", "package removal"),
    (r"\bsystemctl\s+(stop|disable|mask)\b", "service stop/disable"),
    (r"\bdocker\s+rm\b", "container removal"),
    (r"\bdocker\s+rmi\b", "image removal"),
    (r"\bsudo\b", "privilege escalation"),
    (r">\s*/data/\w+", "data directory overwrite"),
]

# --- Python code risk patterns ---

_PYTHON_HIGH = [
    (r"os\.system\s*\(", "os.system call"),
    (r"subprocess\.\w+\s*\(.*shell\s*=\s*True", "subprocess with shell=True"),
    (r"shutil\.rmtree\s*\(", "recursive directory deletion"),
    (r"os\.remove\s*\(.*/(etc|bin|usr|lib)", "system file deletion"),
    (r"__import__\s*\(['\"]os['\"]", "dynamic os import"),
    (r"eval\s*\(", "eval() call"),
    (r"exec\s*\(", "exec() call"),
]

_PYTHON_MEDIUM = [
    (r"open\s*\(.*['\"]w['\"].*\).*/(etc|bin|usr)", "system file write"),
    (r"requests\.(post|put|delete|patch)\s*\(", "mutating HTTP from Python"),
    (r"socket\.\w+\s*\(", "raw socket"),
    (r"importlib\.", "dynamic import"),
]


def _check_patterns(text: str, high_patterns, medium_patterns):
    """Returns (high_reasons, medium_reasons, RiskLevel)."""
    reasons_high = []
    reasons_medium = []
    text_lower = text.lower()

    for pattern, reason in high_patterns:
        if re.search(pattern, text_lower, re.IGNORECASE | re.DOTALL):
            reasons_high.append(reason)

    for pattern, reason in medium_patterns:
        if re.search(pattern, text_lower, re.IGNORECASE | re.DOTALL):
            reasons_medium.append(reason)

    if reasons_high:
        return reasons_high, reasons_medium, RiskLevel.HIGH
    if reasons_medium:
        return [], reasons_medium, RiskLevel.MEDIUM
    return [], [], RiskLevel.LOW


class RiskAssessor:
    """Stateless risk assessor for skill arguments."""

    def assess(self, skill_name: str, arguments: dict) -> RiskAssessment:
        """Assess risk of a skill call before execution.

        Returns a RiskAssessment; never raises.
        """
        try:
            return self._assess(skill_name, arguments)
        except Exception:
            return RiskAssessment(
                level=RiskLevel.LOW,
                reasons=[],
                skill_name=skill_name,
                input_summary=str(arguments)[:100],
            )

    def _assess(self, skill_name: str, arguments: dict) -> RiskAssessment:
        level = RiskLevel.LOW
        reasons: list[str] = []
        input_summary = str(arguments)[:200]

        if skill_name == "shell":
            command = arguments.get("command", "")
            high_r, med_r, detected_level = _check_patterns(
                command, _SHELL_HIGH, _SHELL_MEDIUM
            )
            reasons = high_r + med_r
            level = detected_level

        elif skill_name == "python_exec":
            code = arguments.get("code", "")
            high_r, med_r, detected_level = _check_patterns(
                code, _PYTHON_HIGH, _PYTHON_MEDIUM
            )
            reasons = high_r + med_r
            level = detected_level

        elif skill_name == "http_request":
            method = arguments.get("method", "GET").upper()
            url = arguments.get("url", "")
            # DELETE/PUT to internal addresses is medium risk
            if method in ("DELETE", "PUT", "PATCH"):
                if re.search(r"(localhost|127\.|172\.|10\.|192\.168\.)", url):
                    reasons.append(f"mutating {method} to internal address")
                    level = RiskLevel.MEDIUM
            if method == "DELETE":
                if not re.search(r"(localhost|127\.|172\.|10\.|192\.168\.)", url):
                    reasons.append("DELETE request to external URL")
                    level = RiskLevel.MEDIUM

        elif skill_name in ("read_file", "write_file"):
            path = arguments.get("path", arguments.get("file_path", ""))
            if re.search(r"/(etc|bin|usr|lib|sbin|boot)", path):
                reasons.append(f"access to system path: {path}")
                level = RiskLevel.HIGH
            elif skill_name == "write_file" and "/data/" not in path:
                reasons.append(f"write outside /data/: {path}")
                level = RiskLevel.MEDIUM

        return RiskAssessment(
            level=level,
            reasons=reasons,
            skill_name=skill_name,
            input_summary=input_summary,
        )


# Singleton assessor
risk_assessor = RiskAssessor()
