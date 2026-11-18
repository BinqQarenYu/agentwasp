---
name: Systematic debugging
description: Use when encountering any bug, test failure, or unexpected behavior, before proposing fixes.
version: "1.0.0"
metadata:
  openclaw:
    emoji: "🔍"
    homepage: https://github.com/obra/superpowers/tree/main/skills/systematic-debugging
---

# Systematic debugging

Iron Law: NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST. Symptom fixes are failure.

1. **Root Cause Investigation**: Read full error messages and stack traces without skipping. Reproduce reliably. Inspect recent git/config changes. In multi-component flows, log inputs/outputs at each boundary and trace bad values backward to their origin.
2. **Pattern Analysis**: Compare broken code against working reference patterns in this repo; list concrete differences and dependencies instead of guessing.
3. **Single Hypothesis**: State one falsifiable hypothesis ("X causes Y because Z") and test it with the smallest single-variable change.
4. **Root-Cause Fix & 3-Fix Stop Rule**: Write a failing reproduction test first, apply one scoped fix at the source, and verify cleanly. If 3 or more fixes fail, STOP patching symptoms and escalate an architectural review.
