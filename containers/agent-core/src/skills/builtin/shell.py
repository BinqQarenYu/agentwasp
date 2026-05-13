import asyncio
import re
import uuid
from datetime import datetime, timezone

import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()

MAX_OUTPUT_CHARS = 8000
DEFAULT_TIMEOUT = 60

_REDACT_RE = None


def _redact_command(cmd: str) -> str:
    """Redact secret-like tokens from shell commands before audit logging."""
    global _REDACT_RE
    if _REDACT_RE is None:
        import re
        _REDACT_RE = re.compile(
            r'(sk-[A-Za-z0-9\-_]{20,}|AIza[A-Za-z0-9\-_]{30,}|'
            r'xai-[A-Za-z0-9\-_]{20,}|hf_[A-Za-z0-9]{20,}|'
            r'(?:password|passwd|token|secret|key)\s*[=:]\s*\S+)',
            re.IGNORECASE,
        )
    return _REDACT_RE.sub("[REDACTED]", cmd)


# ── Command classification: SAFE / SENSITIVE / DANGEROUS ─────────────────────
# DANGEROUS: hard-block.  Operations that escape the container, modify the
# host kernel/filesystem, escalate privilege, or destroy data outside /data.
# SENSITIVE: allow but flag in audit log.  Modifies state inside /data.
# SAFE: pure-read or trivially safe.
#
# All matching is permissive (substring/regex), not allowlist.  This keeps
# shell useful for ad-hoc admin work while denying obvious destructive paths.

_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Privilege escalation
    (re.compile(r"\bsudo\b|\bsu\s+-|\bsu\s+root\b|\bdoas\b"),
     "privilege escalation (sudo/su/doas)"),
    # Host control
    (re.compile(r"\b(?:reboot|shutdown|halt|poweroff|init\s+[06])\b"),
     "host power-state command"),
    # Disk destruction
    (re.compile(r"\b(?:mkfs|mke2fs|wipefs|sgdisk|fdisk|parted)\b"),
     "filesystem destruction (mkfs/wipefs/fdisk)"),
    (re.compile(r"\bdd\s+.*\bof\s*=\s*/dev/"),
     "raw block device write (dd → /dev/...)"),
    # Recursive nuke of system roots
    (re.compile(r"\brm\s+(?:-[a-zA-Z]*r[a-zA-Z]*\s+|-[a-zA-Z]*f[a-zA-Z]*\s+).*(?:^|\s)/(?:\s|$|\*)"),
     "rm -rf on system root"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r"),
     "rm -rf — must target paths inside /data only"),
    # Direct kernel/process attacks
    (re.compile(r"\bkill\s+-9\s+1\b|\bkillall\s+-9\b"),
     "kill PID 1 / killall -9"),
    (re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*}\s*;\s*:"),
     "fork bomb"),
    # Kernel/system file rewrites
    (re.compile(r"(?:>|>>)\s*/(?:etc|boot|proc|sys|lib|sbin|bin|usr|root)"),
     "redirect into system path"),
    (re.compile(r"\b(?:chattr|setfattr|setcap)\b"),
     "extended attribute / capability modification"),
    # Network execution / arbitrary download-and-run
    (re.compile(r"\bnc\s+-e\b|\bncat\s+-e\b"),
     "netcat -e (remote shell)"),
    (re.compile(r"(?:curl|wget)\s[^|;&\n]*\|\s*(?:bash|sh|zsh|python\d?|perl|ruby|node)\b"),
     "pipe-to-shell from network"),
    (re.compile(r"\bbash\s+-c\s+['\"][^'\"]*\$\(\s*(?:curl|wget)"),
     "bash -c with embedded network fetch"),
    # ── Indirect command composition — micro-hardening ──────────────────────
    # bash builtin `eval` parses+executes a string; almost always a code-injection
    # vector when called from an LLM context.
    (re.compile(r"(?:^|[\s;&|`(])eval\s+"),
     "shell eval (dynamic command parsing/execution)"),
    # Pipe arbitrary content into ANY interpreter (broader than just network):
    # catches `cat exploit.sh | bash`, `echo '...' | sh`, `printf | python -c`,
    # `... | sudo sh`, etc.  Permissive on read-only pipelines (grep|wc, etc).
    (re.compile(
        r"\|\s*(?:sudo\s+)?(?:bash|sh|zsh|dash|ksh|csh|tcsh|fish)\b\s*"
        r"(?:-c\b|-s\b|--?login\b|$|;|&|\||\n)"),
     "pipe into shell interpreter"),
    (re.compile(r"\|\s*(?:python\d?|perl|ruby|node|lua|php|pwsh|powershell)\s+-c?e?\b"),
     "pipe into language interpreter executing inline code"),
    # Process substitution — `bash <(curl ...)`, `source <(...)`, `>(bash)`
    (re.compile(r"<\s*\(\s*(?:curl|wget|fetch)\b"),
     "process substitution sourcing from network"),
    (re.compile(r"(?:bash|sh|zsh|source|\.\s)\s+<\s*\(\s*(?:curl|wget)"),
     "shell sourcing process substitution from network"),
    (re.compile(r">\s*\(\s*(?:bash|sh|zsh|python\d?|perl|ruby|node)\b"),
     "redirect into process substitution running interpreter"),
    # Command substitution that fetches from network
    (re.compile(r"\$\(\s*(?:curl|wget|fetch)\b[^)]*\)|`\s*(?:curl|wget|fetch)\b[^`]*`"),
     "command substitution fetching from network"),
    # bash -c / sh -c whose argument contains a destructive verb (caught
    # transitively, but adds an explicit reason for clarity in audit logs)
    (re.compile(r"\b(?:bash|sh|zsh)\s+-c\s+['\"][^'\"]*\b(?:rm\s+-[a-zA-Z]*[rf]|mkfs|dd\s+.*of=/dev|chmod\s+(?:[ug]?\+s|[24]\d{3}|6\d{3}))\b"),
     "interpreter -c wrapping a destructive command"),
    # source/. with a network URL or remote-only target
    (re.compile(r"\b(?:source|\.\s)\s+(?:https?://|<\(|/dev/(?:tcp|udp)/)"),
     "source from network or remote stream"),
    # /dev/tcp / /dev/udp — bash builtin networking
    (re.compile(r"/dev/(?:tcp|udp)/"),
     "bash /dev/tcp,/dev/udp networking"),
    # exec replacing the shell with another command (escape vector)
    (re.compile(r"^\s*exec\s+(?!2>|>|&>|<)[a-zA-Z./]"),
     "exec replacing shell with another binary"),
    # LD_PRELOAD / LD_LIBRARY_PATH injection
    (re.compile(r"\b(?:LD_PRELOAD|LD_LIBRARY_PATH|LD_AUDIT)\s*="),
     "dynamic linker environment injection"),
    # Cron / persistence
    (re.compile(r"(?:>|>>)\s*/(?:etc/cron|var/spool/cron)"),
     "cron persistence write"),
    # Docker socket / container escape
    (re.compile(r"/var/run/docker\.sock|\bdocker\s+(?:run|exec|cp)\s"),
     "docker socket / container manipulation"),
    # Capability bits
    (re.compile(r"\bchmod\s+(?:[ug]?\+s|[24]\d{3}|6\d{3})\b"),
     "setuid/setgid bit modification"),
    # Outbound to internal metadata services (defense in depth — http_request blocks too)
    (re.compile(r"\b169\.254\.169\.254\b|metadata\.google\.internal"),
     "cloud metadata endpoint access"),
]

_SENSITIVE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(?:rm|mv|cp|ln)\b"),
    re.compile(r"\bmkdir\b|\brmdir\b|\btouch\b"),
    re.compile(r"\bchmod\b|\bchown\b|\bchgrp\b"),
    re.compile(r"(?:>|>>)\s*\S"),                # any redirection
    re.compile(r"\btee\b"),
    re.compile(r"\bgit\s+(?:reset|clean|push|checkout\s+--)\b"),
    re.compile(r"\bpip(?:3)?\s+(?:install|uninstall)\b"),
    re.compile(r"\bnpm\s+(?:install|uninstall)\b"),
    re.compile(r"\bapt(?:-get)?\s+(?:install|remove|purge)\b"),
    re.compile(r"\bdocker\s+(?:rm|rmi|stop|kill|restart)\b"),
    # Indirect-execution composition — flag for visibility, allow when no
    # DANGEROUS pattern was matched.  Plain `bash -c "ls"` is allowed but
    # logged as SENSITIVE because the `-c` form is a frequent vector.
    re.compile(r"\b(?:bash|sh|zsh|dash)\s+-c\b"),
    re.compile(r"\bxargs\s+(?:bash|sh|-I)"),     # xargs piping into shell
]


def _classify_command(command: str) -> tuple[str, str]:
    """Return (level, reason).

    level = "DANGEROUS" | "SENSITIVE" | "SAFE"
    reason = empty for SAFE; pattern explanation otherwise.
    """
    if not command or not command.strip():
        return "SAFE", ""
    for pat, reason in _DANGEROUS_PATTERNS:
        if pat.search(command):
            return "DANGEROUS", reason
    for pat in _SENSITIVE_PATTERNS:
        if pat.search(command):
            return "SENSITIVE", "state-modifying command"
    return "SAFE", ""


async def _audit_shell(
    command: str,
    exit_code: int,
    error: str,
    goal_id: str = "",
    classification: str = "SAFE",
    block_reason: str = "",
) -> None:
    """Fire-and-forget: write shell invocation to AuditLog with classification."""
    try:
        from ...db.session import async_session
        from ...db.models import AuditLog
        safe_cmd = _redact_command(command)
        # Use event_type to surface classification in dashboards/queries
        if classification == "DANGEROUS":
            event_type = "skill.shell.blocked"
        elif classification == "SENSITIVE":
            event_type = "skill.shell.sensitive"
        else:
            event_type = "skill.shell"
        out = f"class={classification} exit={exit_code}"
        if goal_id:
            out += f" goal={goal_id}"
        if block_reason:
            out += f" reason={block_reason[:80]}"
        async with async_session() as session:
            session.add(AuditLog(
                id=str(uuid.uuid4()),
                event_type=event_type,
                source="skill",
                action="skill.shell",
                timestamp=datetime.now(timezone.utc),
                input_summary=safe_cmd[:400],
                output_summary=out[:400],
                user_id="",
                chat_id="",
                latency_ms=0,
                error=error[:200] if error else None,
            ))
            await session.commit()
    except Exception:
        pass


class ShellSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="shell",
            description="Execute a shell command and return stdout+stderr.",
            params=[
                SkillParam(
                    name="command",
                    param_type=ParamType.STRING,
                    description="Bash command to execute",
                ),
                SkillParam(
                    name="timeout",
                    param_type=ParamType.INTEGER,
                    description="Timeout in seconds (max 120)",
                    required=False,
                    default=str(DEFAULT_TIMEOUT),
                ),
            ],
            category="system",
            timeout_seconds=120.0,
        )

    async def execute(self, command: str, timeout: str = str(DEFAULT_TIMEOUT), **kwargs) -> SkillResult:
        timeout_s = min(int(timeout), 120)
        goal_id = str(kwargs.get("goal_id", "") or "")

        # ── Classification gate ──────────────────────────────────────────────
        level, reason = _classify_command(command)
        if level == "DANGEROUS":
            logger.warning(
                "shell.blocked_dangerous",
                command_preview=_redact_command(command)[:200],
                reason=reason,
                goal_id=goal_id[:8],
            )
            asyncio.ensure_future(_audit_shell(
                command, -1, f"blocked: {reason}",
                goal_id=goal_id, classification="DANGEROUS", block_reason=reason,
            ))
            return SkillResult(
                skill_name="shell",
                success=False,
                output="",
                error=(
                    f"⛔ Shell command blocked (DANGEROUS): {reason}. "
                    "If you need this functionality, use a higher-level skill or reformulate. "
                    "If this is a legitimate admin task, the operator must run it manually."
                ),
            )

        if level == "SENSITIVE":
            logger.info(
                "shell.sensitive",
                command_preview=_redact_command(command)[:200],
                goal_id=goal_id[:8],
            )

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd="/data",
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                asyncio.ensure_future(_audit_shell(
                    command, -1, f"timeout after {timeout_s}s",
                    goal_id, classification=level,
                ))
                return SkillResult(
                    skill_name="shell",
                    success=False,
                    output="",
                    error=f"Command timed out after {timeout_s}s",
                )

            output = stdout.decode("utf-8", errors="replace")
            exit_code = proc.returncode

            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(output)} total chars)"

            err_str = "" if exit_code == 0 else f"Exit code: {exit_code}"
            asyncio.ensure_future(_audit_shell(
                command, exit_code, err_str,
                goal_id, classification=level,
            ))
            return SkillResult(
                skill_name="shell",
                success=exit_code == 0,
                output=f"[exit {exit_code}]\n{output}".strip(),
                error=err_str,
            )
        except Exception as e:
            logger.exception("shell.execute_error", command=command[:100])
            asyncio.ensure_future(_audit_shell(
                command, -1, str(e)[:200],
                goal_id, classification=level,
            ))
            return SkillResult(skill_name="shell", success=False, output="", error=str(e))
