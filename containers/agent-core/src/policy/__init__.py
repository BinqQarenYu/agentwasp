"""WASP central enforcement policy.

Single source of truth for:
  • intent_gate         — does the user explicitly authorize a side-effect?
  • response_guard      — final-response policy: schedule honesty, side-effect
                          text scrub, language consistency, prompt-leak strip.
  • decision_trace      — why-did-this-happen log emitted once per request.
  • regression_checks   — deterministic verifications used by tests AND
                          referenced from prime.md regression suite.

All execution paths (Telegram handler, dashboard chat_direct, goal executor,
fast-paths, deterministic sequences) go through this module. There is no
secondary copy of any pattern or rule. Adding a new path means importing from
here, not re-implementing.

Stability contract:
  • Functions in this module are pure (no I/O, no Redis, no DB). The only
    side-effect is `decision_trace.record_trace()` which writes to Redis.
  • Public names are the ones re-exported here. Anything else is internal.
"""

from .intent_gate import (
    INTENT_GATE_PATTERNS,
    REFERENCE_PHRASE_RE,
    EMAIL_ADDR_RE,
    SIDE_EFFECT_SKILLS,
    SKILL_SAFE_ACTIONS,
    is_placeholder_subject,
    is_placeholder_body,
    user_message_provides_content,
    intent_gate_check,
    filter_inferred_side_effects,
)
from .response_guard import (
    SIDE_EFFECT_ANNOUNCEMENT_PATTERNS,
    TIME_CLAIM_RE,
    SCHEDULING_CONTEXT_RE,
    enforce_schedule_honesty,
    enforce_side_effect_text_gate,
    extract_fixed_time_unhonored,
    has_real_task_create,
    apply_final_response_policy,
)
from .decision_trace import (
    DecisionTrace,
    new_trace,
    record_trace,
    with_trace,
)
from .action_announcer import (
    ActionRecord,
    apply_action_announcer,
    collect_actions,
    render_actions_block,
    strip_unverified_claims,
)

__all__ = [
    "INTENT_GATE_PATTERNS",
    "REFERENCE_PHRASE_RE",
    "EMAIL_ADDR_RE",
    "SIDE_EFFECT_SKILLS",
    "SKILL_SAFE_ACTIONS",
    "is_placeholder_subject",
    "is_placeholder_body",
    "user_message_provides_content",
    "intent_gate_check",
    "filter_inferred_side_effects",
    "SIDE_EFFECT_ANNOUNCEMENT_PATTERNS",
    "TIME_CLAIM_RE",
    "SCHEDULING_CONTEXT_RE",
    "enforce_schedule_honesty",
    "enforce_side_effect_text_gate",
    "extract_fixed_time_unhonored",
    "has_real_task_create",
    "apply_final_response_policy",
    "DecisionTrace",
    "new_trace",
    "record_trace",
    "with_trace",
    "ActionRecord",
    "apply_action_announcer",
    "collect_actions",
    "render_actions_block",
    "strip_unverified_claims",
]
