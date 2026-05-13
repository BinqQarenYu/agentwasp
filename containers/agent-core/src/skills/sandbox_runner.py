#!/usr/bin/env python3
"""Sandboxed skill runner — executed as a subprocess by the SkillExecutor.

Protocol (stdio):
  stdin  — one JSON line: {"skill_path": str, "skill_name": str, "arguments": dict}
  stdout — one JSON line: {"success": bool, "output": str, "error": str, "execution_ms": int}

Design:
  - No network sockets, no DB connections.
  - Runs with a stripped environment (no secrets injected by caller).
  - Any exception is caught and returned as {"success": false, "error": ...}.
  - This script is the ONLY code that runs inside the sandbox subprocess.
"""
from __future__ import annotations

# sys.path fix MUST happen before importing asyncio/json/etc.
# When Python runs this script it auto-inserts the script's directory
# (/app/src/skills/) as sys.path[0].  That directory contains
# skills/types.py which shadows the stdlib `types` module, causing
# "cannot import name 'Enum' from partially initialized module 'enum'".
# `import sys` is safe (built-in C module, not loaded from sys.path).
import sys
_skills_dir = "/app/src/skills"
sys.path = [p for p in sys.path if p != _skills_dir]
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

import asyncio
import json
import time
import types as _module_types


def _setup_sandbox_stubs() -> None:
    """Inject minimal stub modules so generated skills can import
    src.skills.base / src.skills.types WITHOUT triggering DB connections.

    The import chain that causes the problem:
      src.skills.__init__  → src.skills.executor
      src.skills.executor  → src.db.session
      src.db.session       → create_async_engine(DATABASE_URL)  ← fails (no DB in sandbox)

    We stub just enough of src.db / src.db.session to break that chain while
    leaving the real src.skills.base and src.skills.types importable directly.
    """
    from abc import ABC, abstractmethod

    # Minimal SkillResult compatible with the real one
    class _SkillResult:
        __slots__ = ("success", "output", "error")
        def __init__(self, success: bool = True, output: str = "", error: str = ""):
            self.success = success
            self.output = output
            self.error = error

    class _SkillBase(ABC):
        @abstractmethod
        async def execute(self, **kwargs) -> _SkillResult: ...

    def _stub(name: str, **attrs: object) -> _module_types.ModuleType:
        if name in sys.modules:
            return sys.modules[name]
        m = _module_types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    # Stub DB layer to prevent create_async_engine() at import time
    _stub("src.db")
    _stub("src.db.session",
          async_session=None,
          engine=None,
          get_db=lambda: None)
    _stub("src.db.models")

    # Stub the skills package __init__ to prevent executor import
    _skills_pkg = _stub("src.skills",
                        SkillBase=_SkillBase,
                        SkillResult=_SkillResult)
    _skills_pkg.__path__ = [_skills_dir]
    _skills_pkg.__package__ = "src.skills"

    # Expose base/types if the generated skill imports them directly.
    # Both SkillBase and SkillResult must be present in base stub because
    # real src/skills/base.py re-exports SkillResult from .types.
    _stub("src.skills.base",
          SkillBase=_SkillBase,
          SkillResult=_SkillResult)
    _stub("src.skills.types",
          SkillResult=_SkillResult,
          SkillBase=_SkillBase)


async def _run(skill_path: str, skill_name: str, arguments: dict) -> dict:
    import importlib.util

    _setup_sandbox_stubs()

    spec = importlib.util.spec_from_file_location(f"sandbox_skill.{skill_name}", skill_path)
    if spec is None or spec.loader is None:
        return {"success": False, "output": "", "error": f"Cannot load spec from {skill_path}", "execution_ms": 0}

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    skill_class = getattr(module, "GeneratedSkill", None)
    if skill_class is None:
        return {"success": False, "output": "", "error": "GeneratedSkill class not found", "execution_ms": 0}

    skill = skill_class()
    t0 = time.monotonic()
    result = await skill.execute(**arguments)
    ms = int((time.monotonic() - t0) * 1000)

    return {
        "success": result.success,
        "output": result.output or "",
        "error": result.error or "",
        "execution_ms": ms,
    }


def main() -> None:
    try:
        raw = sys.stdin.readline()
        payload = json.loads(raw)
    except Exception as e:
        sys.stdout.write(json.dumps({"success": False, "output": "", "error": f"stdin parse error: {e}", "execution_ms": 0}) + "\n")
        sys.stdout.flush()
        return

    try:
        result = asyncio.run(
            _run(
                skill_path=payload["skill_path"],
                skill_name=payload["skill_name"],
                arguments=payload.get("arguments", {}),
            )
        )
    except Exception as e:
        result = {"success": False, "output": "", "error": f"sandbox error: {e}", "execution_ms": 0}

    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
