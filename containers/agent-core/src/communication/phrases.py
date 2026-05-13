"""Centralised user-facing canonical phrases — English-only source of truth.

The agent's internal voice is English. Every system-emitted message
(refusals, fallbacks, success acknowledgements, contradiction prompts)
lives here as 1-3 English variants per intent. Translation to the user's
detected language happens at publish time via communication.translator.

Design goals
------------
- Single language source: English. Catalogues never grow proportional to
  the number of supported user languages — the translator handles those.
- Multiple variants per intent so back-to-back failures don't read like a
  stuck robot (rotation by deterministic seed).
- Pure functions. No side effects. Trivial to unit-test.

Public API
----------
- ``pick(intent, seed, **kwargs)`` — sync; returns the chosen English
  variant with placeholders filled. Always English. Use the async
  ``translator.apick(intent, lang, seed, ...)`` when you need the
  rendering in the user's language.

Variant equivalence rule
------------------------
All variants for a single intent MUST convey identical meaning. They
differ only in phrasing. If they ever drift, response binding becomes
non-deterministic across rotations.
"""
from __future__ import annotations

import hashlib

# ── Phrase catalogue (English-only, canonical) ───────────────────────────────
# Each intent → list of variants. Placeholders filled by .format(**kwargs).
_CATALOGUE: dict[str, list[str]] = {
    # Refusals / failures (no skill data to ground a substantive answer)
    "generic_failure": [
        "I couldn't complete that. Want me to try a different angle?",
        "That didn't work this time. Tell me how you'd like me to try again.",
        "I'm not able to do that as is. Want to try another approach?",
    ],

    # Action attempted, external block stopped it — reason is known.
    "action_blocked": [
        "I tried, but {reason}. Want me to try another source?",
        "It didn't go through because {reason}.",
        "I attempted it, but {reason}. I can try another route if you want.",
    ],

    # No verified data could be obtained for a numeric / factual answer.
    "ungrounded_data": [
        "I don't have verified data for that right now — the sources I tried "
        "didn't return usable information.",
        "I couldn't pull confirmed data this time. The sources didn't give me "
        "anything I can rely on.",
        "I can't quote a value I haven't confirmed — the sources didn't return "
        "valid data.",
    ],

    # Action successfully completed (task creation, scheduling, etc.).
    "task_scheduled": [
        "✅ Done, it's scheduled: {detail}.",
        "All set — scheduled: {detail}.",
        "Scheduled: {detail}.",
    ],

    # User asked for a screenshot/browser action without a URL.
    "url_required": [
        "Which site do you want a screenshot of? Send me the URL.",
        "I need the URL to capture it. What's the site?",
        "Send me the URL and I'll grab the screenshot.",
    ],

    # User typed a malformed URL — never substitute, ask them to confirm.
    "url_malformed": [
        "That URL doesn't look right. Can you confirm it?",
        "I can't parse that URL. Could you paste it again?",
        "There's a typo in the URL. What's the correct one?",
    ],

    # SSRF / internal-address refusal — bilingual via translator.
    "internal_address_refused": [
        "I can't reach {target}: it's an internal address and blocked by policy.",
        "{target} is a private address — I can't reach it. "
        "Want me to try a public URL instead?",
    ],

    # Sandbox blocked code execution (subprocess, ctypes, raw socket, etc.).
    "code_blocked_security": [
        "I can't run that code: {reason}.",
        "That code was blocked for security reasons ({reason}).",
    ],

    # Outer handler timeout fallback.
    "outer_timeout": [
        "That took too long. Try again in a moment.",
        "I timed out. Want to try once more?",
        "The request ran past the time budget — please try again.",
    ],

    # Outer handler exception fallback.
    "outer_exception": [
        "Something went wrong on my end. Please try again.",
        "An unexpected error occurred. Mind giving it another shot?",
    ],

    # Empty-response last-resort fallback.
    "empty_fallback": [
        "I couldn't put together a response. Want to rephrase?",
        "I drew a blank on that. Reword it and we'll try again.",
    ],

    # Bare-value mismatch — show the user what's actually stored.
    "attribute_recap_header": [
        "I don't have that value stored. Here's what I do have:",
        "That doesn't match what's stored. Here's what I've got:",
    ],

    # Single-attribute override — DB says X, response said Y.
    "attribute_truth_single": [
        "Your stored {label} is {truth}. Tell me explicitly if you want to change it.",
        "I have {truth} as your {label}. Want to change it?",
    ],

    # Contradiction prompt — ask user which value is correct.
    "attribute_contradiction": [
        "Earlier you said your {label} was {old}, now you're saying {new}. Which is correct?",
        "I have {old} as your {label}. Confirm so I can update it to {new}.",
    ],

    # Recent-response duplicate — same answer would repeat within 60s window.
    "dedup_response": [
        "I just sent that same answer — no new information to add. "
        "Let me know if you'd like me to do something different.",
        "That would be the same response as a moment ago. "
        "If you want a different angle, tell me what to change.",
    ],

    # Screenshot validator declared the page invalid (login wall, error,
    # access restriction, etc.). Caption attached to the photo we deliver.
    "invalid_capture_warning": [
        "The screenshot doesn't contain the requested content — the page "
        "appears blocked or the content didn't render.",
        "I captured the page, but the validator marked the result as "
        "blocked or empty. The image is below for reference.",
    ],
    "invalid_capture_caption": [
        "Page appears blocked — see capture for reference.",
        "Captured but flagged as blocked.",
    ],

    # Intent-block streak: agent kept trying side-effects user did not
    # authorize. We stop and ask for explicit instruction.
    "intent_block_streak_note": [
        "I stopped retrying actions you did not explicitly request. "
        "If you want them done, please say so explicitly.",
    ],
    "intent_block_streak_only": [
        "I blocked several actions you did not explicitly request. "
        "Tell me clearly what you want me to do.",
    ],

    # User asked to send an email but did not specify a recipient.
    "email_recipient_required": [
        "Which email address should I send it to?",
        "Who should receive it? Send me the email address.",
    ],

    # Follow-up turn (e.g. "do the same again") but agent has no
    # confirmed prior domain to follow up on.
    "followup_no_context": [
        "I don't have a prior page in this conversation to follow up on. "
        "Send me the URL again, please.",
        "There's nothing to continue from. Could you paste the URL again?",
    ],

    # B1 fix: agent captured a different domain than the last confirmed
    # one — refuse to claim the result.
    "domain_mismatch_block": [
        "I tried to capture the page, but the result came from a different "
        "domain than the one you confirmed. Could you resend the URL?",
        "The captured content does not match the expected site. "
        "Please send the URL again so I can be sure.",
    ],

    # B4 fix: scheduled task auto-disabled after repeated failures.
    "task_circuit_breaker": [
        "⚠ I disabled the task '{name}' because it failed {failures} times in a row. "
        "Check the dashboard or recreate the task with adjusted parameters.",
    ],

    # Phase 4 closure — early warning when a task starts failing repeatedly
    # (≥3 consecutive failures, before circuit-breaker disables it at 5).
    # Sent ONCE per failure streak via Redis dedup flag.
    "task_failing_warning": [
        "⚠ The task '{name}' has failed {failures} times in a row "
        "(last reason: {reason}). I'll auto-disable it if it hits {threshold}. "
        "Check the dashboard if this looks wrong.",
    ],
}


def _pick_index(seed: str, n: int) -> int:
    """Deterministic index in [0, n) from ``seed``. Same seed → same index."""
    if n <= 1:
        return 0
    if not seed:
        return 0
    h = hashlib.blake2s(seed.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(h, "big") % n


def pick(intent: str, *, seed: str = "", **kwargs) -> str:
    """Render one canonical English variant of ``intent``, varied by ``seed``.

    Always returns English. For language-aware rendering use
    ``communication.translator.apick(intent, lang, seed, ...)``.
    """
    variants = _CATALOGUE.get(intent)
    if not variants:
        return f"[missing intent: {intent}]"
    chosen = variants[_pick_index(seed, len(variants))]
    if kwargs:
        try:
            return chosen.format(**kwargs)
        except (KeyError, IndexError):
            return chosen
    return chosen
