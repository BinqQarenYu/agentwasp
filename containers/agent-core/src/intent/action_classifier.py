"""Action Intent Classifier — Phase 1 of the Action Commitment Architecture.

Conservative by design: requires BOTH an imperative verb AND an actionable
target before asserting action_commitment=True.  False negatives (missing a
real action request) are far safer than false positives (treating an
informational question as an action demand).

Does NOT call any LLM — pure regex heuristics.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ObjectiveSpec:
    """Defines what constitutes REAL SUCCESS for an action — not just step completion.

    The system uses these to distinguish "objective achieved" from "steps attempted".
    For bookings, success = confirmation number visible.
    For tracking, success = status text visible.
    For scraping, success = actual data extracted (not just page loaded).

    Phase 1 extensions add a machine-verifiable evidence contract:
      objective         — one-line goal statement (for prompt injection + error messages)
      done_when         — human-readable conditions that constitute completion
      required_evidence — keyword tokens that MUST appear in execution outputs.
                          Each item is an OR-list: "token_a|token_b" means either must match.
                          ALL items must be satisfied (AND logic across items).
      verification_rules — natural language rules for response quality.
                          Checked mechanically where possible; advisory otherwise.
    """
    # ── Existing fields (backward compatible) ─────────────────────────────────
    # Human-readable description of what "done" looks like — injected into prompt
    done_description: str = ""
    # What must NOT be enough — these signals are "partial only", not success
    partial_only_signals: list = field(default_factory=list)
    # Patterns that prove real objective completion (at least one must match)
    confirmation_patterns: list = field(default_factory=list)
    # Minimum interaction steps required beyond just navigate+capture
    min_interaction_steps: int = 0
    # Whether completion can be inferred without explicit confirmation signal
    requires_explicit_confirmation: bool = False

    # ── Phase 1: Formal DONE contract ─────────────────────────────────────────
    # One-line machine-readable goal statement (injected into prompts/error msgs)
    objective: str = ""
    # Conditions that constitute completion (human-readable, for prompts + logs)
    done_when: list = field(default_factory=list)
    # Evidence tokens that MUST be findable in execution outputs.
    # Format: each item is an OR-group: "tokenA|tokenB" — any token satisfies the item.
    # ALL items must be satisfied for spec to pass (AND across items, OR within items).
    required_evidence: list = field(default_factory=list)
    # Constraint rules applied to the final response (advisory Phase 1, blocking Phase 2+)
    verification_rules: list = field(default_factory=list)


def _humanize_required_evidence(required_evidence: list) -> list:
    """Convert required_evidence regex patterns into human-readable done_when phrases.

    Ensures done_when is always derived from required_evidence — single source of truth.
    Strips regex metacharacters and extracts the main human-readable tokens.

    Example:
      ["sent|enviado|delivered|message_id"]
      → ["Output must contain: sent, enviado, delivered, message_id"]
    """
    phrases = []
    for item in (required_evidence or []):
        try:
            # Strip regex metacharacters and noise: \\b, \\s, quantifiers, groups
            _clean = re.sub(r"\\[bBdDwWsSnStT]|\(\?:?P?<?[^>]*>?|\?|\*|\+|\{[^}]*\}|\^|\$", " ", item)
            _clean = re.sub(r"[()[\]{}\\]", " ", _clean)
            # Split on OR operator and whitespace, keep word-like tokens (>=3 chars, has letter)
            _tokens = [
                t.strip() for t in re.split(r"[|\s]+", _clean)
                if len(t.strip()) >= 3 and re.search(r"[a-zA-Z]", t)
                and not re.match(r"^\d+$", t.strip())
            ]
            if _tokens:
                phrases.append(f"Output must contain: {', '.join(_tokens[:4])}")
            else:
                phrases.append("Required evidence present in output")
        except Exception:
            phrases.append("Required evidence present in output")
    return phrases


def generate_default_spec(action_type: str, objective_text: str = "") -> "ObjectiveSpec":
    """Generate a basic ObjectiveSpec for action types without a specific template.

    Used as fallback for: agent goals, scheduled tasks, retry_demand, and any
    action type not explicitly handled by classify_action_intent().

    Args:
        action_type:    The action type string (e.g. "browser_navigation")
        objective_text: Optional free-text description of the goal

    Returns:
        A minimal ObjectiveSpec that is better than nothing.
    """
    goal_label = objective_text[:120] if objective_text else action_type or "task"

    if action_type == "email_send":
        _ev = ["sent|enviado|delivered|message_id|email sent"]
        return ObjectiveSpec(
            objective=f"Send email: {goal_label}",
            done_when=_humanize_required_evidence(_ev),  # derived from required_evidence
            required_evidence=_ev,
            verification_rules=["Response must confirm the email was sent, not just composed"],
            done_description="Email sent — delivery confirmation or message ID present",
            confirmation_patterns=[r"\bsent\b|\benviado\b|\bdelivered\b"],
        )

    if action_type == "retry_demand":
        return ObjectiveSpec(
            objective=f"Complete requested task: {goal_label}",
            done_when=["Task completed with real evidence", "Output shows actual result"],
            required_evidence=[],  # open-ended — no specific evidence required
            verification_rules=["Response must show real execution result, not explanation"],
            done_description="Task completed with real execution evidence",
        )

    # Generic fallback for unknown / goal-system tasks
    return ObjectiveSpec(
        objective=goal_label or "Complete requested objective",
        done_when=["Objective completed", "Real evidence of completion present"],
        required_evidence=[],  # no specific requirement — existing heuristics apply
        verification_rules=["Response must reflect actual execution, not speculation"],
        done_description=goal_label or "Task completed",
    )


@dataclass
class ActionIntent:
    """Result of action intent classification."""

    action_commitment: bool = False  # True → agent MUST attempt execution
    action_type: str = ""            # "browser_navigation", "browser_package_check", "email_send",
                                     # "browser_form_workflow", "browser_web_workflow", "retry_demand"
    action_target: str = ""          # URL, domain, or recipient extracted from text
    confidence: float = 0.0          # 0.0–1.0
    primary_skill: str = ""          # suggested skill name ("browser", "gmail", "shell")
    is_retry_signal: bool = False    # user explicitly said "do it" after agent failed
    tracking_code: str = ""          # Extracted package tracking number (for browser_package_check)
    workflow_objective: str = ""     # Short description of the web workflow goal (for form/web workflows)
    objective_spec: "ObjectiveSpec" = field(default_factory=ObjectiveSpec)  # Success criteria


# ---------------------------------------------------------------------------
# Imperative browser/web action verbs
# ---------------------------------------------------------------------------
_BROWSER_VERB_RE = re.compile(
    r"\b(?:"
    # Spanish
    r"entra(?:\s+a)?|ve\s+a\b|abre(?:\s+(?:la|el|el\s+sitio|la\s+p[aá]gina))?|"
    r"navega(?:\s+a)?|visita(?:\s+(?:la|el))?|"
    r"revisa(?:\s+(?:el\s+sitio|la\s+p[aá]gina|en))?|"
    r"verifica(?:\s+en)?|chequea(?:\s+en)?|"
    r"rastrea(?:\s+(?:el\s+)?(?:paquete|pedido|env[ií]o|c[oó]digo))?|"
    r"captura(?:\s+la)?|toma(?:\s+una\s+)?captura|screenshot\s+de|"
    r"descarga(?:\s+de)?|extrae(?:\s+de)?|obt[eé]n(?:\s+de)?|saca(?:\s+de)?|"
    r"busca(?:\s+en\s+(?:la\s+p[aá]gina|el\s+sitio))?|"
    r"hazlo\s+t[uú]|usa\s+(?:el\s+)?(?:browser|navegador)|usa\s+tus?\s+skills?|"
    r"h[aá]zlo\b|ejecútalo\b|tr[aá]eme(?:\s+de)?|"
    r"scrapea(?:\s+(?:el|la|los|las))?|raspea(?:\s+(?:el|la))?|"
    # English
    r"go\s+to\b|open(?:\s+(?:the|a))?|navigate(?:\s+to)?|visit(?:\s+(?:the|a))?|"
    r"check(?:\s+(?:the\s+)?(?:package|status|page|site|on))?|"
    r"browse(?:\s+to)?|capture(?:\s+(?:the|a|this))?|screenshot(?:\s+of)?|"
    r"download(?:\s+from)?|extract(?:\s+from)?|fetch(?:\s+from)?|"
    r"scrape(?:\s+(?:the|from))?|get(?:\s+(?:the|data|prices?|list|table)\s+from)?|"
    r"track(?:\s+(?:the\s+)?(?:package|order|shipment))?|"
    r"search(?:\s+(?:on|at)\s+(?:the|a))?|look\s+up(?:\s+on)?"
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Actionable targets
# ---------------------------------------------------------------------------
# URL pattern — matches http(s) URLs and bare domains like "17track.net"
_URL_RE = re.compile(
    r"https?://[^\s<>\"]+|(?<![/\w@])(?:www\.)?[\w][\w\-]+\.(?:net|com|cl|org|io|co|mx|ar|pe|uy|bo|py|ec|ve|cr|gt|hn|sv|ni|pa|do|cu|pr|vc|gd|tt|jm|bb|ag|kn|lc|dm)[/\w\-?=&%#.]*",
    re.IGNORECASE,
)

# Named-site extraction removed: the previous regex enumerated specific
# brands ("17track", "amazon", "binance", ...) which biased the agent
# toward those sites. If the user names a site without a URL, the LLM now
# resolves it via web_search at runtime — works for ANY brand, not just a
# curated list.

# Tracking CODE only — actual code patterns (not keywords like "package")
_TRACKING_CODE_RE = re.compile(
    r"\b(?:"
    r"[A-Z]{2}\d{7,11}[A-Z]{2}|"   # postal codes: LJ040128393CN, RX123456789CN
    r"1Z[0-9A-Z]{16}|"              # UPS
    r"\d{12,22}"                    # numeric codes (FedEx, USPS, etc.)
    r")\b",
)

# Package / shipment tracking codes and keywords
_PACKAGE_RE = re.compile(
    r"\b(?:"
    r"paquete|package|pedido|order|env[ií]o|shipment|encomienda|"
    r"tracking\s+(?:number|code|n[uú]mero)|n[uú]mero\s+de\s+rastreo|"
    r"c[oó]digo\s+de\s+(?:rastreo|seguimiento|tracking)|"
    r"[A-Z]{2}\d{8,10}[A-Z]{2}|"          # e.g. LJ040128393CN, RX123456789CN
    r"[A-Z]{2}\d{9}[A-Z]{2}|"             # standard postal codes
    r"1Z[0-9A-Z]{16}|"                    # UPS
    r"\b[0-9]{12,22}\b"                   # numeric tracking codes
    r")\b",
    re.IGNORECASE,
)

# Email send with recipient
_EMAIL_SEND_RE = re.compile(
    r"\b(?:env[ií]a(?:me)?|manda(?:me)?|send(?:\s+me)?)\b.{0,80}(?:correo|email|@|\bmail\b)",
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# Form / booking / reservation workflow detection
# ---------------------------------------------------------------------------
_FORM_WORKFLOW_VERB_RE = re.compile(
    r"\b(?:"
    # Spanish
    r"llena(?:\s+(?:el|la|un|una))?|rellena(?:\s+(?:el|la))?|completa(?:\s+(?:el|la|un|una))?|"
    r"env[ií]a(?:\s+(?:el|la))?|regístrame\b|r[eé]gistrame\b|registra(?:me|te|nos)?\b|"
    r"suscr[ií]bete|suscr[ií]beme\b|inscr[ií]bete|inscr[ií]beme\b|"
    r"reserva(?:\s+(?:un|una|el|la))?|agenda(?:\s+(?:un|una))?|compra(?:\s+(?:en|un|una))?|"
    r"ingresa(?:\s+(?:en|a|tus?))?|inicia\s+sesi[oó]n|haz\s+(?:click|clic)|"
    r"descarga(?:\s+(?:el|la|un))?|acepta(?:\s+(?:el|la|los))?|"
    # English
    r"fill(?:\s+(?:out|in|the))?|complete(?:\s+the)?|submit(?:\s+the)?|"
    r"register(?:\s+(?:for|on|at))?|subscribe(?:\s+to)?|sign\s+up(?:\s+for)?|"
    r"book(?:\s+(?:a|the|an))?|reserve(?:\s+a)?|purchase(?:\s+(?:a|the))?|buy(?:\s+(?:a|on))?|"
    r"log\s+in(?:\s+to)?|login(?:\s+to)?|sign\s+in(?:\s+to)?|"
    r"download(?:\s+(?:the|a|from))?|click(?:\s+(?:on|the))?"
    r")\b",
    re.IGNORECASE,
)

# Data-extraction / scraping verbs — signals browser_web_workflow, not simple navigation
_DATA_EXTRACT_VERB_RE = re.compile(
    r"\b(?:"
    r"extrae(?:\s+de)?|saca(?:\s+de)?|obt[eé]n(?:\s+de)?|scrapea?(?:\s+(?:el|la|los))?|"
    r"raspea?|desc[aá]rgate?|"
    r"extract(?:\s+(?:the|from|data|table|list|prices?))?|"
    r"scrape(?:\s+(?:the|from))?|"
    r"get(?:\s+(?:all\s+)?(?:the\s+)?(?:data|prices?|list|table|items?|products?|results?)\s+from)|"
    r"fetch(?:\s+(?:all\s+)?(?:the\s+)?(?:data|prices?|list|items?|products?|results?)\s+from)|"
    r"harvest(?:\s+(?:the|from))?|mine(?:\s+data\s+from)?"
    r")\b",
    re.IGNORECASE,
)

_FORM_WORKFLOW_TARGET_RE = re.compile(
    r"\b(?:"
    r"formulario|form|registro|registration|suscripci[oó]n|subscription|"
    r"reservaci[oó]n|reservation|reserva|booking|cita|appointment|turno|"
    r"cuenta|account|perfil|profile|p[aá]gina|page|bot[oó]n|button|"
    r"sitio|website|portal|plataforma|platform|campo|field|"
    r"inicio\s+de\s+sesi[oó]n|login\s+page|access|acceso"
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Veto patterns — explicitly informational, never trigger action commitment
# ---------------------------------------------------------------------------
_VETO_RE = re.compile(
    r"\b(?:"
    r"c[oó]mo\s+(?:puedo|se\s+puede|funciona|se\s+hace|hago)|"
    r"how\s+(?:can\s+I|do\s+I|does\s+it|to\s+\w+)|"
    r"expl[ií]ca(?:me)?|explain(?:\s+to\s+me)?|"
    r"qu[eé]\s+es\b|what\s+is\b|qui[eé]n\s+es\b|who\s+is\b|"
    r"qu[eé]\s+significa|what\s+does\s+.+\s+mean|"
    r"solo\s+expl[ií]ca|just\s+explain|sin\s+ejecutar|without\s+executing|"
    r"no\s+ejecutes?|don.?t\s+execute|dime\s+(?:c[oó]mo|qu[eé])|"
    r"tell\s+me\s+(?:how|what|about)|cu[eé]ntame"
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Retry / correction signals — user telling agent to DO what it failed to do
# ---------------------------------------------------------------------------
_RETRY_SIGNAL_RE = re.compile(
    r"\b(?:"
    r"hazlo(?:\s+t[uú])?|h[aá]zlo\b|inténtalo|int[eé]ntalo\s+(?:de\s+)?nuevo|"
    r"no\s+me\s+digas\s+(?:c[oó]mo|qu[eé])|"
    r"usa\s+tus?\s+(?:skills?|capacidades?|habilidades?|tools?|browser)|"
    r"do\s+it(?:\s+(?:now|yourself|please))?|try\s+(?:it\s+)?(?:again|now)|"
    r"just\s+do\s+it|execute\s+it|use\s+your\s+(?:skills?|tools?|browser|capabilities)|"
    r"no\s+(?:lo\s+)?(?:hiciste|hizo)|you\s+didn.?t\s+do\s+it|"
    r"no\s+me\s+(?:expliques?|digas?)\s+c[oó]mo|"
    r"hazme\s+(?:el\s+)?(?:favor\s+de\s+)?(?:hacerlo|ejecutarlo|intentarlo)|"
    r"para\s+eso\s+(?:tienes?|tens?)\s+(?:las?\s+)?(?:habilidades?|skills?|capacidades?)"
    r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_action_intent(text: str, planning_mode: bool = False) -> ActionIntent:
    """Classify whether a user request requires active skill execution.

    Conservative: requires BOTH an imperative verb AND an actionable target
    (URL, domain name, or package tracking context).  Pure questions and
    informational requests return action_commitment=False.

    Args:
        text:          User message text (after any URL injection).
        planning_mode: If True, always returns action_commitment=False —
                       planning mode explicitly blocks execution.

    Returns:
        ActionIntent with action_commitment=True only when confidence is high.
    """
    # Planning mode hard-overrides everything — execution is blocked
    if planning_mode:
        return ActionIntent()

    # Veto: explicit "explain / how to / what is" → never an action request
    if _VETO_RE.search(text):
        return ActionIntent()

    # ── Retry / correction signal (highest confidence) ─────────────────────
    # User is explicitly saying "do it yourself" or "try again"
    if _RETRY_SIGNAL_RE.search(text):
        url_m = _URL_RE.search(text)
        target = url_m.group(0) if url_m else ""
        return ActionIntent(
            action_commitment=True,
            action_type="retry_demand",
            action_target=target,
            confidence=0.95,
            primary_skill="browser",
            is_retry_signal=True,
        )

    # ── Extract actionable targets ─────────────────────────────────────────
    url_m = _URL_RE.search(text)
    has_url = bool(url_m)
    has_site = False  # named-site extraction removed; caller falls through to LLM/web_search
    pkg_m = _PACKAGE_RE.search(text)
    has_pkg = bool(pkg_m)
    target = url_m.group(0) if url_m else ""

    # Extract the actual tracking code using the code-specific pattern
    _tracking_code = ""
    _code_m = _TRACKING_CODE_RE.search(text)
    if _code_m:
        _tracking_code = _code_m.group(0)

    # ── Browser action detection (specific types first, generic last) ────────
    browser_verb = bool(_BROWSER_VERB_RE.search(text))
    form_verb = bool(_FORM_WORKFLOW_VERB_RE.search(text))
    data_verb = bool(_DATA_EXTRACT_VERB_RE.search(text))
    has_browser_action = (browser_verb or form_verb or data_verb) and (has_url or has_site)

    if has_browser_action:
        _obj = text[:120].strip().replace("\n", " ")

        # 1. Package tracking (most specific — has tracking code pattern)
        if has_pkg:
            return ActionIntent(
                action_commitment=True,
                action_type="browser_package_check",
                action_target=target,
                confidence=0.90 if has_url else 0.87,
                primary_skill="browser",
                tracking_code=_tracking_code,
                objective_spec=ObjectiveSpec(
                    done_description="Tracking result is VISIBLE — status text or event history present in page",
                    partial_only_signals=["page loaded", "input found", "form opened"],
                    confirmation_patterns=[
                        r"\[TRACK_STATUS:\s*(?:FOUND|PARTIAL)\]",
                        r"\b(?:delivered|in\s+transit|shipped|customs|departed|arrived)\b",
                        r"\d{4}-\d{2}-\d{2}",
                    ],
                    requires_explicit_confirmation=True,
                    # ── Phase 1 ──────────────────────────────────────────────
                    objective=f"Retrieve package tracking status for {_tracking_code or target}",
                    done_when=[
                        "Tracking status or delivery event history retrieved",
                        "Current location or estimated delivery date visible",
                    ],
                    required_evidence=[
                        r"delivered|in transit|shipped|customs|departed|arrived|"
                        r"TRACK_STATUS|entregado|en tr[aá]nsito|despachado|\d{4}-\d{2}-\d{2}",
                    ],
                    verification_rules=[
                        "Response must show real tracking status, not just that the page was visited",
                        "Must not report success if only a screenshot of the input form was captured",
                    ],
                ),
            )

        # 2. Form / booking / reservation (explicit form verb or form target keywords)
        _has_form_target = bool(_FORM_WORKFLOW_TARGET_RE.search(text))
        if form_verb or _has_form_target:
            return ActionIntent(
                action_commitment=True,
                action_type="browser_form_workflow",
                action_target=target,
                confidence=0.86 if _has_form_target else 0.78,
                primary_skill="browser",
                workflow_objective=_obj,
                objective_spec=ObjectiveSpec(
                    done_description="Form submitted AND confirmation received — success page, confirmation #, or thank-you message visible",
                    partial_only_signals=["form opened", "page loaded", "input found", "button clicked", "screenshot captured"],
                    confirmation_patterns=[
                        r"\b(?:confirm(?:ed|ation|aci[oó]n)?|thank\s+you|gracias|success(?:ful(?:ly)?)?|"
                        r"complet(?:ed|ada?)|registr(?:ado|ada)|reserv(?:ado|ada)|booked|"
                        r"order\s*(?:#|number|n[uú]mero)?\s*\d+|booking\s*(?:ref|#|id)?\s*\w+|"
                        r"confirmaci[oó]n\s*\d+)\b",
                    ],
                    min_interaction_steps=2,  # must have at least type+click beyond navigate
                    requires_explicit_confirmation=True,
                    # ── Phase 1 ──────────────────────────────────────────────
                    objective=f"Complete form/workflow: {_obj[:80]}",
                    done_when=[
                        "Form submitted and server accepted the submission",
                        "Confirmation number, booking reference, or explicit success message received",
                    ],
                    required_evidence=[
                        r"confirm|thank\s+you|gracias|success|complet|registr|reserv|booked|"
                        r"order\s*#|\d+\s*(?:confirmaci[oó]n|booking|reservation)",
                    ],
                    verification_rules=[
                        "Response must include a confirmation number or explicit server-side success message",
                        "Page load, screenshot, or form open alone does NOT constitute success",
                        "Must report actual form submission outcome, not describe navigation steps",
                    ],
                ),
            )

        # 3. Data extraction / scraping (extract, scrape, get data from...)
        if data_verb:
            return ActionIntent(
                action_commitment=True,
                action_type="browser_web_workflow",
                action_target=target,
                confidence=0.84 if has_url else 0.77,
                primary_skill="browser",
                workflow_objective=_obj,
                objective_spec=ObjectiveSpec(
                    done_description="Actual data extracted and visible — prices, list items, table rows, or structured content present in output",
                    partial_only_signals=["page loaded", "navigated to", "screenshot captured"],
                    confirmation_patterns=[
                        r"\$\s*\d+(?:\.\d{2})?",           # prices
                        r"\d+\s*(?:items?|results?|products?|rows?)",  # counts
                        r"(?:\n[^\n]+){5,}",                # at least 5 lines of content
                    ],
                    min_interaction_steps=1,
                    requires_explicit_confirmation=False,
                    # ── Phase 1 ──────────────────────────────────────────────
                    objective=f"Extract data from {target or 'website'}: {_obj[:60]}",
                    done_when=[
                        "Structured data retrieved from the target page",
                        "Prices, list items, table rows, or named values present in output",
                    ],
                    required_evidence=[
                        r"\$|€|£|\d+[\.,]\d+|\d+\s*(?:items?|results?|products?|rows?|registros?)",
                    ],
                    verification_rules=[
                        "Response must contain actual extracted data values, not navigation descriptions",
                        "Screenshot alone does not satisfy this objective — text data must be present",
                    ],
                ),
            )

        # 4. Generic navigation (navigate/visit/open/check URL — simple)
        if browser_verb:
            return ActionIntent(
                action_commitment=True,
                action_type="browser_navigation",
                action_target=target,
                confidence=0.90 if has_url else 0.83,
                primary_skill="browser",
                workflow_objective=_obj,
                objective_spec=ObjectiveSpec(
                    done_description="Page loaded and screenshot captured",
                    partial_only_signals=[],
                    confirmation_patterns=[r"screenshot"],
                    requires_explicit_confirmation=False,
                    # ── Phase 1 ──────────────────────────────────────────────
                    objective=f"Navigate to {target} and capture page",
                    done_when=[
                        "Target page loaded successfully",
                        "Screenshot or page content captured",
                    ],
                    required_evidence=[
                        r"screenshot|captura|loaded|cargado|navegado|visited|page\s+content",
                    ],
                    verification_rules=[
                        "Response must confirm the page was loaded or show its content",
                    ],
                ),
            )

    # ── Package tracking without explicit verb ─────────────────────────────
    # "check package LJ040128393CN on 17track.net" — implicit action
    if has_pkg and (has_url or has_site):
        return ActionIntent(
            action_commitment=True,
            action_type="browser_package_check",
            action_target=target,
            confidence=0.87,
            primary_skill="browser",
            tracking_code=_tracking_code,
            objective_spec=ObjectiveSpec(
                done_description="Tracking result is VISIBLE — status text or event history present in page",
                partial_only_signals=["page loaded", "input found"],
                confirmation_patterns=[
                    r"\[TRACK_STATUS:\s*(?:FOUND|PARTIAL)\]",
                    r"\b(?:delivered|in\s+transit|shipped|customs|departed|arrived)\b",
                    r"\d{4}-\d{2}-\d{2}",
                ],
                requires_explicit_confirmation=True,
                objective=f"Retrieve package tracking status for {_tracking_code or target}",
                done_when=["Tracking status or delivery event history retrieved"],
                required_evidence=[
                    r"delivered|in transit|shipped|customs|departed|arrived|"
                    r"TRACK_STATUS|entregado|en tr[aá]nsito|despachado|\d{4}-\d{2}-\d{2}",
                ],
                verification_rules=[
                    "Response must show real tracking status, not just that the page was visited",
                ],
            ),
        )

    # ── Email send with recipient ──────────────────────────────────────────
    if _EMAIL_SEND_RE.search(text):
        return ActionIntent(
            action_commitment=True,
            action_type="email_send",
            action_target="",
            confidence=0.85,
            primary_skill="gmail",
            objective_spec=ObjectiveSpec(
                done_description="Email sent — delivery confirmation or message ID present",
                partial_only_signals=["email composed", "draft created"],
                confirmation_patterns=[r"\bsent\b|\benviado\b|\bdelivered\b|\bmessage_id\b"],
                requires_explicit_confirmation=True,
                objective="Send email as requested",
                done_when=[
                    "Email sent successfully",
                    "Delivery confirmation or message ID received from mail provider",
                ],
                required_evidence=[
                    r"sent|enviado|delivered|message_id|email sent|correo enviado",
                ],
                verification_rules=[
                    "Response must confirm the email was sent, not just composed or drafted",
                ],
            ),
        )

    return ActionIntent()
