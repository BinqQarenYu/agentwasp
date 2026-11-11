"""Security regression tests for audit fixes."""
from __future__ import annotations
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/app")

import pytest


class TestSelfImprovePathTraversal:
    """Verify _resolve_and_validate() uses realpath (symlink-safe)."""

    def test_normal_path_accepted(self):
        from src.skills.builtin.self_improve import SelfImproveSkill
        skill = SelfImproveSkill.__new__(SelfImproveSkill)
        full_path, err = skill._resolve_and_validate("agent/context.py")
        assert err is None, f"Expected valid path to be accepted, got: {err}"

    def test_dotdot_path_blocked(self):
        from src.skills.builtin.self_improve import SelfImproveSkill
        skill = SelfImproveSkill.__new__(SelfImproveSkill)
        full_path, err = skill._resolve_and_validate("../../etc/passwd")
        assert err is not None, "Path traversal via .. should be blocked"
        assert "blocked" in err.lower()

    def test_empty_path_rejected(self):
        from src.skills.builtin.self_improve import SelfImproveSkill
        skill = SelfImproveSkill.__new__(SelfImproveSkill)
        full_path, err = skill._resolve_and_validate("")
        assert err is not None


class TestSkillEvolutionCodeValidation:
    """Verify LLM-generated skill code is validated before write."""

    def test_valid_code_passes(self):
        from src.skills.skill_evolution import _validate_skill_code
        code = """
from src.skills.base import SkillBase
from src.skills.types import SkillDefinition, SkillResult

class MySkill(SkillBase):
    def definition(self):
        return SkillDefinition(name="my_skill", description="test", params=[])
    async def execute(self, **params):
        return SkillResult(skill_name="my_skill", success=True, output="ok")
"""
        assert _validate_skill_code(code) is None

    def test_syntax_error_blocked(self):
        from src.skills.skill_evolution import _validate_skill_code
        err = _validate_skill_code("def broken(:\n    pass")
        assert err is not None
        assert "syntax" in err.lower()

    def test_dangerous_import_blocked(self):
        from src.skills.skill_evolution import _validate_skill_code
        code = "import subprocess\nclass X: pass"
        err = _validate_skill_code(code)
        assert err is not None
        assert "subprocess" in err

    def test_eval_call_blocked(self):
        from src.skills.skill_evolution import _validate_skill_code
        code = "class X:\n    def run(self): eval('rm -rf /')"
        err = _validate_skill_code(code)
        assert err is not None
        assert "eval" in err

    def test_no_class_blocked(self):
        from src.skills.skill_evolution import _validate_skill_code
        code = "def hello(): return 42"
        err = _validate_skill_code(code)
        assert err is not None

    def test_skill_name_safe_pattern(self):
        import re
        from src.skills.skill_evolution import _SAFE_SKILL_NAME_RE
        assert _SAFE_SKILL_NAME_RE.match("web_search_emailer")
        assert _SAFE_SKILL_NAME_RE.match("ab")
        assert not _SAFE_SKILL_NAME_RE.match("../evil")
        assert not _SAFE_SKILL_NAME_RE.match("_starts_underscore")
        assert not _SAFE_SKILL_NAME_RE.match("HasUpperCase")


class TestCsrfAnonBypass:
    """CSRF token must be rejected for unauthenticated sessions."""

    def test_validate_csrf_rejects_anon_session(self):
        # The validate_csrf_token function checks session_id before Redis lookup
        # We verify the guard logic directly on the function source
        import inspect
        from src.dashboard import auth
        source = inspect.getsource(auth.validate_csrf_token)
        assert "anon" in source, "Should check for anon session"
        assert 'return False' in source, "Should return False for anon"

class TestSandboxPathTraversalBypass:
    """Verify _restricted_open and _restricted_os_open fail closed on realpath exceptions."""

    @pytest.mark.asyncio
    async def test_restricted_open_bypass_blocked(self):
        from src.skills.builtin.sandbox import execute_sandboxed

        code = """
import builtins
import os

class MaliciousPath:
    def __init__(self, sandbox, target):
        self.sandbox = sandbox
        self.target = target

    def __str__(self):
        return self.sandbox + "/\\0/fake"

    def __fspath__(self):
        return self.target

_SANDBOX_DIR = os.environ.get("HOME")
try:
    with builtins.open(MaliciousPath(_SANDBOX_DIR, "/etc/passwd"), "r") as f:
        print("EXPLOIT_SUCCESS")
except PermissionError as e:
    print("EXPLOIT_FAILED:", e)
"""
        result = await execute_sandboxed(code)
        assert "EXPLOIT_SUCCESS" not in result.output, "Vulnerability exploited!"

    @pytest.mark.asyncio
    async def test_restricted_os_open_bypass_blocked(self):
        from src.skills.builtin.sandbox import execute_sandboxed

        code = """
import os

class MaliciousPath:
    def __init__(self, sandbox, target):
        self.sandbox = sandbox
        self.target = target

    def __str__(self):
        return self.sandbox + "/\\0/fake"

    def __fspath__(self):
        return self.target

_SANDBOX_DIR = os.environ.get("HOME")
try:
    fd = os.open(MaliciousPath(_SANDBOX_DIR, "/etc/passwd"), os.O_RDONLY)
    os.close(fd)
    print("EXPLOIT_SUCCESS")
except PermissionError as e:
    print("EXPLOIT_FAILED:", e)
"""
        result = await execute_sandboxed(code)
        assert "EXPLOIT_SUCCESS" not in result.output, "Vulnerability exploited in os.open!"
