---
name: Implementation planning
description: Use when turning a spec or multi-step requirement into an unambiguous, testable implementation plan before coding.
version: "1.0.0"
metadata:
  openclaw:
    emoji: "📐"
    homepage: https://github.com/obra/superpowers/tree/main/skills/writing-plans
---

# Implementation planning

For multi-step code changes, lock down key architectural decisions before editing:

1. **Map File Boundaries**: List exact files to create, modify, and test with one clear responsibility per file.
2. **Right-Size Tasks**: Split into independently testable deliverables with explicit `Consumes` and `Produces` function/type signatures so tasks match cleanly.
3. **Unambiguous Steps**: For each step, name exact test assertions, exact signatures/paths, and the exact verification command + expected outcome. Avoid vague placeholders ("TBD", "handle edge cases").
4. **Self-Review**: Before executing, verify full spec coverage, cross-task signature consistency, and top failure modes/edge cases implied by the requirements.
