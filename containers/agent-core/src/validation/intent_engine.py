"""Intent Completeness Engine.

Extracts all required outputs from a user message and verifies the agent's response
covers every one of them. Deterministic — no LLM calls, no external dependencies.

Public API
----------
IntentParser.parse(user_text)  → ResponseContract
ResponseContract.check(response) → CompletenessResult
CompletenessResult.correction_prompt → inject as "user" message for one retry round

Architecture
------------
The engine fires inside the LLM loop on the natural break (no more skill calls)
and injects ONE correction round if required outputs are missing.  A flag prevents
infinite loops: each request gets at most one completeness retry.

Extraction strategy (in priority order):
1. Colon-introduced list  — "quiero: X, Y, Z"  or  "necesito: 1) X  2) Y"
2. Numbered list          — "1. X\n2. Y\n3. Z" anywhere in the message
3. "Y también" chain      — "quiero X y también Y y también Z"
4. Multi-question         — two or more "?" in the message → one slot per question

If none of the above detects ≥2 distinct items, the contract is empty and the
engine does not intervene (no false positives on simple single-intent requests).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ── Spanish stop words stripped from extracted item labels ────────────────────
_STOP_WORDS = re.compile(
    r"^(?:un|una|el|la|los|las|me|te|se|lo|al|del|de|que|si|por|para|con|"
    r"en|y|o|es|son|ser|estar|a|an|the|some|any|each|all|every|also|too|"
    r"qu[eé]|c[oó]mo|cu[aá]ndo|d[oó]nde|cu[aá]l|qui[eé]n|cuya|cuyo)\s+",
    re.IGNORECASE,
)

# Separators between list items: comma, semicolon, newline-based
_ITEM_SEP_RE = re.compile(
    r",\s*(?:y\s+(?:tambi[eé]n\s+)?|and\s+(?:also\s+)?)?|"
    r";\s*(?:y\s+)?",
    re.IGNORECASE,
)

# "y también" / "y además" as item separator (handles chains)
_YTAMBIEN_RE = re.compile(
    r"\s+y\s+(?:tambi[eé]n|adem[aá]s)\s+",
    re.IGNORECASE,
)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class RequiredOutput:
    key: str            # internal identifier (normalized, max 40 chars)
    label: str          # human-readable label shown to LLM in correction prompt
    keywords: list[str] # content words used to detect presence in the response


@dataclass
class CompletenessResult:
    complete: bool
    missing: list[RequiredOutput]
    present: list[RequiredOutput]
    correction_prompt: str = ""


@dataclass
class ResponseContract:
    required: list[RequiredOutput]
    raw_intent: str

    def check(self, response_text: str) -> CompletenessResult:
        """Check whether response_text covers all required outputs.

        Coverage rule: at least ONE keyword from the required item must appear
        in the response (or its singular/plural root).
        """
        if not self.required:
            return CompletenessResult(complete=True, missing=[], present=[])

        resp_lower = response_text.lower()

        # Absolute minimum: a truly empty or single-word response can't cover anything
        if len(response_text.strip()) < 15:
            return CompletenessResult(
                complete=False,
                missing=self.required,
                present=[],
                correction_prompt=_build_correction(
                    self.required,
                    "Your response is empty or too short.",
                ),
            )

        present: list[RequiredOutput] = []
        missing: list[RequiredOutput] = []

        for req in self.required:
            found = False
            for kw in req.keywords:
                if kw in resp_lower:
                    found = True
                    break
                # Simple root matching for Spanish/English plurals
                if len(kw) > 4 and kw[:-1] in resp_lower:
                    found = True
                    break
                if len(kw) > 5 and kw[:-2] in resp_lower:
                    found = True
                    break
            if found:
                present.append(req)
            else:
                missing.append(req)

        if not missing:
            return CompletenessResult(complete=True, missing=[], present=present)

        return CompletenessResult(
            complete=False,
            missing=missing,
            present=present,
            correction_prompt=_build_correction(missing),
        )


def _build_correction(missing: list[RequiredOutput], preamble: str = "") -> str:
    labels = "\n".join(f"  • {r.label}" for r in missing)
    intro = preamble + "\n\n" if preamble else ""
    return (
        f"{intro}"
        "[COMPLETENESS CHECK — REQUIRED ACTION]\n"
        "Your previous response did NOT address the following required sections:\n"
        f"{labels}\n\n"
        "You MUST add these sections to your response now.\n"
        "Rules:\n"
        "1. Include ALL sections listed above — do not skip any.\n"
        "2. Even if uncertain, provide your best answer for each section and mark it.\n"
        "3. Keep the sections you already answered correctly — only ADD the missing ones.\n"
        "4. Use clear numbered sections or headers so each required item is identifiable.\n"
        "5. Do NOT say 'I already covered this' — if it's listed above, it was not detected."
    )


# ── Intent Parser ─────────────────────────────────────────────────────────────

class IntentParser:
    """Parse a user message into a ResponseContract of required outputs.

    Only generates non-empty contracts when the user CLEARLY enumerates ≥2 distinct
    outputs.  Conservative by design to avoid false positives.
    """

    # Request verbs — Spanish accents handled explicitly (e.g. explícame / explicame)
    _REQUEST_VERBS = (
        r"quiero|necesito|d[aá]me|expl[ií]ca(?:me)?|descri(?:be|bi)(?:me)?|"
        r"lista(?:r)?|enumera(?:r)?|include|give\s+me|tell\s+me(?:\s+about)?|"
        r"show\s+me|I\s+(?:want|need)|explain|describe|cu[eé]ntame"
    )

    # Pattern 1: "quiero: X, Y, Z" — colon-introduced list (optional preceding words)
    _COLON_INTRO_RE = re.compile(
        rf"(?:{_REQUEST_VERBS})(?:\s+\w+){{0,4}}\s*:\s*(.+?)(?:\?|$)",
        re.IGNORECASE | re.DOTALL,
    )

    # Pattern 2: Numbered list anywhere — handles "1. X\n2. Y" AND "1) X\n2) Y"
    # Multi-line: numbered items on separate lines
    _NUMBERED_ML_RE = re.compile(
        r"(?:^|\n)\s*(?:\d+[.\)]\s+|\(\d+\)\s+)(.+?)(?=\n\s*(?:\d+[.\)]\s+|\(\d+\))|\Z)",
        re.DOTALL | re.MULTILINE,
    )
    # Inline: "1) X 2) Y 3) Z" all on one line
    _NUMBERED_INLINE_RE = re.compile(
        r"(?:\d+[.\)]\s+)([^0-9\n]{3,60})(?=\s*\d+[.\)]|\Z)",
        re.IGNORECASE,
    )

    # Pattern 3: "y también" chain — "quiero X y también Y y también Z"
    # Requires a request verb before the first item
    _YTAMBIEN_CHAIN_RE = re.compile(
        rf"(?:{_REQUEST_VERBS})\s+(.+?)(?:\?|$)",
        re.IGNORECASE | re.DOTALL,
    )

    @classmethod
    def parse(cls, user_text: str) -> ResponseContract:
        """Return a ResponseContract for user_text. Empty if intent is a single item."""
        text = user_text.strip()

        # 1. Colon-introduced list (highest confidence)
        required = cls._parse_colon_list(text)
        if len(required) >= 2:
            return ResponseContract(required=required[:8], raw_intent=text)

        # 2. Numbered list (multi-line then inline)
        required = cls._parse_numbered_list(text)
        if len(required) >= 2:
            return ResponseContract(required=required[:8], raw_intent=text)

        # 3. "Y también" chain with request verb
        required = cls._parse_ytambien_chain(text)
        if len(required) >= 2:
            return ResponseContract(required=required[:8], raw_intent=text)

        # 4. Multi-question (≥2 question marks)
        required = cls._parse_multi_question(text)
        if len(required) >= 2:
            return ResponseContract(required=required[:8], raw_intent=text)

        return ResponseContract(required=[], raw_intent=text)

    # ── Extraction helpers ─────────────────────────────────────────────────────

    @classmethod
    def _parse_colon_list(cls, text: str) -> list[RequiredOutput]:
        m = cls._COLON_INTRO_RE.search(text)
        if not m:
            return []
        body = m.group(1).strip()
        if not body:
            return []

        # Check for inline numbered items first: "1) X 2) Y 3) Z"
        inline_numbered = cls._NUMBERED_INLINE_RE.findall(body)
        if len(inline_numbered) >= 2:
            return _build_outputs(inline_numbered)

        # Check for multi-line numbered items
        ml_numbered = cls._NUMBERED_ML_RE.findall(body)
        if len(ml_numbered) >= 2:
            return _build_outputs(ml_numbered)

        # Plain comma/semicolon list
        items = _ITEM_SEP_RE.split(body)
        # Also try "y también" splits inside the body
        if len(items) < 2 and _YTAMBIEN_RE.search(body):
            items = _YTAMBIEN_RE.split(body)

        return _build_outputs(items)

    @classmethod
    def _parse_numbered_list(cls, text: str) -> list[RequiredOutput]:
        # Multi-line numbered list
        matches = cls._NUMBERED_ML_RE.findall(text)
        if len(matches) >= 2:
            return _build_outputs(matches)

        # Inline numbered list (requires ≥2 matches in same sentence)
        inline = cls._NUMBERED_INLINE_RE.findall(text)
        if len(inline) >= 2:
            return _build_outputs(inline)

        return []

    @classmethod
    def _parse_ytambien_chain(cls, text: str) -> list[RequiredOutput]:
        """Detect 'quiero X y también Y y también Z' chains."""
        # Need at least 2 "y también" occurrences to confirm a chain
        ytambien_count = len(_YTAMBIEN_RE.findall(text))
        if ytambien_count < 1:
            return []

        m = cls._YTAMBIEN_CHAIN_RE.search(text)
        if not m:
            return []

        chain_text = m.group(1).strip()
        # Split the chain on "y también" separators
        parts = _YTAMBIEN_RE.split(chain_text)
        # Also split each part on commas for any sub-items
        all_items: list[str] = []
        for part in parts:
            sub = _ITEM_SEP_RE.split(part.strip())
            all_items.extend(sub)

        return _build_outputs(all_items)

    @classmethod
    def _parse_multi_question(cls, text: str) -> list[RequiredOutput]:
        if text.count("?") < 2:
            return []
        # Split on question marks; each segment (before ?) is one question
        segments = re.split(r"\?", text)
        outputs: list[RequiredOutput] = []
        for seg in segments:
            seg = seg.strip()
            if len(seg) < 8:
                continue
            # Use last 3-5 meaningful words as the label
            words = [w for w in seg.split() if len(w) > 3]
            if not words:
                continue
            label = " ".join(words[-4:]).strip(",;: ")
            if not label or len(label) < 4:
                continue
            key = re.sub(r"\W+", "_", label.lower())[:40]
            keywords = [w.lower() for w in words[-5:] if len(w) >= 4]
            if not keywords:
                continue
            outputs.append(RequiredOutput(key=key, label=label, keywords=keywords))
        return outputs


# ── Shared helpers ────────────────────────────────────────────────────────────

def _build_outputs(raw_items: list[str]) -> list[RequiredOutput]:
    """Normalize raw string items into RequiredOutput objects. Deduplicates."""
    outputs: list[RequiredOutput] = []
    seen: set[str] = set()
    for item in raw_items:
        item = item.strip(" ,;.\"\u2019\n\t\u2022-*)(")
        if not item or len(item) < 3:
            continue
        # Strip leading stop words
        cleaned = _STOP_WORDS.sub("", item).strip()
        if not cleaned or len(cleaned) < 3:
            cleaned = item  # fallback to original if stripping left nothing
        label = cleaned[:70]
        key = re.sub(r"\W+", "_", label.lower())[:40].strip("_")
        if not key or key in seen:
            continue
        seen.add(key)
        # Keywords: all content words ≥4 chars (deduped)
        keywords = list({w.lower() for w in re.findall(r"\b\w{4,}\b", label)})
        if not keywords:
            keywords = [label.lower()[:25]]
        outputs.append(RequiredOutput(key=key, label=label, keywords=keywords))
    return outputs
