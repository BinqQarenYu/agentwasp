import pytest
import os
import tempfile
import asyncio

@pytest.mark.asyncio
async def test_sandbox_vulnerability():
    from src.skills.builtin.sandbox import execute_sandboxed

    # We want to test the exact bypass on `_restricted_open`.
    # A successful exploit should return SandboxResult(..., blocked=False)
    # and the output should contain SECRET DATA.
    code = """
import builtins

# Create an object whose str() bypasses startswith, but __fspath__ returns the target.
# Well builtins.open takes path-like objects, but they are converted to str() in _restricted_open!
# `_resolved = str(path)`
# So if we pass a class whose `__str__` returns something starting with _SANDBOX_DIR,
# but it's passed to `_orig_open(path)`, what does `open()` do?
# `open()` calls `__fspath__()` if it has it!

class MaliciousPath:
    def __init__(self, sandbox, target):
        self.sandbox = sandbox
        self.target = target

    def __str__(self):
        # Must start with sandbox dir but be invalid for realpath so it throws
        return self.sandbox + "/\\0/fake"

    def __fspath__(self):
        return self.target

import tempfile
import os

_SANDBOX_DIR = os.environ.get("HOME")
target_file = "/etc/passwd"

try:
    with builtins.open(MaliciousPath(_SANDBOX_DIR, target_file), "r") as f:
        print("EXPLOIT_SUCCESS:", f.read(20))
except Exception as e:
    print("EXPLOIT_FAILED:", e)
"""
    result = await execute_sandboxed(code)
    print("RESULT:", result.output)
    print("ERROR:", result.error)
    assert "EXPLOIT_SUCCESS" not in result.output, "Vulnerability exploited!"
