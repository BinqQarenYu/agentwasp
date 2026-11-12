---
name: Test-driven development
description: Use for requested code changes to guide a focused, behavior-first test and implementation loop.
version: "1.0.0"
metadata:
  openclaw:
    emoji: "🧪"
    homepage: https://github.com/mattpocock/skills/tree/main/skills/engineering/tdd
---

# Test-driven development

Apply this workflow to code changes and bug fixes, not ordinary questions. Follow the repository's existing test framework and conventions.

1. Inspect the relevant implementation, tests, and documented test commands. Identify one observable behavior at a time.
2. Add a focused test through the public interface, then run it and confirm it fails for the expected reason.
3. Make the smallest change that passes that test. Repeat in small vertical slices instead of writing a batch of speculative tests.
4. After the behavior is covered, run the narrowest relevant test set; broaden validation when the change warrants it.

Prefer tests that describe outcomes callers care about and use expected values independent of the implementation. Avoid tests coupled to private methods, internal call order, or implementation-shaped calculations. Mock at external boundaries, not between components owned by this project.

Keep the change scoped to the requested behavior. Do not remove or weaken a test just to get a green result. Keep refactoring separate from the red-green loop and rerun the relevant tests after any refactor. If tests cannot be run, report that plainly.
