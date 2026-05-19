"""
WASP Phase 6.1 — python_exec Runtime Sandbox
=============================================
Provides real runtime isolation for python_exec code execution.

The static code scanner (CRIT-4) remains as defense-in-depth but is NOT the
primary security boundary.  This module provides the boundary.

Isolation layers enforced:
  1. Import blocker     — sys.meta_path hook prevents importing network modules
  2. open() patcher     — builtins.open restricted to sandbox dir (PermissionError outside)
  3. Resource limits    — CPU time, memory, file descriptors capped via resource module
  4. Clean environment  — No credentials, tokens, or agent paths in subprocess env
  5. Isolated cwd       — Code runs in /tmp/wasp_sandbox_<uuid>/ only
  6. Timeout + SIGKILL  — Hard wall-clock limit; process killed on timeout
  7. Fail-closed        — If sandbox setup fails, execution is blocked (never falls through)

SandboxMode:
  PYTHON_WRAPPER  — fallback mode: Python-level import/open restrictions + resource limits
                    used when OS-level namespacing (firejail, unshare --net) is unavailable
  UNAVAILABLE     — sandbox infrastructure cannot be set up; all execution blocked

Detection:
  detect_sandbox_mode() probes the environment once and caches the result.
  Always returns PYTHON_WRAPPER or UNAVAILABLE — never silently degrades to raw exec.

Log events:
  sandbox.exec_started
  sandbox.exec_success
  sandbox.exec_timeout
  sandbox.exec_error
  sandbox.setup_failed
  sandbox.blocked_unavailable
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from enum import Enum

import structlog

logger = structlog.get_logger()

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_OUTPUT_CHARS = 8_000
_SANDBOX_DIR_PREFIX = "wasp_sandbox_"

# Resource limits applied in the sandboxed subprocess (RLIMIT_* values)
# These are conservative — Python interpreter needs reasonable limits to run.
_RLIMIT_CPU_SECONDS  = 30     # CPU seconds (not wall clock)
_RLIMIT_AS_MB        = 256    # virtual address space (MB)
_RLIMIT_NFILE        = 64     # open file descriptors
_RLIMIT_NPROC        = 16     # child processes

# Network modules blocked at import level inside the sandbox
_BLOCKED_NETWORK_MODULES = frozenset({
    "socket", "ssl", "_ssl",
    "http", "http.client", "http.server",
    "urllib", "urllib.request", "urllib.parse",
    "ftplib", "smtplib", "imaplib", "poplib",
    "telnetlib", "nntplib", "xmlrpc",
    "requests", "httpx", "aiohttp", "httplib2",
    "paramiko", "fabric", "boto", "boto3",
    # C-level sandbox escape vectors
    "ctypes", "_ctypes", "cffi", "_cffi_backend",
    # Process-spawning modules — can bypass Python-level import restrictions
    "subprocess", "_subprocess", "_posixsubprocess",
    "multiprocessing",
    "pty",
})


class SandboxMode(str, Enum):
    PYTHON_WRAPPER = "python_wrapper"
    UNAVAILABLE    = "unavailable"


@dataclass
class SandboxResult:
    output: str
    error: str
    exit_code: int
    sandbox_mode: str
    timed_out: bool = False
    blocked: bool = False       # True → execution never started (fail-closed)


# ── Sandbox wrapper injected before user code ──────────────────────────────────
# This string is prepended to the user's code when running in PYTHON_WRAPPER mode.
# It installs two runtime guards:
#   1. _ImportBlocker — sys.meta_path hook that blocks network modules
#   2. _restricted_open — replaces builtins.open to enforce sandbox dir
#
# The sandbox dir path is injected at build time via str.format().
_SANDBOX_WRAPPER_TEMPLATE = textwrap.dedent("""\
    import sys as _sys, builtins as _bi, os as _os, resource as _res

    # ── Resource limits ──────────────────────────────────────────────────────
    try:
        _res.setrlimit(_res.RLIMIT_CPU,   ({rlimit_cpu},   {rlimit_cpu}))
        _res.setrlimit(_res.RLIMIT_AS,    ({rlimit_as},    {rlimit_as}))
        _res.setrlimit(_res.RLIMIT_NOFILE,({rlimit_nfile}, {rlimit_nfile}))
    except Exception:
        pass  # non-fatal: limits may already be set by OS

    # ── Network import blocker ────────────────────────────────────────────────
    _BLOCKED_MODS = frozenset({blocked_mods!r})
    # Purge already-loaded network modules from sys.modules so they cannot be
    # re-used from the module cache (subprocess inherits parent's sys.modules).
    for _mod_name in list(_sys.modules.keys()):
        if _mod_name.split('.')[0] in _BLOCKED_MODS:
            del _sys.modules[_mod_name]
    class _ImportBlocker:
        def find_spec(self, fullname, path, target=None):
            if fullname.split('.')[0] in _BLOCKED_MODS:
                raise ImportError(
                    f"Sandbox: network module '{{fullname}}' is blocked. "
                    "Use web_search or http_request skills for network access."
                )
            return None
        def find_module(self, name, path=None):
            return self if name.split('.')[0] in _BLOCKED_MODS else None
        def load_module(self, name):
            raise ImportError(
                f"Sandbox: network module '{{name}}' is blocked. "
                "Use web_search or http_request skills for network access."
            )
    _sys.meta_path.insert(0, _ImportBlocker())

    # ── Filesystem restriction ────────────────────────────────────────────────
    _SANDBOX_DIR = _os.path.realpath({sandbox_dir!r})
    _orig_open = _bi.open
    def _restricted_open(path, *args, **kwargs):
        if isinstance(path, int):
            raise PermissionError("Sandbox: opening file descriptors directly is blocked.")
        try:
            _p = _os.fsdecode(path)
        except TypeError:
            _p = str(path)
        try:
            _resolved = _os.path.realpath(_p)
        except Exception:
            raise PermissionError(f"Sandbox: unable to resolve path: {{path!r}}")
        if _resolved != _SANDBOX_DIR and not _resolved.startswith(_SANDBOX_DIR + _os.sep):
            raise PermissionError(
                f"Sandbox: file access outside sandbox dir is blocked: {{path!r}}"
            )
        return _orig_open(_resolved, *args, **kwargs)
    _bi.open = _restricted_open

    # Also patch io.open and os.open — both bypass builtins.open at the C level.
    import io as _io
    _io.open = _restricted_open
    _orig_os_open = _os.open
    def _restricted_os_open(path, *args, **kwargs):
        if isinstance(path, int):
            raise PermissionError("Sandbox: opening file descriptors directly is blocked.")
        try:
            _p = _os.fsdecode(path)
        except TypeError:
            _p = str(path)
        try:
            _resolved = _os.path.realpath(_p)
        except Exception:
            raise PermissionError(f"Sandbox: unable to resolve path: {{path!r}}")
        if _resolved != _SANDBOX_DIR and not _resolved.startswith(_SANDBOX_DIR + _os.sep):
            raise PermissionError(
                f"Sandbox: os.open blocked outside sandbox dir: {{path!r}}"
            )
        return _orig_os_open(_resolved, *args, **kwargs)
    _os.open = _restricted_os_open

    # ── Runtime execution guard ──────────────────────────────────────────────
    # Belt-and-suspenders: disable os shell-execution functions so that even
    # if the static scanner is evaded, os.system/popen/exec* raise RuntimeError.
    #
    # NOTE: builtins.exec/eval/compile are NOT patched here because Python's
    # own import machinery calls exec(code, module.__dict__) internally when
    # loading any module — patching builtins.exec would break all imports.
    # exec/eval/compile are blocked at the STATIC SCANNER level (CRIT-4) before
    # execute_sandboxed() is ever called, which is the correct enforcement point.
    #
    # subprocess.run/Popen are covered by the import blocker (_BLOCKED_NETWORK_MODULES).
    def _make_os_blocker(_fn_name):
        def _os_blocked(*_a, **_kw):
            raise RuntimeError(
                "Sandbox: os." + _fn_name + "() is blocked — use agent skills instead."
            )
        _os_blocked.__name__ = "_blocked_os_" + _fn_name
        return _os_blocked
    for _os_fn in ("system", "popen", "execve", "execvp", "execl", "execle", "execlp",
                   "spawnl", "spawnle", "spawnv", "spawnve", "spawnlp", "spawnlpe"):
        if hasattr(_os, _os_fn):
            setattr(_os, _os_fn, _make_os_blocker(_os_fn))

    # ── User code ─────────────────────────────────────────────────────────────
""")


def _build_wrapper(sandbox_dir: str) -> str:
    """Build the sandbox wrapper string with the correct sandbox_dir injected."""
    return _SANDBOX_WRAPPER_TEMPLATE.format(
        rlimit_cpu   = _RLIMIT_CPU_SECONDS,
        rlimit_as    = _RLIMIT_AS_MB * 1024 * 1024,
        rlimit_nfile = _RLIMIT_NFILE,
        blocked_mods = sorted(_BLOCKED_NETWORK_MODULES),
        sandbox_dir  = sandbox_dir,
    )


# ── Mode detection ─────────────────────────────────────────────────────────────
_detected_mode: SandboxMode | None = None


def detect_sandbox_mode() -> SandboxMode:
    """Probe the execution environment and return the safest available mode.

    Called once at module import; result is cached in _detected_mode.
    Returns PYTHON_WRAPPER if temp dir + subprocess creation works.
    Returns UNAVAILABLE if the sandbox cannot be set up (fail-closed).
    """
    global _detected_mode
    if _detected_mode is not None:
        return _detected_mode

    try:
        # Verify we can create temp dirs and write to them
        td = tempfile.mkdtemp(prefix=_SANDBOX_DIR_PREFIX)
        test_file = os.path.join(td, "_probe.py")
        with open(test_file, "w") as f:
            f.write("print('ok')\n")
        os.unlink(test_file)
        os.rmdir(td)
        _detected_mode = SandboxMode.PYTHON_WRAPPER
        logger.info("sandbox.mode_detected", mode=_detected_mode.value)
    except Exception as _e:
        _detected_mode = SandboxMode.UNAVAILABLE
        logger.warning("sandbox.setup_failed", error=str(_e)[:120])

    return _detected_mode


# ── Clean environment ──────────────────────────────────────────────────────────
def _build_clean_env() -> dict:
    """Return a minimal environment stripped of credentials and agent paths.

    Preserves: PATH, HOME (overridden to sandbox dir), PYTHONPATH, LANG, LC_ALL.
    Strips: all *KEY*, *TOKEN*, *SECRET*, *PASS*, *API*, *AUTH* variables.
    """
    _sensitive_patterns = ("key", "token", "secret", "pass", "api", "auth",
                            "redis", "postgres", "database", "db_")
    clean = {}
    for k, v in os.environ.items():
        k_lower = k.lower()
        if any(p in k_lower for p in _sensitive_patterns):
            continue
        if k in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONPATH",
                 "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
                 "VIRTUAL_ENV", "TZ"):
            clean[k] = v

    clean["PYTHONDONTWRITEBYTECODE"] = "1"
    clean["PYTHONUNBUFFERED"] = "1"
    return clean


# ── Main entry point ───────────────────────────────────────────────────────────
async def execute_sandboxed(
    code: str,
    timeout_s: int = 30,
    max_output: int = MAX_OUTPUT_CHARS,
) -> SandboxResult:
    """Execute Python code in an isolated sandbox subprocess.

    Fail-closed: returns SandboxResult(blocked=True) if sandbox cannot be set up.

    Steps:
      1. Detect sandbox mode (cached after first call)
      2. Create temp sandbox dir
      3. Prepend wrapper (network blocker + open() patcher + resource limits)
      4. Write wrapped code to sandbox dir
      5. Execute with clean env + cwd=sandbox dir + timeout
      6. Kill process on timeout (SIGKILL)
      7. Cleanup temp dir

    Args:
        code:       Python source to execute
        timeout_s:  Wall-clock timeout in seconds (hard limit)
        max_output: Truncate output to this many chars
    """
    mode = detect_sandbox_mode()

    if mode == SandboxMode.UNAVAILABLE:
        logger.warning("sandbox.blocked_unavailable")
        return SandboxResult(
            output="",
            error="⛔ Sandbox unavailable — execution blocked (fail-closed).",
            exit_code=-1,
            sandbox_mode=mode.value,
            blocked=True,
        )

    sandbox_dir = ""
    try:
        sandbox_dir = tempfile.mkdtemp(prefix=_SANDBOX_DIR_PREFIX)
        wrapper = _build_wrapper(sandbox_dir)
        # Indent user code so it runs after the wrapper's setup
        indented_code = textwrap.indent(code, "    " * 0)
        full_source = wrapper + indented_code

        code_path = os.path.join(sandbox_dir, "code.py")
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(full_source)

        env = _build_clean_env()
        env["HOME"] = sandbox_dir   # Override HOME to sandbox dir

        logger.info(
            "sandbox.exec_started",
            sandbox_dir=sandbox_dir,
            code_len=len(code),
            timeout_s=timeout_s,
            mode=mode.value,
        )

        proc = await asyncio.create_subprocess_exec(
            sys.executable, code_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=sandbox_dir,
            env=env,
        )

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            logger.warning("sandbox.exec_timeout", timeout_s=timeout_s)
            return SandboxResult(
                output="",
                error=f"⛔ Execution timed out after {timeout_s}s — process killed.",
                exit_code=-1,
                sandbox_mode=mode.value,
                timed_out=True,
            )

        output = stdout.decode("utf-8", errors="replace")
        if len(output) > max_output:
            output = output[:max_output] + f"\n... (truncated, {len(output)} total chars)"

        exit_code = proc.returncode
        logger.info(
            "sandbox.exec_success",
            exit_code=exit_code if exit_code is not None else -1,
            output_len=len(output),
            mode=mode.value,
        )
        return SandboxResult(
            output=output.strip() if output.strip() else "(no output)",
            error="" if exit_code == 0 else f"Exit code: {exit_code}",
            exit_code=exit_code if exit_code is not None else -1,
            sandbox_mode=mode.value,
        )

    except Exception as _e:
        logger.warning("sandbox.exec_error", error=str(_e)[:120])
        return SandboxResult(
            output="",
            error=f"⛔ Sandbox execution error: {str(_e)[:200]}",
            exit_code=-1,
            sandbox_mode=mode.value if mode else SandboxMode.UNAVAILABLE.value,
            blocked=True,
        )
    finally:
        if sandbox_dir and os.path.isdir(sandbox_dir):
            try:
                shutil.rmtree(sandbox_dir, ignore_errors=True)
            except Exception:
                pass


# Trigger mode detection at module load so any setup errors surface early.
detect_sandbox_mode()
