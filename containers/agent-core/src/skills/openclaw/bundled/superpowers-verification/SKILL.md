---
name: Verification before completion
description: Use before claiming any task is complete, fixed, or passing; requires fresh verification evidence.
version: "1.0.0"
metadata:
  openclaw:
    emoji: "✅"
    homepage: https://github.com/obra/superpowers/tree/main/skills/verification-before-completion
---

# Verification before completion

Iron Law: NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE.

Before claiming tests pass, a bug is fixed, a build succeeds, or work is done:
1. **Identify** the exact command that proves the claim.
2. **Run** the full command fresh after your final edit (never rely on prior runs or partial checks).
3. **Read** the full output, exit code, and pass/failure counts.
4. **Verify** every requirement and inspect VCS diff after subagent delegation instead of trusting self-reports.
5. **Report** actual verified status with observed evidence. Never claim success using "should", "probably", or linter-only checks in place of tests/builds.
