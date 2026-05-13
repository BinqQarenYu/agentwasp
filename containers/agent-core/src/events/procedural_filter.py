"""
WASP Phase 6.2 — Procedural Memory Content Filter
===================================================
Prevents procedural memory from becoming a persistent prompt injection vector.

Applies the same security posture as behavioral rule filtering, but integrated
directly into the procedural memory ingestion path — BEFORE any DB write.

Blocked content categories:
  1. System instruction override  — "ignore previous", "new system prompt"
  2. Guard / lock disabling       — "disable validation", "bypass lock", "skip guard"
  3. Privilege escalation         — "grant admin", "elevate privilege", "root access"
  4. Execution policy alteration  — "always run", "never check", "skip confirmation"
  5. Prompt control language      — jailbreak/DAN/JAILBREAK triggers
  6. Malicious operator injection — embedded SYSTEM/HUMAN/ASSISTANT role markers

Detection is conservative: false positives on edge-cases are acceptable because
the cost of a persistent injection is far higher than a skipped abstraction.

scan_procedural_content(data) → (safe: bool, reason: str)
  safe=True  → store normally
  safe=False → reject; do NOT persist; log rejection with reason

Log events:
  procedural_filter.rejected
  procedural_filter.accepted
"""
from __future__ import annotations

import base64
import re
import unicodedata
from typing import Any

import structlog

logger = structlog.get_logger()


# ── Homoglyph / leet-speak normalisation table ────────────────────────────────
# Maps digits, symbols, and Cyrillic lookalikes used to evade keyword patterns.
_HOMOGLYPH_TABLE: dict[int, int] = str.maketrans(
    {
        "0": "o", "1": "i", "3": "e", "4": "a", "5": "s",
        "7": "t", "@": "a", "$": "s", "|": "l", "!": "i",
        "\u0430": "a",   # Cyrillic а
        "\u0435": "e",   # Cyrillic е
        "\u043e": "o",   # Cyrillic о
        "\u0440": "r",   # Cyrillic р
        "\u0441": "c",   # Cyrillic с
        "\u0445": "x",   # Cyrillic х
        "\u0456": "i",   # Cyrillic і
        "\u04b1": "u",   # Cyrillic ұ
    }
)

# Base64 token heuristic: printable ASCII after decode, 4–200 decoded bytes
_B64_TOKEN_RE = re.compile(r'^[A-Za-z0-9+/]{8,268}={0,2}$')


def _normalize_corpus(corpus: str) -> tuple[str, bool, bool]:
    """Return (normalized_corpus, was_transformed, base64_decoded).

    Steps:
      1. NFKC unicode normalisation (decomposes ligatures, fullwidth chars, etc.)
      2. Lowercase
      3. Homoglyph / leet-speak substitution
      4. Collapse whitespace
      5. Best-effort base64 decoding of token-shaped substrings
    """
    # NFKC + lowercase
    norm = unicodedata.normalize("NFKC", corpus).lower()
    # Homoglyph substitution
    norm = norm.translate(_HOMOGLYPH_TABLE)
    # Collapse whitespace
    norm = re.sub(r"\s+", " ", norm).strip()

    was_transformed = norm != corpus.lower().strip()

    # Try base64 decoding on whitespace-separated tokens
    base64_decoded = False
    extra_parts: list[str] = []
    for token in corpus.split():
        if _B64_TOKEN_RE.match(token):
            try:
                decoded_bytes = base64.b64decode(token + "==")  # pad defensively
                decoded_str = decoded_bytes.decode("utf-8", errors="ignore")
                # Only accept if result is printable, non-trivial (>=8 chars),
                # and at least half the chars are ASCII letters/spaces (natural language signal)
                alpha_ratio = sum(c.isalpha() or c == " " for c in decoded_str) / max(len(decoded_str), 1)
                if len(decoded_str) >= 8 and decoded_str.isprintable() and alpha_ratio >= 0.5:
                    extra_parts.append(decoded_str.lower())
                    base64_decoded = True
            except Exception:
                pass

    if base64_decoded:
        norm = norm + "\n" + " ".join(extra_parts)

    return norm, was_transformed, base64_decoded


# ── Injection detection patterns ───────────────────────────────────────────────
# Each entry: (regex_pattern, human_readable_reason)
# Case-insensitive; matched against: name, description, keywords (joined), steps (joined).
_INJECTION_PATTERNS: list[tuple[str, str]] = [
    # Category 1: System instruction override
    (
        r"\bignore\s+(?:previous|all|prior|above|earlier)\s+(?:instructions?|rules?|prompts?|constraints?)",
        "system_instruction_override: 'ignore previous instructions' style content",
    ),
    (
        r"\bnew\s+(?:system\s+)?(?:prompt|instructions?|rules?|persona|identity)\b",
        "system_instruction_override: new system prompt injection",
    ),
    (
        r"\boverride\s+(?:system|all|previous|prior|existing)\s+(?:instructions?|rules?|prompts?|settings?)",
        "system_instruction_override: override system instructions",
    ),
    (
        r"\byou\s+are\s+now\s+(?:a\s+)?(?:different|new|an?\s+)",
        "system_instruction_override: persona redefinition",
    ),

    # Category 2: Guard / lock / validation disabling
    (
        r"\b(?:disable|skip|bypass|circumvent|remove|deactivate)\s+"
        r"(?:the\s+)?(?:domain\s+lock|lock|guard|validation|check|filter|safety|sandbox|restriction)",
        "guard_disable: attempt to disable safety controls",
    ),
    (
        r"\balways\s+(?:bypass|skip|disable|circumvent|allow\s+without)",
        "guard_disable: unconditional bypass directive",
    ),
    (
        r"\bnever\s+(?:validate|check|verify|confirm|filter|block|reject)",
        "guard_disable: unconditional skip-validation directive",
    ),

    # Category 3: Privilege escalation
    (
        r"\b(?:grant|give|assign|escalate)\s+(?:admin|root|super|privileged?|elevated?|full)\s*(?:access|privilege|permission|rights?)?",
        "privilege_escalation: unauthorized privilege grant",
    ),
    (
        r"\b(?:root|admin|superuser)\s+(?:access|mode|privilege|override)",
        "privilege_escalation: root/admin access attempt",
    ),
    (
        r"\brun\s+as\s+(?:root|admin|superuser|privileged?)",
        "privilege_escalation: run-as-root directive",
    ),

    # Category 4: Execution policy alteration
    (
        r"\bskip\s+(?:all\s+)?(?:confirmation|auth(?:orization)?|approval|review)",
        "execution_policy: skip confirmation directive",
    ),
    (
        r"\bexecute\s+(?:without|with\s+no|bypassing)\s+(?:check|validation|confirmation|review)",
        "execution_policy: execute without check",
    ),
    (
        r"\bfrom\s+now\s+on\s+(?:always|never|skip|bypass|disable|ignore)",
        "execution_policy: persistent policy alteration directive",
    ),
    (
        r"\bevery\s+time\b.*(?:skip|bypass|disable|ignore|allow\s+without)",
        "execution_policy: recurring bypass directive",
    ),

    # Category 5: Prompt control / jailbreak language
    (
        r"\b(?:DAN|JAILBREAK|DUDE|AIM|STAN|KEVIN|OMEGA|evil\s+twin)\b",
        "prompt_control: known jailbreak persona trigger",
    ),
    (
        r"\bdo\s+anything\s+now\b",
        "prompt_control: DAN-style 'do anything now' trigger",
    ),
    (
        r"\bin\s+(?:developer|dev|god|unrestricted|unfiltered|uncensored)\s+mode",
        "prompt_control: developer/god mode injection",
    ),
    (
        r"\bacting\s+as\s+(?:an?\s+)?(?:unrestricted|unfiltered|uncensored|jailbroken)",
        "prompt_control: unrestricted persona injection",
    ),

    # Category 6: Embedded role markers
    (
        r"^(?:SYSTEM|HUMAN|USER|ASSISTANT|OPERATOR)\s*[:\|>]",
        "role_injection: embedded chat role marker",
    ),
    (
        r"\[(?:SYSTEM|HUMAN|USER|ASSISTANT|OPERATOR)\]",
        "role_injection: bracketed role marker",
    ),
    (
        r"<(?:system|human|user|assistant|operator)>",
        "role_injection: XML-style role marker",
    ),

    # Category 7: Behavioral override targeting procedural context
    (
        r"\b(?:this\s+procedure\s+overrides?|procedure\s+takes?\s+priority\s+over"
        r"|this\s+supersedes?|this\s+replaces?\s+(?:all|any|the)\s+(?:rule|instruction|constraint))",
        "policy_override: procedure claims override authority",
    ),
]

# Compile all patterns once at module load (re.IGNORECASE | re.MULTILINE)
_COMPILED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE | re.MULTILINE), reason)
    for pat, reason in _INJECTION_PATTERNS
]


def scan_procedural_content(data: dict[str, Any]) -> tuple[bool, str]:
    """Scan a procedural memory dict for injection / override content.

    Checks: name, description, trigger_keywords (joined), steps (joined).

    Returns:
        (True,  "")      — content is safe, store normally
        (False, reason)  — content is unsafe, do NOT persist, log rejection
    """
    # Build a single flat text corpus from all content fields
    parts: list[str] = []

    # Normalize: replace underscores with spaces in name so patterns like
    # "ignore_previous_instructions" match the same as "ignore previous instructions".
    name = str(data.get("name", "") or "").replace("_", " ")
    description = str(data.get("description", "") or "")
    keywords = data.get("keywords", data.get("trigger_keywords", [])) or []
    steps = data.get("steps", []) or []

    parts.append(name)
    parts.append(description)
    if isinstance(keywords, list):
        parts.extend(str(k) for k in keywords)
    elif isinstance(keywords, str):
        parts.append(keywords)
    if isinstance(steps, list):
        parts.extend(str(s) for s in steps)
    elif isinstance(steps, str):
        parts.append(steps)

    corpus = "\n".join(parts)

    # Build normalized corpus and emit observability events
    norm_corpus, was_transformed, base64_decoded = _normalize_corpus(corpus)
    if was_transformed:
        logger.debug(
            "procedural_filter.normalization_triggered",
            procedure_name=name[:60],
        )
    if base64_decoded:
        logger.warning(
            "procedural_filter.base64_detected",
            procedure_name=name[:60],
        )

    # Scan original corpus first, then normalized — reject if either matches
    for scan_corpus, corpus_label in ((corpus, "original"), (norm_corpus, "normalized")):
        for pattern, reason in _COMPILED_PATTERNS:
            match = pattern.search(scan_corpus)
            if match:
                logger.warning(
                    "procedural_filter.rejected",
                    reason=reason,
                    corpus=corpus_label,
                    matched_text=match.group(0)[:80],
                    procedure_name=name[:60],
                )
                return False, reason

    logger.debug(
        "procedural_filter.accepted",
        procedure_name=name[:60],
        corpus_len=len(corpus),
    )
    return True, ""
