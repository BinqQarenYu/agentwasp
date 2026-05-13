import ast
import re

import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult
from .sandbox import execute_sandboxed

logger = structlog.get_logger()

MAX_OUTPUT_CHARS = 8000
DEFAULT_TIMEOUT = 30   # Phase 6: reduced to match sandbox default

# ── CRIT-4: Static code safety scanner (two-layer: regex + AST) ──────────────
# Layer 1 — Regex patterns: fast; catches textual patterns and obfuscated variants.
# Layer 2 — AST structural analysis: catches import variants and builtin calls
#           that regex cannot reliably distinguish (e.g. from X import Y).
#
# Design: fail-closed on any match — block and explain; allow on clean pass.
# The LLM receives the specific reason so it can reformulate safely.

_PYEXEC_BLOCKED_PATTERNS: list[tuple[str, str]] = [
    # Raw socket creation — bypasses domain lock and SSRF checks entirely
    (
        r"\bimport\s+socket\b|socket\s*\.\s*(?:socket|connect|create_connection)\s*\("
        r"|__import__\s*\(\s*['\"]socket['\"]",
        "raw socket usage (socket module) — bypasses network controls",
    ),
    # subprocess spawning — arbitrary shell execution within Python
    (
        r"\bimport\s+subprocess\b"
        r"|from\s+subprocess\s+import\b"
        r"|subprocess\s*\.\s*(?:run|Popen|call|check_output|getoutput|getstatusoutput)\s*\("
        r"|__import__\s*\(\s*['\"]subprocess['\"]",
        "subprocess spawning — arbitrary shell execution",
    ),
    # multiprocessing — process spawning that escapes Python-level import restrictions
    (
        r"\bimport\s+multiprocessing\b"
        r"|from\s+multiprocessing\s+import\b"
        r"|__import__\s*\(\s*['\"]multiprocessing['\"]",
        "multiprocessing import — process spawning bypasses sandbox",
    ),
    # os shell-exec calls — direct shell invocation via os module
    (
        r"\bos\s*\.\s*(?:system|popen|execve|execvp|execl|execle|execlp"
        r"|spawnl|spawnle|spawnv|spawnve|spawnlp|spawnlpe)\s*\(",
        "os shell-exec call (os.system / os.popen / os.exec* / os.spawn*)",
    ),
    # from os import [dangerous shell-exec functions]
    (
        r"from\s+os\s+import\s+(?:[*]|.*\b(?:system|popen|execve|execvp|execl"
        r"|execle|execlp|spawnl|spawnle|spawnv|spawnve|spawnlp|spawnlpe)\b)",
        "from os import dangerous shell-exec function",
    ),
    # pty — interactive pseudo-terminal spawning
    (
        r"\bpty\s*\.\s*(?:spawn|openpty)\s*\("
        r"|\bimport\s+pty\b"
        r"|from\s+pty\s+import\b"
        r"|__import__\s*\(\s*['\"]pty['\"]",
        "pty pseudo-terminal spawning",
    ),
    # exec / eval / compile builtins — dynamic execution / static scanner bypass
    # Negative lookbehind (?<!\.) prevents matching method calls (.eval(), .compile())
    (
        r"(?<!\.)\bexec\s*\("
        r"|(?<!\.)\beval\s*\("
        r"|(?<!\.)\bcompile\s*\(",
        "dynamic execution (exec/eval/compile) — sandbox bypass vector",
    ),
    # importlib.import_module() of blocked modules
    (
        r"importlib\s*\.\s*import_module\s*\(\s*['\"]"
        r"(?:subprocess|multiprocessing|pty|socket|ssl|ctypes|cffi|_ctypes|os)\b",
        "importlib.import_module() of blocked module",
    ),
    # Sensitive filesystem paths — credentials, source code, config
    (
        r"['\"](?:/data/config/|/data/apikeys|/data/src_patches/)[^'\"]*['\"]"
        r"|open\s*\(\s*['\"](?:/data/config/|/data/apikeys)",
        "access to sensitive filesystem path (/data/config/, /data/apikeys)",
    ),
    # ctypes — C-level FFI; can call dlopen/mmap to escape Python-level sandbox
    (
        r"\bimport\s+ctypes\b"
        r"|__import__\s*\(\s*['\"]ctypes['\"]"
        r"|ctypes\s*\.\s*(?:CDLL|cdll|windll|pythonapi|WinDLL|OleDLL|util)\b"
        r"|ctypes\s*\.\s*(?:CDLL|cdll|windll|pythonapi)\s*\(",
        "ctypes usage — C-level sandbox escape vector",
    ),
    # cffi — C FFI; same escape risk as ctypes
    (
        r"\bimport\s+cffi\b"
        r"|__import__\s*\(\s*['\"]cffi['\"]"
        r"|from\s+cffi\s+import"
        r"|\bFFI\s*\(\s*\)"
        r"|ffi\s*\.\s*dlopen\s*\(",
        "cffi usage — C FFI sandbox escape vector",
    ),
]

# Compile once at module load
_PYEXEC_BLOCKED_RE: list[tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE), reason)
    for pat, reason in _PYEXEC_BLOCKED_PATTERNS
]

# ── AST-level scanner constants ───────────────────────────────────────────────
# Complete set of modules that may never be imported inside python_exec.
# Mirrors sandbox._BLOCKED_NETWORK_MODULES so scan-time and run-time agree.
_BLOCKED_AST_MODULES: frozenset[str] = frozenset({
    # process spawning
    "subprocess", "multiprocessing", "pty",
    "_posixsubprocess", "_subprocess",
    # raw network
    "socket", "ssl", "_ssl",
    # C-level escape vectors
    "ctypes", "_ctypes", "cffi", "_cffi_backend",
    # HTTP / network high-level
    "http", "http.client", "http.server",
    "urllib", "urllib.request", "urllib.parse",
    "ftplib", "smtplib", "imaplib", "poplib",
    "telnetlib", "nntplib", "xmlrpc",
    "requests", "httpx", "aiohttp", "httplib2",
    "paramiko", "fabric", "boto", "boto3",
})

# SSRF target patterns — URLs/IPs that must never appear in user code,
# even if the imports were going to fail. Hard-fail at scan time so the
# agent's response binding can see "security_violation" instead of a
# generic "Exit code 1" and accurately tell the user the request was blocked.
_PYEXEC_SSRF_TARGET_RE = re.compile(
    r"(?:"
    r"(?:https?|ftp|file|gopher)://(?:localhost|127\.0\.0\.1|169\.254\."
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|0\.0\.0\.0|::1|\[::1\]|fc00:|fd00:|fe80:|metadata\.google\.internal)"
    r"|(?:^|[^\w])(?:localhost|127\.0\.0\.1|169\.254\.\d+\.\d+"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})"
    r")",
    re.IGNORECASE,
)

# os module functions that must not be imported via 'from os import <fn>'
_BLOCKED_OS_FUNCTIONS: frozenset[str] = frozenset({
    "system", "popen", "execve", "execvp", "execl", "execle", "execlp",
    "spawnl", "spawnle", "spawnv", "spawnve", "spawnlp", "spawnlpe",
})


def _scan_code_ast(code: str) -> tuple[bool, str]:
    """AST-level structural scan for dangerous imports and dynamic execution.

    Returns (safe: bool, reason: str).
    Fail-open on SyntaxError — malformed code will fail at execution time.
    Fail-closed on all detected dangerous AST patterns.

    Checks:
      1. import <blocked_module>            (ast.Import)
      2. from <blocked_module> import ...   (ast.ImportFrom)
      3. from os import <dangerous_fn>      (ast.ImportFrom on 'os')
      4. exec(...) / eval(...) / compile()  (ast.Call on Name node)
      5. __import__(<blocked_module>)       (ast.Call on '__import__' Name)
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return True, ""   # Syntax errors surface at execution; not a security bypass

    for node in ast.walk(tree):
        # ── Import statements ──────────────────────────────────────────────────
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _BLOCKED_AST_MODULES:
                    return False, f"import {alias.name!r} — blocked module"

        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module in _BLOCKED_AST_MODULES:
                return False, f"from {node.module!r} import — blocked module"
            if module == "os":
                for alias in node.names:
                    if alias.name == "*" or alias.name in _BLOCKED_OS_FUNCTIONS:
                        return False, f"from os import {alias.name!r} — shell exec function"

        # ── Dangerous builtin calls ────────────────────────────────────────────
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                fn = node.func.id

                # exec() / eval() / compile() — dynamic execution
                if fn in ("exec", "eval", "compile"):
                    return False, f"{fn}() — dynamic execution blocked"

                # __import__("blocked_module")
                if fn == "__import__" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        top = first.value.split(".")[0]
                        if top in _BLOCKED_AST_MODULES:
                            return False, f"__import__({first.value!r}) — blocked module"

    return True, ""


def _scan_code_safety(code: str) -> tuple[bool, str]:
    """Scan Python code for dangerous patterns before execution.

    Three-layer scan:
      Layer 0 — SSRF target literals (RFC-1918, loopback, link-local, metadata).
      Layer 1 — Regex: fast, catches textual/obfuscated patterns.
      Layer 2 — AST:   structural, catches import variants and builtin calls.

    Returns (safe: bool, reason: str).
    safe=True  → all layers passed; safe to proceed.
    safe=False → blocked pattern found; reason identifies the violation.
    """
    # Layer 0: SSRF target literals — even if imports would fail, hardcoded
    # internal targets are an intent-to-attack signal. Block before sandbox.
    if _PYEXEC_SSRF_TARGET_RE.search(code):
        return False, "SSRF target — internal/loopback/RFC-1918/metadata host blocked"
    # Layer 1: regex scan
    for pattern, reason in _PYEXEC_BLOCKED_RE:
        if pattern.search(code):
            return False, reason
    # Layer 2: AST structural scan
    return _scan_code_ast(code)


class PythonExecSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="python_exec",
            description="Execute a Python code snippet and return stdout.",
            params=[
                SkillParam(
                    name="code",
                    param_type=ParamType.STRING,
                    description="Python code to execute",
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

    async def execute(self, code: str, timeout: str = str(DEFAULT_TIMEOUT), **kwargs) -> SkillResult:
        timeout_s = min(int(timeout), 120)

        # ── Static safety scan before execution ───────────────────────────────
        _safe, _reason = _scan_code_safety(code)
        if not _safe:
            # Phase 2: classify network/SSRF blocks separately so honesty-layer
            # downstream can attribute correctly and audit_log distinguishes
            # them from generic syntax/library blocks.
            _is_security = (
                "SSRF" in _reason
                or "blocked module" in _reason
                or "shell exec" in _reason
                or "raw socket" in _reason
                or "dynamic execution" in _reason
            )
            logger.warning(
                "python_exec.security_violation" if _is_security else "python_exec.blocked",
                reason=_reason,
                code_snippet=code[:150],
            )
            # Phase 3 metrics — best-effort, never raises.
            if _is_security:
                try:
                    import os as _os
                    _redis_url = _os.environ.get("REDIS_URL", "")
                    if _redis_url:
                        from ...observability.truth_metrics import bump as _bump_sec
                        await _bump_sec(_redis_url, "python_exec_security_violation")
                except Exception:
                    pass
            return SkillResult(
                skill_name="python_exec",
                success=False,
                output="",
                error=(
                    f"⛔ Code execution blocked: {_reason}. "
                    "Use higher-level skills (http_request, fetch_url, web_search) "
                    "for network access, or reformulate to avoid the blocked construct."
                ),
            )
        # ── End CRIT-4 ────────────────────────────────────────────────────────

        # ── Phase 6.1: Route through runtime sandbox ──────────────────────────
        # execute_sandboxed() enforces: import blocker, open() patcher,
        # resource limits, clean env, isolated cwd, timeout+kill, fail-closed.
        result = await execute_sandboxed(code, timeout_s=timeout_s, max_output=MAX_OUTPUT_CHARS)

        if result.blocked:
            return SkillResult(
                skill_name="python_exec",
                success=False,
                output="",
                error=result.error,
            )
        if result.timed_out:
            return SkillResult(
                skill_name="python_exec",
                success=False,
                output="",
                error=result.error,
            )
        return SkillResult(
            skill_name="python_exec",
            success=result.exit_code == 0,
            output=result.output,
            error=result.error,
        )
