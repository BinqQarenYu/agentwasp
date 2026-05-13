"""System Execution Planner — Pre-execution plan generation for ActionIntent.

Converts a classified ActionIntent into a concrete, ordered ExecutionPlan before
any browser actions begin. Known workflows generate deterministic plans.
Unknown workflows generate exploratory plans with LLM_ASSIST nodes.

Architecture:
  ActionIntent → ExecutionPlanner → ExecutionPlan → PlanExecutor → SkillCalls

The system decides WHAT to do and in WHAT ORDER.
The LLM only answers WHAT IT OBSERVES when the system cannot determine a value.
LLM is NEVER asked "what should I do next?" — only "what is the value of X?".
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .action_classifier import ActionIntent


class StepType(str, Enum):
    DETERMINISTIC = "deterministic"  # System knows exact params; no LLM needed
    ADAPTIVE = "adaptive"            # System knows goal; params partially resolved
    LLM_ASSIST = "llm_assist"       # LLM asked ONE narrow factual question
    VALIDATION = "validation"        # System-side evidence check; never LLM
    FALLBACK = "fallback"           # Executes only if preceding step failed


@dataclass
class ExecutionStep:
    """A single step in an execution plan."""
    id: str
    step_type: StepType
    skill: str
    params: dict = field(default_factory=dict)
    required: bool = True
    success_signal: str = ""          # Output pattern that means this step succeeded
    fallback_ids: list[str] = field(default_factory=list)
    max_attempts: int = 3
    param_hints: dict = field(default_factory=dict)  # Context for LLM_ASSIST resolution
    evidence_hook: str | None = None  # Name of validation fn to run on output
    llm_query: str = ""               # For LLM_ASSIST: the narrow factual question
    llm_answer_schema: str = ""       # Expected format: "css_selector"|"next_action_json"|"javascript_code"
    completed: bool = False
    attempts: int = 0
    last_result: str | None = None


@dataclass
class FallbackStrategy:
    """A fallback sequence triggered by a specific failure condition."""
    trigger: str             # "selector_not_found" | "click_failed" | "no_dom_change" | "timeout"
    steps: list[ExecutionStep] = field(default_factory=list)
    max_activations: int = 2
    activations: int = 0


@dataclass
class ExecutionPlan:
    """Complete execution plan for an ActionIntent.

    The plan drives execution step by step. DETERMINISTIC steps execute directly
    (no LLM). LLM_ASSIST steps ask LLM one factual question then resume
    DETERMINISTIC execution with the resolved value.
    """
    plan_id: str
    action_type: str
    steps: list[ExecutionStep]
    required_step_ids: list[str]
    fallback_strategies: list[FallbackStrategy] = field(default_factory=list)
    confidence: float = 0.5          # 0.0–1.0: how deterministic this plan is
    max_rounds: int = 12
    current_step_idx: int = 0
    total_fallback_activations: int = 0

    def next_step(self) -> ExecutionStep | None:
        """Return the next pending step, or None if plan is done."""
        for step in self.steps:
            if not step.completed:
                return step
        return None

    def mark_step_done(self, step_id: str, success: bool, output: str = "") -> None:
        """Mark a step as completed."""
        for step in self.steps:
            if step.id == step_id:
                step.completed = True
                step.last_result = output
                return

    def mark_step_attempt(self, step_id: str) -> None:
        """Increment attempt counter for a step."""
        for step in self.steps:
            if step.id == step_id:
                step.attempts += 1
                return

    def all_required_complete(self) -> bool:
        """Check if all required steps are done."""
        completed_ids = {s.id for s in self.steps if s.completed}
        return all(req_id in completed_ids for req_id in self.required_step_ids)

    def is_exhausted(self) -> bool:
        """Return True if no more steps can be executed."""
        return all(
            s.completed or (not s.required and s.attempts >= s.max_attempts)
            for s in self.steps
        )

    def pending_deterministic(self) -> list[ExecutionStep]:
        """Return all pending DETERMINISTIC steps (can execute without LLM)."""
        return [
            s for s in self.steps
            if not s.completed and s.step_type == StepType.DETERMINISTIC
        ]

    def completed_step_ids(self) -> list[str]:
        return [s.id for s in self.steps if s.completed]


# ---------------------------------------------------------------------------
# Selector Registry — known site-specific selectors (seed data + learned)
# ---------------------------------------------------------------------------

# Seed registry: {domain → {element_type → selector}}
# Populated from real successful executions over time.
_SELECTOR_REGISTRY: dict[str, dict[str, str]] = {
    "17track.net": {
        "tracking_input": "textarea.batch_track_textarea__rhhSa",
        "submit_button": "[title*='Track the']",
    },
    "parcelsapp.com": {
        "tracking_input": "#trackinput",
        "submit_button": "button[type='submit']",
    },
    "correosexpress.com": {
        "tracking_input": "input[name='codigo']",
        "submit_button": ".boton-buscar",
    },
    "aftership.com": {
        "tracking_input": "input[placeholder*='track']",
        "submit_button": "button[type='submit']",
    },
    "laposte.fr": {
        "tracking_input": "#trackingNumber",
        "submit_button": "button[type='submit']",
    },
}

# Generic fallback selectors (tried when no site-specific selector found)
_GENERIC_TRACKING_INPUT = (
    "#search-input,input[name='trackingNumber'],input[name*='track'],"
    "input[placeholder*='track'],input[placeholder*='Track'],"
    "input[type='text']:first-of-type"
)
_GENERIC_SUBMIT = (
    "#search-button,button[type='submit'],input[type='submit'],"
    "button.search,button.btn-track,.btn-search,.track-btn"
)


# ── Selector Health Registry ─────────────────────────────────────────────────
# Tracks success/failure counts per selector for staleness detection.
# {"{domain}:{element_type}" → {success: int, failure: int, consecutive_failures: int}}
_SELECTOR_HEALTH: dict[str, dict] = {}

# Staleness thresholds
_SELECTOR_WEAK_THRESHOLD = 3     # ≥ N consecutive failures → mark weak
_SELECTOR_PRUNE_THRESHOLD = 6    # ≥ N consecutive failures → remove from registry


def record_selector_outcome(domain: str, element_type: str, success: bool) -> None:
    """Update selector health after an execution attempt."""
    domain_clean = re.sub(r"^www\.", "", domain.lower())
    key = f"{domain_clean}:{element_type}"
    if key not in _SELECTOR_HEALTH:
        _SELECTOR_HEALTH[key] = {"success": 0, "failure": 0, "consecutive_failures": 0}
    health = _SELECTOR_HEALTH[key]
    if success:
        health["success"] += 1
        health["consecutive_failures"] = 0
    else:
        health["failure"] += 1
        health["consecutive_failures"] += 1

    # Prune from registry if too many consecutive failures
    cf = health["consecutive_failures"]
    if cf >= _SELECTOR_PRUNE_THRESHOLD:
        if domain_clean in _SELECTOR_REGISTRY and element_type in _SELECTOR_REGISTRY[domain_clean]:
            pruned = _SELECTOR_REGISTRY[domain_clean].pop(element_type)
            import logging
            logging.getLogger(__name__).warning(
                "selector.pruned",
                domain=domain_clean, element_type=element_type,
                selector=pruned[:60], consecutive_failures=cf,
            )


def get_selector_confidence(domain: str, element_type: str) -> float:
    """Return confidence score [0.0..1.0] for a selector based on health data.

    1.0 = never failed or no data (optimistic default).
    Decreases as consecutive failures accumulate.
    """
    domain_clean = re.sub(r"^www\.", "", domain.lower())
    key = f"{domain_clean}:{element_type}"
    health = _SELECTOR_HEALTH.get(key)
    if not health:
        return 1.0
    total = health["success"] + health["failure"]
    if total == 0:
        return 1.0
    # Penalize consecutive failures more than total failures
    consecutive_penalty = min(0.5, health["consecutive_failures"] * 0.08)
    base = health["success"] / total
    return max(0.0, base - consecutive_penalty)


def is_selector_weak(domain: str, element_type: str) -> bool:
    """Return True if selector has exceeded the weak threshold."""
    domain_clean = re.sub(r"^www\.", "", domain.lower())
    key = f"{domain_clean}:{element_type}"
    health = _SELECTOR_HEALTH.get(key)
    if not health:
        return False
    return health["consecutive_failures"] >= _SELECTOR_WEAK_THRESHOLD


def get_selector(domain: str, element_type: str) -> tuple[str, bool]:
    """Look up a proven selector for a site element.

    Returns:
        (selector, is_known): selector string + whether it's from the registry.
        If not known, returns generic fallback selector.
        Weak selectors (high consecutive failure count) are bypassed.
    """
    domain_clean = re.sub(r"^www\.", "", domain.lower())
    registry = _SELECTOR_REGISTRY.get(domain_clean, {})
    if element_type in registry:
        # Check health — skip weak selectors and fall through to generic
        if not is_selector_weak(domain_clean, element_type):
            return registry[element_type], True
        import logging
        logging.getLogger(__name__).info(
            "selector.weak_bypassed",
            domain=domain_clean, element_type=element_type,
            confidence=get_selector_confidence(domain_clean, element_type),
        )

    # Generic fallbacks
    if element_type == "tracking_input":
        return _GENERIC_TRACKING_INPUT, False
    if element_type == "submit_button":
        return _GENERIC_SUBMIT, False
    return "", False


def register_learned_selector(domain: str, element_type: str, selector: str) -> None:
    """Register a proven selector from a successful execution."""
    domain_clean = re.sub(r"^www\.", "", domain.lower())
    if domain_clean not in _SELECTOR_REGISTRY:
        _SELECTOR_REGISTRY[domain_clean] = {}
    _SELECTOR_REGISTRY[domain_clean][element_type] = selector
    # Reset health for freshly learned selector
    key = f"{domain_clean}:{element_type}"
    _SELECTOR_HEALTH[key] = {"success": 1, "failure": 0, "consecutive_failures": 0}
    # Persist to Redis so selector survives container restarts
    try:
        from .execution_persistence import persist_selector
        persist_selector(domain_clean, element_type, selector)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _extract_domain(url: str) -> str:
    m = re.search(r"(?:https?://)?(?:www\.)?([^/\s?#]+)", url)
    return m.group(1).lower() if m else url.lower()


def _ensure_https(url: str) -> str:
    if url and not url.startswith("http"):
        return "https://" + url
    return url


# ---------------------------------------------------------------------------
# Plan templates per action type
# ---------------------------------------------------------------------------

def _plan_package_check(intent: "ActionIntent") -> ExecutionPlan | None:
    """Generate deterministic execution plan for package tracking.

    Primary strategy: compound 'track' action (single skill call, handles
    navigate+type+submit+capture internally). Fallback: manual step sequence.
    Confidence: 0.90 if site selector is known, 0.75 otherwise.

    Returns None when ``intent.action_target`` is missing — the LLM picks
    the tracker via search rather than defaulting to a hardcoded site.
    """
    code = intent.tracking_code or "TRACKING_CODE"
    if not intent.action_target:
        return None
    url = _ensure_https(intent.action_target)
    domain = _extract_domain(url)
    session = "track1"

    input_sel, input_known = get_selector(domain, "tracking_input")
    submit_sel, submit_known = get_selector(domain, "submit_button")
    confidence = 0.90 if (input_known and submit_known) else 0.75

    # ── Primary step: compound track action ──────────────────────────────────
    # The browser skill's 'track' action handles the full sequence internally.
    # This is the highest-confidence path — one call, no LLM.
    compound_track = ExecutionStep(
        id="compound_track",
        step_type=StepType.DETERMINISTIC,
        skill="browser",
        params={"action": "track", "tracking_number": code, "url": url, "session": session},
        required=True,
        success_signal="[TRACK_STATUS:",
        evidence_hook="check_track_status",
        max_attempts=2,
    )

    # ── Fallback steps: manual navigate → type → click → capture ─────────────
    navigate_fb = ExecutionStep(
        id="navigate_carrier",
        step_type=StepType.DETERMINISTIC,
        skill="browser",
        params={"action": "navigate", "url": url, "session": session},
        required=True,
        success_signal="",
        max_attempts=2,
    )

    type_code = ExecutionStep(
        id="type_tracking_code",
        step_type=StepType.DETERMINISTIC if input_known else StepType.ADAPTIVE,
        skill="browser",
        params={"action": "type", "selector": input_sel, "text": code, "session": session},
        required=True,
        success_signal="",
        fallback_ids=["type_llm_assist"],
        max_attempts=2,
    )

    click_submit = ExecutionStep(
        id="click_submit",
        step_type=StepType.DETERMINISTIC if submit_known else StepType.ADAPTIVE,
        skill="browser",
        params={"action": "click", "selector": submit_sel, "session": session},
        required=True,
        success_signal="",
        fallback_ids=["press_enter"],
        max_attempts=2,
    )

    capture_result = ExecutionStep(
        id="capture_result",
        step_type=StepType.DETERMINISTIC,
        skill="browser",
        params={"action": "capture", "session": session},
        required=True,
        success_signal="",
        evidence_hook="check_track_status",
        max_attempts=1,
    )

    # ── LLM_ASSIST fallback for unknown selector ──────────────────────────────
    type_llm_assist = ExecutionStep(
        id="type_llm_assist",
        step_type=StepType.LLM_ASSIST,
        skill="browser",
        params={"action": "type", "text": code, "session": session},
        required=False,
        llm_query=(
            f"What is the CSS selector for the tracking code input field on this page? "
            f"Answer with ONLY the CSS selector string (e.g. #input-id or input[name='track'])."
        ),
        llm_answer_schema="css_selector",
        param_hints={"param_to_fill": "selector"},
    )

    press_enter = ExecutionStep(
        id="press_enter",
        step_type=StepType.DETERMINISTIC,
        skill="browser",
        params={"action": "execute_js",
                "code": "(document.activeElement.form||document.querySelector('form')||{submit:()=>{}}).submit()",
                "session": session},
        required=False,
        max_attempts=1,
    )

    fallback_strategies = [
        FallbackStrategy(
            trigger="compound_track_failed",
            steps=[navigate_fb, type_code, click_submit, capture_result],
        ),
        FallbackStrategy(
            trigger="selector_not_found",
            steps=[type_llm_assist],
        ),
        FallbackStrategy(
            trigger="click_failed",
            steps=[press_enter],
        ),
    ]

    return ExecutionPlan(
        plan_id=str(uuid.uuid4())[:8],
        action_type=intent.action_type,
        steps=[compound_track],  # Primary: single step; fallbacks expand on failure
        required_step_ids=["compound_track"],
        fallback_strategies=fallback_strategies,
        confidence=confidence,
    )


def _plan_form_workflow(intent: "ActionIntent") -> ExecutionPlan:
    """Generate plan for form/booking/registration workflows.

    These always start DETERMINISTIC (navigate) then require LLM_ASSIST
    to identify form structure (unknown per-site). Confidence: 0.65.
    """
    url = _ensure_https(intent.action_target) if intent.action_target else "TARGET_URL"
    session = "wf1"

    navigate_step = ExecutionStep(
        id="navigate_form",
        step_type=StepType.DETERMINISTIC,
        skill="browser",
        params={"action": "navigate", "url": url, "session": session},
        required=True,
        max_attempts=2,
    )

    capture_initial = ExecutionStep(
        id="capture_initial",
        step_type=StepType.DETERMINISTIC,
        skill="browser",
        params={"action": "capture", "session": session},
        required=False,  # Useful for LLM_ASSIST context but not blocking
        max_attempts=1,
    )

    # LLM_ASSIST: identify form structure from the loaded page
    identify_form = ExecutionStep(
        id="identify_form_fields",
        step_type=StepType.LLM_ASSIST,
        skill="browser",
        params={},
        required=True,
        llm_query=(
            "You have loaded a form page. List ALL visible form fields as JSON: "
            '[{"label":"...", "selector":"CSS_SELECTOR", "type":"text|select|checkbox|radio"}]. '
            "Also provide the submit button selector on a new line: SUBMIT_SELECTOR=<selector>. "
            "Answer with ONLY the JSON array and the SUBMIT_SELECTOR line."
        ),
        llm_answer_schema="form_field_list_json",
        param_hints={"output_format": "json_array_plus_submit"},
    )

    # Adaptive: fill fields based on LLM_ASSIST identification
    fill_fields = ExecutionStep(
        id="fill_form_fields",
        step_type=StepType.ADAPTIVE,
        skill="browser",
        params={"session": session},
        required=True,
        success_signal="",
        fallback_ids=["fill_llm_direct"],
        max_attempts=2,
    )

    click_submit = ExecutionStep(
        id="click_form_submit",
        step_type=StepType.ADAPTIVE,
        skill="browser",
        params={"action": "click",
                "selector": "button[type='submit'],input[type='submit'],.btn-submit,.submit-btn,[type='submit']",
                "session": session},
        required=True,
        fallback_ids=["submit_via_js"],
        max_attempts=2,
    )

    capture_result = ExecutionStep(
        id="capture_form_result",
        step_type=StepType.DETERMINISTIC,
        skill="browser",
        params={"action": "capture", "session": session},
        required=True,
        evidence_hook="check_confirmation_pattern",
        max_attempts=1,
    )

    submit_via_js = ExecutionStep(
        id="submit_via_js",
        step_type=StepType.DETERMINISTIC,
        skill="browser",
        params={"action": "execute_js",
                "code": "document.querySelector('form') && document.querySelector('form').submit()",
                "session": session},
        required=False,
        max_attempts=1,
    )

    fallback_strategies = [
        FallbackStrategy(
            trigger="submit_failed",
            steps=[submit_via_js],
        ),
    ]

    return ExecutionPlan(
        plan_id=str(uuid.uuid4())[:8],
        action_type=intent.action_type,
        steps=[navigate_step, capture_initial, identify_form, fill_fields, click_submit, capture_result],
        required_step_ids=["navigate_form", "fill_form_fields", "click_form_submit", "capture_form_result"],
        fallback_strategies=fallback_strategies,
        confidence=0.65,
    )


def _plan_web_workflow(intent: "ActionIntent") -> ExecutionPlan:
    """Generate plan for general web data extraction / interaction workflows.

    Lower confidence (0.50) — structure is exploratory, relies on LLM_ASSIST
    to interpret page state and determine next action.
    """
    url = _ensure_https(intent.action_target) if intent.action_target else "TARGET_URL"
    session = "wf1"
    goal = (intent.workflow_objective or "complete the requested web task")[:100]

    navigate_step = ExecutionStep(
        id="navigate_target",
        step_type=StepType.DETERMINISTIC,
        skill="browser",
        params={"action": "navigate", "url": url, "session": session},
        required=True,
        max_attempts=2,
    )

    capture_page = ExecutionStep(
        id="capture_page_state",
        step_type=StepType.DETERMINISTIC,
        skill="browser",
        params={"action": "capture", "session": session},
        required=True,
        max_attempts=1,
    )

    # LLM_ASSIST: determine the next single action from current page state
    analyze_page = ExecutionStep(
        id="analyze_page_structure",
        step_type=StepType.LLM_ASSIST,
        skill="browser",
        params={},
        required=True,
        llm_query=(
            f"Goal: {goal}\n"
            "Based on the current page state, what is the NEXT SINGLE ACTION needed? "
            'Answer in JSON: {"action":"navigate|type|click|execute_js|scroll|get_text",'
            '"selector":"CSS_SELECTOR_OR_NULL","value":"VALUE_OR_NULL","reason":"one sentence"}.'
            " Answer with ONLY the JSON object."
        ),
        llm_answer_schema="next_action_json",
        param_hints={"context": "page_screenshot_available"},
    )

    # Adaptive: execute action determined by page analysis
    execute_action = ExecutionStep(
        id="execute_primary_action",
        step_type=StepType.ADAPTIVE,
        skill="browser",
        params={"session": session},  # Populated from analyze_page result
        required=True,
        evidence_hook="check_dom_change",
        max_attempts=2,
    )

    validate_data = ExecutionStep(
        id="validate_extraction",
        step_type=StepType.VALIDATION,
        skill="browser",
        params={"action": "capture", "session": session},
        required=True,
        evidence_hook="check_objective_achieved",
        max_attempts=1,
    )

    # Fallback: wait + retry after no DOM change
    wait_retry = ExecutionStep(
        id="wait_and_retry",
        step_type=StepType.DETERMINISTIC,
        skill="browser",
        params={"action": "wait", "seconds": "2", "session": session},
        required=False,
        max_attempts=1,
    )

    # Fallback: JS extraction
    js_extract = ExecutionStep(
        id="try_js_extract",
        step_type=StepType.LLM_ASSIST,
        skill="browser",
        params={"action": "execute_js", "session": session},
        required=False,
        llm_query=(
            f"Standard interaction failed. Goal: {goal}. "
            "Write JavaScript to extract the required data from the current page. "
            "Answer with ONLY the JavaScript code (no markdown)."
        ),
        llm_answer_schema="javascript_code",
        param_hints={"param_to_fill": "code"},
    )

    fallback_strategies = [
        FallbackStrategy(
            trigger="no_dom_change",
            steps=[wait_retry, validate_data],
        ),
        FallbackStrategy(
            trigger="extraction_failed",
            steps=[js_extract],
        ),
    ]

    return ExecutionPlan(
        plan_id=str(uuid.uuid4())[:8],
        action_type=intent.action_type,
        steps=[navigate_step, capture_page, analyze_page, execute_action, validate_data],
        required_step_ids=["navigate_target", "execute_primary_action"],
        fallback_strategies=fallback_strategies,
        confidence=0.50,
    )


def _plan_navigation(intent: "ActionIntent") -> ExecutionPlan:
    """Generate fully deterministic plan for simple navigation."""
    url = _ensure_https(intent.action_target) if intent.action_target else "TARGET_URL"
    session = "s1"

    navigate_step = ExecutionStep(
        id="navigate",
        step_type=StepType.DETERMINISTIC,
        skill="browser",
        params={"action": "navigate", "url": url, "session": session},
        required=True,
        max_attempts=2,
    )

    capture_step = ExecutionStep(
        id="capture",
        step_type=StepType.DETERMINISTIC,
        skill="browser",
        params={"action": "capture", "session": session},
        required=True,
        success_signal="screenshot",
        max_attempts=1,
    )

    return ExecutionPlan(
        plan_id=str(uuid.uuid4())[:8],
        action_type=intent.action_type,
        steps=[navigate_step, capture_step],
        required_step_ids=["navigate", "capture"],
        fallback_strategies=[],
        confidence=0.95,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_plan(intent: "ActionIntent") -> ExecutionPlan | None:
    """Generate an ExecutionPlan from a classified ActionIntent.

    Returns None for action types without planner support
    (email_send, retry_demand — handled by existing LLM path).
    """
    if not intent.action_commitment:
        return None

    if intent.action_type == "browser_package_check":
        return _plan_package_check(intent)
    elif intent.action_type == "browser_form_workflow":
        return _plan_form_workflow(intent)
    elif intent.action_type == "browser_web_workflow":
        return _plan_web_workflow(intent)
    elif intent.action_type == "browser_navigation":
        return _plan_navigation(intent)

    return None  # email_send, retry_demand → use existing path


def format_plan_for_prompt(plan: ExecutionPlan) -> str:
    """Format an ExecutionPlan as a structured block to inject into the system prompt.

    Replaces the free-form commitment hint with a machine-readable plan.
    The LLM must follow the plan step by step — no deviation allowed.
    """
    completed_ids = plan.completed_step_ids()
    pending_steps = [s for s in plan.steps if not s.completed]
    next_step = plan.next_step()

    step_type_labels = {
        StepType.DETERMINISTIC: "[SYSTEM-DRIVEN: call exactly as shown]",
        StepType.ADAPTIVE: "[ADAPTIVE: use shown params as starting point]",
        StepType.LLM_ASSIST: "[LLM-ASSIST: answer the question below, then call the skill]",
        StepType.VALIDATION: "[VALIDATION: call skill and check output]",
        StepType.FALLBACK: "[FALLBACK]",
    }

    lines = [
        "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"[EXECUTION PLAN #{plan.plan_id} | Type: {plan.action_type} | Confidence: {plan.confidence:.0%}]",
        "The system has generated a step-by-step execution plan.",
        "You MUST follow this plan. Execute ONE step per response. Do NOT skip steps.",
        "",
        "PLAN STEPS:",
    ]

    for i, step in enumerate(plan.steps, 1):
        if step.completed:
            status_icon = "✓"
        elif step is next_step:
            status_icon = "▶"  # Currently executing
        else:
            status_icon = "○"

        label = step_type_labels.get(step.step_type, "")
        param_str = ", ".join(
            f'{k}="{v}"' for k, v in step.params.items()
            if v and k != "session"
        )
        req_marker = "" if step.required else " (optional)"
        lines.append(f"  {status_icon} Step {i}: {step.skill}({param_str}){req_marker}  {label}")

        if step.step_type == StepType.LLM_ASSIST and step.llm_query and not step.completed:
            lines.append(f"     ↳ Answer: {step.llm_query[:120]}")

    lines += [
        "",
        f"NEXT ACTION: Execute Step marked ▶ above.",
        "RULES:",
        "  • For [SYSTEM-DRIVEN] steps: use skill params EXACTLY as shown above.",
        "  • For [LLM-ASSIST] steps: answer the question, then call the skill with your answer.",
        "  • For [ADAPTIVE] steps: use the shown params as a starting selector — adapt if needed.",
        "  • Screenshot alone is NOT completion. Continue until evidence validates the objective.",
        "  • Do NOT declare success — the system validates outcomes automatically.",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    return "\n".join(lines)


def update_plan_from_action_history(plan: ExecutionPlan, action_history: list[dict]) -> None:
    """Mark plan steps as completed based on observed action history.

    Called after each round to synchronize plan state with actual executions.
    Matches history entries to plan steps by action + params overlap.
    """
    for entry in action_history:
        action = entry.get("action", "")
        success = entry.get("success", False)
        output = entry.get("output", "")

        for step in plan.steps:
            if step.completed:
                continue
            step_action = step.params.get("action", "")
            if not step_action or step_action != action:
                continue

            # Check if this history entry matches this plan step
            if step.step_type == StepType.DETERMINISTIC:
                # For deterministic steps, action match is sufficient
                if success or action in ("navigate", "capture", "track"):
                    # Check success signal if defined
                    if not step.success_signal or step.success_signal in output:
                        plan.mark_step_done(step.id, success, output)
                        break
                    elif action == "track" and "[TRACK_STATUS:" in output:
                        plan.mark_step_done(step.id, True, output)
                        break
            elif step.step_type in (StepType.ADAPTIVE, StepType.VALIDATION):
                if success:
                    plan.mark_step_done(step.id, success, output)
                    break
