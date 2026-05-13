"""Phase 8 — Global Control Layer.

Sits above the LLM loop. Enforces behavioral contracts:
  1. Universal grounding (all action types, not just tracking)
  2. Evidence contract (claimed success must have verifiable output)
  3. Task lock (skill calls must match task intent)
  4. Context integrity (response topic must match user intent)
  5. Hallucination zero (no invented URLs, names, data)
  6. Tool execution guard (per-task allowed skill list)
  7. Failure honesty (explicit failure messages, no fabrication)

Entry points:
  validate_tool_guard(action_type, skill_name) → bool
  enforce_response_contract(response, action_type, results, artifacts,
                             terminal_success, user_text, ref_id) → str
  build_honest_failure(action_type, results, ref_id) → str
  extract_evidence_summary(results, artifacts) → dict

Called from handlers.py — never raises, always degrades gracefully.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field

# Use structlog (matches rest of codebase)
import structlog as _structlog
_slog = _structlog.get_logger()

def _log_warn(event: str, **kw) -> None:
    _slog.warning(event, **kw)

def _log_info(event: str, **kw) -> None:
    _slog.info(event, **kw)

# ── Tool Execution Guard ───────────────────────────────────────────────────────
# Maps action_type → set of skill names allowed for that task type.
# Empty set means no restriction (use for unknown types).
# "browser" family is always allowed when action_type involves browser.
_ALLOWED_TOOLS: dict[str, frozenset[str]] = {
    "browser_package_check":  frozenset({"browser", "web_search"}),
    "browser_form_workflow":  frozenset({"browser", "web_search", "file_manager"}),
    "browser_web_workflow":   frozenset({"browser", "web_search", "file_manager",
                                         "calculate", "render_report"}),
    "browser_navigation":     frozenset({"browser", "web_search"}),
    "email_send":             frozenset({"gmail", "browser", "file_manager"}),
    "retry_demand":           frozenset(),  # unrestricted
}

# Skills that are ALWAYS allowed regardless of action_type (meta/infra skills).
# HIGH-1 Fix: python_exec removed — it is the highest-risk builtin and must
# go through the same validate_tool_guard() check as any other tool.
# web_search is kept because it is legitimately needed across all task types
# and is already constrained by the H6 domain lock check (Check 2b).
_ALWAYS_ALLOWED_SKILLS = frozenset({
    "skill_manager", "reminders", "task_manager", "memory",
    "web_search", "calculate",
})


def validate_tool_guard(action_type: str, skill_name: str) -> tuple[bool, str]:
    """Check if a skill call is within the task's allowed scope.

    Returns (allowed: bool, reason: str).
    Soft guard — caller may log+warn but should not hard-block to avoid
    breaking valid edge cases.
    """
    if not action_type or not skill_name:
        return True, "no_constraint"
    if skill_name in _ALWAYS_ALLOWED_SKILLS:
        return True, "always_allowed"
    allowed = _ALLOWED_TOOLS.get(action_type, frozenset())
    if not allowed:
        return True, "unrestricted_type"
    if skill_name in allowed:
        return True, "in_scope"
    return False, f"out_of_scope: {skill_name} not in {sorted(allowed)} for {action_type}"


# ── Evidence Contract ──────────────────────────────────────────────────────────
# Patterns in a response that claim success — each requires verifiable evidence.
# Broad patterns: the presence of screenshot/captura vocabulary in a success
# response claims visual evidence was produced.
_CLAIMED_SCREENSHOT = re.compile(
    r"(?:screenshot|captura|pantalla|imagen)",
    re.IGNORECASE,
)
_CLAIMED_FOUND = re.compile(
    r"(?:encontré|encontramos|found|hallé|obtuve|extracted?|extraje|"
    r"datos?\s+(?:obtenidos?|encontrados?)|information\s+(?:found|extracted))",
    re.IGNORECASE,
)
_CLAIMED_SUBMITTED = re.compile(
    r"(?:envié|enviamos|submitted?|completé|completamos|"
    r"formulario\s+(?:enviado|completado)|form\s+(?:sent|submitted))",
    re.IGNORECASE,
)

# Hallucination risk patterns: invented URLs, availability, fake details
_INVENTED_URL_RE = re.compile(
    r"https?://[^\s\)\"']{10,}",
    re.IGNORECASE,
)
_INVENTED_DOCTOR_RE = re.compile(
    r"\b(?:Dr\.|Dra\.|Doctor|Doctora)\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+",
    re.IGNORECASE,
)
# Availability: doctor name + any time expression → invented appointment
# Catches "tiene disponibilidad a las 14:30", "available at 3pm", etc.
_INVENTED_AVAILABILITY_RE = re.compile(
    r"\b(?:disponible|disponibilidad|available|tiene hora|tiene cita|has an appointment|"
    r"puede atender|can see you|agenda|horario)\b",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")


@dataclass
class EvidenceCheck:
    has_real_output: bool
    screenshot_present: bool
    extracted_text: str           # first 2000 chars — used by success-path checks
    urls_in_results: set[str]
    all_output_tokens: frozenset = field(default_factory=frozenset)
    # ↑ Capitalized proper-noun tokens (4+ chars) extracted from ALL result outputs
    # without truncation. Used by failure-path checks for token-based membership
    # instead of substring search on a 2000-char window.


# Matches capitalized proper-noun tokens (4+ chars) — used for evidence tokenization.
# Short words ("Las", "Dr.", "de") are excluded by the 4-char minimum.
_PROPER_TOKEN_RE = re.compile(r'[A-ZÁÉÍÓÚÑ][a-záéíóúñü]{3,}')


def extract_evidence_summary(results: list, artifacts: dict) -> EvidenceCheck:
    """Pull verifiable evidence from execution results + artifacts."""
    screenshots = artifacts.get("screenshots", [])
    combined_output = ""
    urls_seen: set[str] = set()
    proper_tokens: set[str] = set()

    for r in results:
        out = (r.output or "") if hasattr(r, "output") else ""
        combined_output += out + "\n"
        # Collect URLs that were actually navigated / returned
        for m in _INVENTED_URL_RE.finditer(out):
            urls_seen.add(m.group(0).rstrip(".,)\"'"))
        # Collect proper-noun tokens from full output (no truncation)
        for tok in _PROPER_TOKEN_RE.findall(out):
            proper_tokens.add(tok.lower())

    return EvidenceCheck(
        has_real_output=bool(combined_output.strip()),
        screenshot_present=bool(screenshots),
        extracted_text=combined_output[:2000],
        urls_in_results=urls_seen,
        all_output_tokens=frozenset(proper_tokens),
    )


def _check_evidence_contract(
    response: str,
    action_type: str,
    evidence: EvidenceCheck,
    terminal_success: bool,
) -> tuple[bool, str]:
    """Validate that claimed successes have actual evidence.

    Returns (contract_met: bool, reason: str).
    """
    if not terminal_success or not response:
        return True, "ok"

    resp = response.lower()

    # Contract 1: Screenshot claims need real screenshots
    if _CLAIMED_SCREENSHOT.search(response) and not evidence.screenshot_present:
        return False, "claimed_screenshot_without_evidence"

    # Contract 2: "Found data / extracted results" needs real extracted text
    if _CLAIMED_FOUND.search(response) and not evidence.has_real_output:
        return False, "claimed_data_without_evidence"

    # Contract 3: Screenshots exist but response claims a specific type of data
    # that doesn't appear in any result output — only enforce for high-confidence cases.
    if action_type in ("browser_package_check",) and terminal_success:
        # If response claims delivery/transit but results don't show it — checked
        # by response_grounder already; skip here to avoid double-rejection.
        pass

    return True, "ok"


# ── Hallucination Detection ────────────────────────────────────────────────────

def _detect_hallucination(
    response: str,
    evidence: EvidenceCheck,
    action_type: str,
) -> tuple[bool, str]:
    """Detect high-confidence hallucination patterns.

    Returns (hallucinated: bool, reason: str).
    Only flags when confidence is very high — avoids false positives.
    """
    # H1: Doctor + availability/time invented (not in any execution output)
    # Pattern: named doctor + availability keyword + specific time = invented appointment
    doctor_match = _INVENTED_DOCTOR_RE.search(response)
    if doctor_match:
        has_availability = _INVENTED_AVAILABILITY_RE.search(response)
        has_time = _TIME_RE.search(response)
        if (has_availability or has_time):
            # Only hallucination if the doctor name is NOT in execution output
            if doctor_match.group(0) not in evidence.extracted_text:
                return True, "invented_doctor_availability"

    # H2: Specific URL invented (not from execution output)
    urls_in_response = set()
    for m in _INVENTED_URL_RE.finditer(response):
        url = m.group(0).rstrip(".,)\"'")
        urls_in_response.add(url)
    invented_urls = urls_in_response - evidence.urls_in_results
    # Only flag if ALL URLs in response were invented (not returned by browser)
    # and the action type involves web browsing
    if (
        invented_urls
        and not (urls_in_response & evidence.urls_in_results)
        and action_type in ("browser_web_workflow", "browser_navigation",
                            "browser_form_workflow", "browser_package_check")
        and len(invented_urls) >= 2  # Require multiple invented URLs to reduce FP
    ):
        return True, f"invented_urls: {len(invented_urls)} url(s) not in execution"

    return False, "ok"


# ── Context Integrity ─────────────────────────────────────────────────────────
# Maps action_type → topic anchor words from user intent.
# At least one must appear in response for context integrity.
_CONTEXT_ANCHORS: dict[str, list[str]] = {
    "browser_package_check": [
        "paquete", "package", "rastreo", "tracking", "seguimiento",
        "envío", "shipment", "pedido", "order", "código", "code",
        "estado", "status", "courier",
    ],
    "browser_form_workflow": [
        "formulario", "form", "enviado", "sent", "completado", "completed",
        "ingresé", "rellené", "submitted", "confirmación", "confirmation",
    ],
    "browser_web_workflow": [
        "página", "page", "sitio", "site", "web", "resultado", "result",
        "encontré", "found", "información", "information", "datos", "data",
        "extraje", "extracted",
    ],
    "browser_navigation": [
        "página", "page", "sitio", "site", "navegué", "navigated",
        "abrí", "opened", "cargó", "loaded", "captura", "screenshot",
        "contenido", "content",
    ],
    "email_send": [
        "correo", "email", "enviado", "sent", "mensaje", "message",
        "destinatario", "recipient",
    ],
}

# Off-topic: phrases that indicate LLM pivoted to unrelated general knowledge
_OFF_TOPIC_PIVOT_RE = re.compile(
    r"(?:"
    r"(?:en general|generally speaking|it is worth noting|cabe destacar|hay que tener en cuenta)"
    r"|(?:según|according to)\s+(?:estudios|experts?|research|investigaciones)"
    r"|(?:the\s+(?:current\s+)?weather\s+in\b)"
    r"|(?:London|Paris|Berlin|Tokyo|Sydney)\s+(?:is|está)\s+(?:currently|actualmente)"
    r")",
    re.IGNORECASE,
)


def _check_context_integrity(
    response: str,
    action_type: str,
    terminal_success: bool,
    user_text: str,
) -> tuple[bool, str]:
    """Validate response stays on topic relative to task intent.

    Returns (intact: bool, reason: str).
    """
    if not terminal_success or not response or not action_type:
        return True, "ok"

    resp_lower = response.lower()

    # Check off-topic pivot patterns
    if _OFF_TOPIC_PIVOT_RE.search(response):
        return False, "off_topic_pivot_detected"

    # Check context anchors — response must mention at least one task-relevant term
    anchors = _CONTEXT_ANCHORS.get(action_type, [])
    if anchors and not any(a in resp_lower for a in anchors):
        return False, f"missing_topic_anchor_for_{action_type}"

    return True, "ok"


# ── Failure Honesty ───────────────────────────────────────────────────────────

def build_honest_failure(
    action_type: str,
    results: list,
    ref_id: str = "",
) -> str:
    """Build an explicit, honest failure response from raw execution data.

    Never fabricates success. Extracts actual failure reason from results.
    """
    # Extract failure signals from results
    failure_details: list[str] = []
    for r in results:
        if hasattr(r, "success") and not r.success:
            out = (r.output or "")
            err = (r.error or "") if hasattr(r, "error") else ""
            detail = err or out
            if detail:
                failure_details.append(detail[:200])
        elif hasattr(r, "output") and r.output:
            out = r.output
            if "[TRACK_STATUS: FAILED]" in out or "[FORM_STATUS: FAILED]" in out:
                failure_details.append(out[:200])

    reason_part = ""
    if failure_details:
        reason_part = f" Detalle: {failure_details[0][:150]}"

    ref_part = f" **{ref_id}**" if ref_id else ""

    type_messages = {
        "browser_package_check": f"No pude rastrear el paquete{ref_part}.{reason_part} Por favor verifica el número de seguimiento e inténtalo de nuevo.",
        "browser_form_workflow":  f"No pude completar el formulario{ref_part}.{reason_part} Es posible que el sitio haya cambiado su estructura.",
        "browser_web_workflow":   f"No pude extraer la información solicitada{ref_part}.{reason_part} El sitio puede requerir interacción manual.",
        "browser_navigation":     f"No pude navegar al sitio solicitado{ref_part}.{reason_part} Verifica que la URL sea correcta.",
        "email_send":             f"No pude enviar el correo{ref_part}.{reason_part} Verifica tu configuración de Gmail.",
    }
    return type_messages.get(
        action_type,
        f"No se pudo completar la operación{ref_part}.{reason_part}"
    )


# ── Universal Response Validation (extended Phase 7) ─────────────────────────

@dataclass
class ControlResult:
    approved: bool
    reason: str
    final_response: str
    fallback_used: bool = False


def enforce_response_contract(
    response: str,
    action_type: str,
    results: list,
    artifacts: dict,
    terminal_success: bool,
    user_text: str = "",
    ref_id: str = "",
) -> ControlResult:
    """Main Phase 8 enforcement entry point.

    Runs all validation layers in order. Returns the approved response,
    which may be the original or a deterministic fallback.

    Never raises — degrades gracefully on any internal error.
    """
    if not response or not response.strip():
        return ControlResult(False, "empty_response", response or "")

    # Gather evidence once
    try:
        evidence = extract_evidence_summary(results, artifacts)
    except Exception as _e:
        _log_warn("control_layer.evidence_extract_failed", error=str(_e)[:60])
        evidence = EvidenceCheck(False, False, "", set())

    checks: list[tuple[str, tuple[bool, str]]] = []

    # Layer 1: Evidence contract
    try:
        checks.append(("evidence_contract", _check_evidence_contract(
            response, action_type, evidence, terminal_success
        )))
    except Exception as _e:
        _log_warn("control_layer.evidence_check_error", error=str(_e)[:60])

    # Layer 2: Context integrity
    try:
        checks.append(("context_integrity", _check_context_integrity(
            response, action_type, terminal_success, user_text
        )))
    except Exception as _e:
        _log_warn("control_layer.context_check_error", error=str(_e)[:60])

    # Layer 3: Hallucination detection
    try:
        hal_detected, hal_reason = _detect_hallucination(response, evidence, action_type)
        checks.append(("hallucination", (not hal_detected, hal_reason)))
    except Exception as _e:
        _log_warn("control_layer.hallucination_check_error", error=str(_e)[:60])

    # Evaluate all checks
    failed_checks = [(name, reason) for name, (ok, reason) in checks if not ok]

    if not failed_checks:
        return ControlResult(True, "all_checks_passed", response)

    # One or more checks failed — build deterministic fallback
    primary_failure = failed_checks[0]
    _log_warn(
        "control_layer.response_rejected",
        action_type=action_type,
        terminal_success=terminal_success,
        failed=[(n, r) for n, r in failed_checks],
        preview=response[:120],
    )

    # Build fallback
    fallback = _build_fallback(
        action_type=action_type,
        results=results,
        artifacts=artifacts,
        terminal_success=terminal_success,
        ref_id=ref_id,
        evidence=evidence,
    )

    return ControlResult(
        approved=False,
        reason=f"{primary_failure[0]}:{primary_failure[1]}",
        final_response=fallback,
        fallback_used=True,
    )


def _build_fallback(
    action_type: str,
    results: list,
    artifacts: dict,
    terminal_success: bool,
    ref_id: str,
    evidence: EvidenceCheck,
) -> str:
    """Build a safe, grounded fallback response from execution data."""
    screenshots = artifacts.get("screenshots", [])

    if not terminal_success:
        return build_honest_failure(action_type, results, ref_id)

    # Success but response failed validation — use deterministic builder
    try:
        from .response_grounder import build_deterministic_response
        det = build_deterministic_response(
            results=results,
            action_type=action_type,
            ref_id=ref_id,
            artifacts=artifacts,
            terminal_success=terminal_success,
        )
        if det:
            return det
    except Exception as _e:
        _log_warn("control_layer.det_builder_failed", error=str(_e)[:60])

    # Ultimate fallback: minimal factual summary
    ref_part = f" ({ref_id})" if ref_id else ""
    shot_part = ""
    if screenshots:
        lines = "\n".join(f"![screenshot]({p})" for p in screenshots[:3])
        shot_part = f"\n\n{lines}"

    if action_type == "browser_navigation":
        content_preview = evidence.extracted_text[:300].strip()
        if content_preview:
            return f"Página cargada{ref_part}. Contenido:\n{content_preview}{shot_part}"
        return f"Navegación completada{ref_part}.{shot_part}"

    if action_type in ("browser_web_workflow", "browser_form_workflow"):
        if evidence.extracted_text:
            return f"Tarea completada{ref_part}. Resultado:\n{evidence.extracted_text[:400]}{shot_part}"
        if screenshots:
            return f"Tarea completada{ref_part}. Se capturó evidencia visual:{shot_part}"
        return f"Tarea completada{ref_part}."

    return f"Operación completada{ref_part}." + shot_part


# ── Pre-execution Task Lock Logging ──────────────────────────────────────────

def log_task_lock_violation(action_type: str, skill_name: str, reason: str) -> None:
    """Log a tool guard violation. Called from handlers when skill is out of scope."""
    _log_warn(
        "task_lock.out_of_scope_skill",
        action_type=action_type,
        skill_name=skill_name,
        reason=reason,
    )


# ── Intent Inference (Phase 8 Hardening) ─────────────────────────────────────
# Lightweight keyword-based intent resolver.
# Used when action_intent is None or uncommitted — guarantees NEVER-None intent.

@dataclass
class _InferredIntent:
    """Minimal intent proxy inferred from user text + skill calls.
    Satisfies the same attribute contract as _ActionIntent."""
    action_type: str
    action_target: str
    action_commitment: bool = True  # inferred intent is always committed
    tracking_code: str = ""

_SCREENSHOT_KW = re.compile(
    r"\b(?:captura|screenshot|pantalla|scroll|full[- ]?page|toma|foto)\b", re.IGNORECASE
)
_TRACKING_KW = re.compile(
    r"\b(?:seguimiento|tracking|rastreo|rastrea|paquete|envío|código|codigo|"
    r"numero\s+de\s+seguimiento|tracking\s+number)\b", re.IGNORECASE
)
_FORM_KW = re.compile(
    r"\b(?:agendar|hora|cita|doctor|médico|medico|reservar|formulario|"
    r"registrar|inscribir|appointment|schedule)\b", re.IGNORECASE
)
_URL_IN_TEXT_RE = re.compile(
    r"https?://[^\s\)\"']{4,}|(?:www\.)?[a-z0-9][a-z0-9\-]*\.[a-z]{2,}(?:/[^\s]*)?",
    re.IGNORECASE,
)

# Phase 3 — No URL substitution.
# Detects URL-shaped tokens in user input that look like the user MEANT to give
# a URL but it's malformed (bad scheme, missing TLD, double slashes after host,
# etc.). When such a token exists, the LLM must NOT substitute a different URL.
_MALFORMED_URL_RE = re.compile(
    # scheme-like prefix that isn't http/https/ftp/file
    r"\b(?:[a-z]{1,5}t{1,3}p{0,2}s?|h+)://[^\s,]+",
    re.IGNORECASE,
)
_TLD_LIKE_RE = re.compile(
    r"\b[a-z0-9][\w\-]+\.[a-z]{2,8}(?:/[^\s]*)?",
    re.IGNORECASE,
)
# Anchored well-formed URL token — used to validate matches.
_WELL_FORMED_URL_RE = re.compile(
    r"^https?://[a-z0-9][\w\-]*(?:\.[a-z0-9][\w\-]*)+(?:/.*)?$",
    re.IGNORECASE,
)


def _extract_url_signals_from_user_text(text: str) -> tuple[set[str], set[str]]:
    """Return (well_formed_domains, malformed_tokens).

    well_formed_domains: set of root domains the user clearly wrote.
    malformed_tokens:   tokens that LOOK URL-ish but don't parse — these are
                        the substitution risk.
    """
    text = (text or "")
    if not text:
        return set(), set()
    well: set[str] = set()
    bad: set[str] = set()

    # 1) Well-formed http(s) URLs.
    for m in re.finditer(r"https?://[^\s,)\]\"']+", text, re.IGNORECASE):
        token = m.group(0).rstrip(".,;:!?)\"'")
        if _WELL_FORMED_URL_RE.match(token):
            d = _extract_domain(token)
            if d:
                well.add(d.lower())

    # 2) Malformed scheme prefixes (htttp://, h://, htps://, etc.)
    for m in _MALFORMED_URL_RE.finditer(text):
        token = m.group(0)
        # Skip if it actually starts with valid http(s)
        if token.lower().startswith(("http://", "https://", "ftp://", "file://")):
            continue
        bad.add(token)

    # 3) TLD-like tokens — accept only obviously well-formed ones; flag the
    #    rest as malformed (single-letter TLDs, missing TLD suffix, etc.).
    for m in _TLD_LIKE_RE.finditer(text):
        token = m.group(0).rstrip(".,;:!?)\"'")
        # Skip if already counted as well-formed http URL match
        if any(token in u or u in token for u in well):
            continue
        # Heuristic: ".cm/.co/.io" etc. with 2-3 char TLD is fine; reject 1-char
        # or known typo TLDs ("googl.cm" → suspicious because no `e`).
        # We can't reliably distinguish typos from valid ccTLDs. Be conservative:
        # accept the token's domain as well-formed if its host part is at least
        # 4 chars and the TLD is in a known short whitelist OR longer than 2.
        host = token.split("/", 1)[0].lower()
        parts = host.split(".")
        if len(parts) >= 2 and len(parts[0]) >= 2 and len(parts[-1]) >= 2:
            # treat as well-formed; agent will normalise to https://
            well.add(host)
        else:
            bad.add(token)

    return well, bad


def _domain_matches_user(call_domain: str, user_well: set[str]) -> bool:
    """Allow exact match or a registrable-suffix match (sub.example.com vs example.com).
    Conservative: never matches across different SLD+TLD."""
    if not call_domain:
        return False
    cd = call_domain.lower().lstrip(".")
    for ud in user_well:
        ud2 = ud.lstrip(".")
        if cd == ud2 or cd.endswith("." + ud2) or ud2.endswith("." + cd):
            return True
    return False


def _extract_url_from_calls(skill_calls: list) -> str:
    """Pull first URL from browser skill call arguments."""
    for call in skill_calls:
        args = call.arguments if isinstance(getattr(call, "arguments", None), dict) else {}
        url = args.get("url", "")
        if url and url.startswith("http"):
            return url
    return ""


def infer_intent_from_text(user_text: str, skill_calls: list) -> _InferredIntent:
    """Infer minimal action intent from user text + proposed skill calls.

    Always returns a valid _InferredIntent — NEVER returns None.
    Rules (first match wins):
      1. Screenshot/scroll keywords → browser_navigation
      2. Tracking/package keywords  → browser_package_check
      3. Scheduling/form keywords   → browser_form_workflow
      4. URL in user text           → browser_navigation with that URL
      5. URL in skill call args     → browser_navigation with that URL
      6. Fallback                   → browser_web_workflow (generic)
    """
    text = user_text or ""

    # Extract candidate URL from user text
    url_match = _URL_IN_TEXT_RE.search(text)
    text_url = url_match.group(0) if url_match else ""
    if text_url and not text_url.startswith("http"):
        text_url = "https://" + text_url

    # Extract URL from proposed skill calls as fallback
    call_url = _extract_url_from_calls(skill_calls)
    target_url = text_url or call_url

    if _SCREENSHOT_KW.search(text):
        return _InferredIntent(
            action_type="browser_navigation",
            action_target=target_url,
        )
    if _TRACKING_KW.search(text):
        return _InferredIntent(
            action_type="browser_package_check",
            action_target=target_url,
        )
    if _FORM_KW.search(text):
        return _InferredIntent(
            action_type="browser_form_workflow",
            action_target=target_url,
        )
    if target_url:
        return _InferredIntent(
            action_type="browser_navigation",
            action_target=target_url,
        )
    # Generic fallback — never None
    return _InferredIntent(
        action_type="browser_web_workflow",
        action_target="",
    )


# Standard replan messages for structured blocks
_REPLAN_MSG_MISSING_INTENT = (
    "⛔ [PRE-EXECUTION BLOCK]\n"
    "Reason: missing_intent — no validated task intent was found.\n\n"
    "Expected behavior:\n"
    "- Clarify the task before attempting execution\n"
    "- Use only tools appropriate for the original request\n"
    "- Stay within the domain specified by the user\n"
    "Revise your approach and generate a corrected action."
)

_REPLAN_MSG_GUARD_EXCEPTION = (
    "⛔ [PRE-EXECUTION BLOCK]\n"
    "Reason: guard_exception — pre-execution validation could not complete.\n\n"
    "Expected behavior:\n"
    "- Do not execute the blocked action\n"
    "- Use a simpler, validated approach\n"
    "- Respect task intent and domain constraints\n"
    "Revise your approach and generate a corrected action."
)


# ── PRE-EXECUTION GUARD (Phase 8 Extension) ──────────────────────────────────
# Hard block: intercepts skill calls BEFORE execution.
# Validates domain lock + intent-action match.
# Returns a replan message when blocked — caller injects into LLM context.


@dataclass(frozen=True)
class DomainLock:
    """Represents an active domain lock for a conversation turn.

    mode        — domain count: "single" | "multi"
    enforcement — "strict" (user-derived) | "exploratory" (system-inferred)
    source      — how the lock was acquired: "override" | "intent" | "user_text" | "skill_call"
    confirmed:
      False  provisional — derived from first skill call; replaceable until earned
      True   confirmed   — from user text / intent / repeat / evidence; immutable

    Enforcement semantics:
      strict      — domain explicitly provided by user (user_text/intent/override);
                    confirmed strict locks cannot be replaced automatically.
      exploratory — inferred by system (first navigation or tool call);
                    confirmed exploratory locks can be replaced by stronger intent/user signals.
    """
    domains: frozenset            # frozenset[str] — all allowed domains
    mode: str                     # "single" | "multi"
    source: str                   # "override" | "intent" | "user_text" | "skill_call"
    confirmed: bool = False       # False = provisional; True = confirmed (sticky)
    enforcement: str = "strict"   # "strict" = user-derived; "exploratory" = system-inferred
    anchor_domain: str = ""       # first domain explicitly introduced by user; "" = unset

    def __bool__(self) -> bool:
        return bool(self.domains)

    def allows(self, domain: str) -> bool:
        """Check if domain is within the allowed set (subdomain rules apply)."""
        if not domain:
            return True
        return any(_domains_match(d, domain) for d in self.domains)

    def serialize(self) -> str:
        """Human-readable representation. Single domain → plain string."""
        if len(self.domains) == 1:
            return next(iter(self.domains))
        return "|".join(sorted(self.domains))

    def confirm(self) -> "DomainLock":
        """Return a confirmed copy of this lock (no other fields changed)."""
        return DomainLock(
            domains=self.domains, mode=self.mode,
            source=self.source, confirmed=True,
            enforcement=self.enforcement,
            anchor_domain=self.anchor_domain,
        )


@dataclass
class PreExecResult:
    blocked: bool
    reason: str
    violation_type: str                # "domain_drift" | "wrong_tool" | "wrong_action" | "hijack"
    blocked_skills: list               # skill names that were blocked
    replan_message: str                # message to inject into LLM for replanning
    active_domain_lock: "DomainLock | None" = None  # lock that was active or newly established
    domain_lock_source: str = ""       # convenience mirror of active_domain_lock.source


# Maps action_type → browser sub-actions that are ALLOWED for that task.
# Empty set means any browser action is allowed for that type.
_ALLOWED_BROWSER_ACTIONS: dict[str, frozenset[str]] = {
    "browser_package_check": frozenset({
        "navigate", "fill", "type", "click", "submit", "read",
        "wait", "scroll", "back", "refresh",
    }),
    "browser_form_workflow": frozenset({
        "navigate", "fill", "type", "click", "submit", "read",
        "wait", "scroll", "capture", "back", "refresh",
    }),
    "browser_web_workflow": frozenset({
        "navigate", "scroll", "capture", "click", "read",
        "extract", "wait", "back", "refresh", "fill", "type",
    }),
    "browser_navigation": frozenset({
        "navigate", "scroll", "capture", "click", "read",
        "wait", "back", "refresh",
    }),
}

# Skills that are HARD-BLOCKED for specific action types.
# Overrides the soft guard from validate_tool_guard().
_HARD_BLOCKED_TOOLS: dict[str, frozenset[str]] = {
    # Screenshot/navigation tasks: never use search engines as the action
    "browser_navigation":    frozenset({"web_search"}),
    # Package tracking: no email, no file ops
    "browser_package_check": frozenset({"gmail", "file_manager", "shell"}),
}

_DOMAIN_RE = re.compile(
    r"(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9\-]*\.[a-z]{2,}(?:\.[a-z]{2})?)",
    re.IGNORECASE,
)

# Known compound ccTLD second-level patterns — keep three parts for these.
_CCTLD_SLD: frozenset[str] = frozenset({
    "co.uk", "co.nz", "co.jp", "co.kr", "co.za", "co.in",
    "com.mx", "com.ar", "com.br", "com.au", "com.pe", "com.co",
    "org.uk", "net.uk",
})

_PORT_STRIP_RE = re.compile(r":\d+$")


def normalize_domain(domain: str) -> str:
    """Normalize a domain or URL to its root domain.

    Steps:
      1. Strip protocol (https://, http://)
      2. Strip path, query, fragment
      3. Strip port (example.com:443 → example.com)
      4. Lowercase and strip leading www.
      5. Extract root: api.example.com → example.com
         (compound ccTLDs kept: foo.co.uk → foo.co.uk)

    Examples:
      https://www.Example.com:443/path → example.com
      http://api.example.com          → example.com
      example.com:443                 → example.com
      shop.co.uk                      → shop.co.uk  (2-part compound ccTLD)
    """
    if not domain:
        return ""
    # Strip protocol
    d = re.sub(r"^https?://", "", domain.strip(), flags=re.IGNORECASE)
    # Strip path, query, fragment
    d = d.split("/")[0].split("?")[0].split("#")[0]
    # Strip port
    d = _PORT_STRIP_RE.sub("", d)
    # Lowercase, strip stray dots
    d = d.lower().strip(".")
    # Strip leading www.
    if d.startswith("www."):
        d = d[4:]
    if not d:
        return ""
    # Extract root domain (strip subdomains)
    parts = d.split(".")
    if len(parts) >= 3:
        last_two = ".".join(parts[-2:])
        if last_two in _CCTLD_SLD:
            d = ".".join(parts[-3:])   # label.sld.tld  e.g. foo.co.uk
        else:
            d = ".".join(parts[-2:])   # label.tld      e.g. example.com
    # Minimum root-label length guard (rejects "a.cl", single-char labels)
    root_parts = d.split(".")
    if not root_parts or len(root_parts[0]) < 2:
        return ""
    return d


def _extract_domain(url: str) -> str:
    """Extract and root-normalize a domain from a URL string.

    Delegates to normalize_domain() which strips protocol, www, port, path,
    and subdomains.  Returns "" if extraction fails the minimum-label guard.

    Examples:
      https://api.example.com/path → example.com
      http://www.biobiochile.cl    → biobiochile.cl
      shop.example.co.uk          → example.co.uk
    """
    if not url:
        return ""
    # First try _DOMAIN_RE to isolate the host portion from complex URLs
    m = _DOMAIN_RE.search(url.strip())
    candidate = m.group(0) if m else url
    return normalize_domain(candidate)


def _domains_match(expected: str, actual: str) -> bool:
    """Check if two domains are equivalent after root normalization.

    Both sides are normalized to their root domain so subdomains, www prefixes,
    protocols and ports are all ignored.

    biobiochile.cl == www.biobiochile.cl == noticias.biobiochile.cl → True
    biobiochile.cl == coinmarketcap.com  → False
    example.com    == example.co         → False
    example.com    == example-login.com  → False
    """
    if not expected or not actual:
        return True  # can't validate without both — allow
    exp = normalize_domain(expected) or expected.lower().lstrip("www.")
    act = normalize_domain(actual)   or actual.lower().lstrip("www.")
    # Exact root match OR actual is a subdomain of expected root
    return act == exp or act.endswith("." + exp)


# ── Priority 2: Universal Domain Lock ─────────────────────────────────────────
# Derives the active domain lock from reality, not from a named-site list.
# Sources in priority order: persistent override → intent target → user text → first skill call.

# Matches bare domain references in user text (no protocol prefix required).
# Restricted to well-known TLDs to avoid false positives on abbreviations / file paths.
_BARE_DOMAIN_RE = re.compile(
    r'(?<![a-z0-9\-])'
    r'([a-z0-9][a-z0-9\-]{1,62}'
    r'\.(?:cl|com|org|net|io|co|pe|mx|ar|br|es|info|gov|edu|app|dev|ai|tv'
    r'|online|store|shop|news|media|radio|prensa))'
    r'(?![a-z0-9\-])',
    re.IGNORECASE,
)

# ── H6: Non-browser tool domain lock patterns ────────────────────────────────
# web_search: site:domain.tld directive and inline URLs
_WEBSEARCH_SITE_RE = re.compile(
    r'\bsite:([a-z0-9][a-z0-9\-.]{1,62}\.[a-z]{2,})',
    re.IGNORECASE,
)

# python_exec: HTTP URL literals in code
_PYEXEC_URL_RE = re.compile(
    r'https?://[^\s\'"\\,\)\]}{]+',
    re.IGNORECASE,
)
# python_exec: network access patterns (requests, httpx, urllib, aiohttp, socket, etc.)
_PYEXEC_NET_RE = re.compile(
    r'(?:'
    r'requests\s*\.\s*(?:get|post|put|delete|patch|head|request|Session)\s*\('
    r'|httpx\s*\.\s*(?:get|post|put|delete|patch|head|request|AsyncClient|Client)\s*\('
    r'|urllib\s*\.\s*(?:request|urlopen)|urllib\.request\.|urlopen\s*\('
    r'|http\.client\.'
    r'|aiohttp\s*\.\s*(?:ClientSession|request)'
    r'|socket\s*\.\s*(?:connect|create_connection)\s*\('
    r'|fetch\s*\('
    r')',
    re.IGNORECASE,
)

# shell: network-capable commands
_SHELL_NET_TOOLS_RE = re.compile(
    r'(?:^|[\s;|&`(])(?:curl|wget|ping|ping6|nc|ncat|netcat|ssh|scp|rsync'
    r'|dig|nslookup|host|telnet|ftp|sftp|aria2c|axel|lynx|w3m|httrack|httpie|http)\b',
    re.IGNORECASE,
)
# shell: URLs in command string
_SHELL_URL_RE = re.compile(
    r'https?://[^\s\'"\\;|&,\)]+',
    re.IGNORECASE,
)
# shell: bare hostname argument (fallback when no URL scheme present)
_SHELL_HOST_ARG_RE = re.compile(
    r'(?:^|[\s])([a-z0-9][a-z0-9\-]{1,62}\.[a-z]{2,}(?:\.[a-z]{2})?)\b',
    re.IGNORECASE,
)

# ── Hijack protection: domain-category mapping ────────────────────────────────
# Only HIGH-SPECIFICITY domains are listed — those whose category is unmistakable
# and whose use for an unrelated task is almost certainly a bad LLM choice.
# Generic domains (google.com, wikipedia.org, etc.) are intentionally omitted
# to avoid over-blocking legitimate open-ended tasks.
_SPECIFIC_DOMAIN_CATEGORIES: dict[str, str] = {
    # crypto pricing / trading
    "coinmarketcap.com": "crypto",
    "coingecko.com": "crypto",
    "binance.com": "crypto",
    "kraken.com": "crypto",
    "coinbase.com": "crypto",
    "bitfinex.com": "crypto",
    # medical / clinical booking
    "bmc.cl": "medical",
    "clinicalasfas.cl": "medical",
    "clinicasantamaria.cl": "medical",
    "clinicaalemana.cl": "medical",
    "clinicalascondes.cl": "medical",
    "redclc.cl": "medical",
    # financial / banking
    "banchile.cl": "finance",
    "bancoestado.cl": "finance",
    "santander.cl": "finance",
    "bci.cl": "finance",
    "scotiabank.cl": "finance",
}

# Keywords that confirm a task belongs to a given category.
# A provisional skill_call lock is accepted only when at least ONE of these
# signals appears in (user_text + action_type). Empty set = no validation.
_CATEGORY_TEXT_SIGNALS: dict[str, frozenset] = {
    "crypto": frozenset([
        "bitcoin", "btc", "ethereum", "eth", "crypto", "cripto", "criptomoneda",
        "binance", "coinbase", "precio btc", "precio eth", "coin", "token",
    ]),
    "medical": frozenset([
        "médico", "doctor", "cita", "hora médica", "clínica", "hospital",
        "consulta", "agendar hora", "reservar hora", "appointment",
    ]),
    "finance": frozenset([
        "banco", "cuenta", "transferencia", "pago", "saldo", "tarjeta",
        "crédito", "débito", "chequera", "bci", "santander", "scotiabank",
    ]),
}


def _is_related_to_anchor(domain: str, anchor: str) -> bool:
    """Return True if domain is topically related to the anchor domain.

    Related means:
      A. Same root domain (exact match or subdomain of anchor)
      B. Both belong to the same known category in _SPECIFIC_DOMAIN_CATEGORIES

    Unknown domains (not in the category map) are considered unrelated.
    """
    nd = normalize_domain(domain) or domain.lower()
    na = normalize_domain(anchor) or anchor.lower()
    # A. Same root or subdomain
    if nd == na or nd.endswith("." + na):
        return True
    # B. Same known category
    cat_d = _SPECIFIC_DOMAIN_CATEGORIES.get(nd, "")
    cat_a = _SPECIFIC_DOMAIN_CATEGORIES.get(na, "")
    return bool(cat_d and cat_a and cat_d == cat_a)


def _is_domain_transition_valid(
    old_lock: "DomainLock",
    new_domains: list,
    user_text: str,
) -> "tuple[bool, str]":
    """Decide whether replacing or extending old_lock with new_domains is legitimate.

    ALLOW only when one of three rules fires:
      Rule A: user_text explicitly names at least one of the new domains.
              (user consciously named the destination)
      Rule B: new domain shares a known category with current lock domains.
              (same-category navigation — e.g. binance → coinbase for crypto)
      Rule C: user_text contains category-specific signals that match the new
              domain's category (e.g. user asks "compare crypto prices" →
              allow navigation to a crypto exchange domain).

    All other transitions are REJECTED to prevent silent LLM-driven domain drift.
    Returns (allowed, reason_str).
    """
    if not new_domains:
        return False, "no_new_domains"

    text_lower = (user_text or "").lower()

    # Rule A — user_text explicitly names at least one new domain.
    # Explicit user intent always wins; anchor check does NOT apply here.
    for d in new_domains:
        nd = normalize_domain(d) or d.lower()
        if nd in text_lower or d.lower() in text_lower:
            return True, f"user_text_explicit:{nd}"

    # Rule D — anchor domain guard (fires when Rule A did NOT match).
    # If the lock has an anchor, the new domain must be related to it.
    # Prevents silent LLM-driven drift to unrelated domains while allowing
    # same-category transitions (e.g. coinbase → binance for crypto tasks).
    if old_lock.anchor_domain:
        for d in new_domains:
            nd = normalize_domain(d) or d.lower()
            if not _is_related_to_anchor(nd, old_lock.anchor_domain):
                _log_info(
                    "anchor_domain_violation",
                    anchor_domain=old_lock.anchor_domain,
                    attempted_domain=nd,
                    reason="unrelated_to_anchor",
                )
                return False, "anchor_domain_violation"

    # Rule B — new domains share a known category with current lock domains
    old_categories = {
        _SPECIFIC_DOMAIN_CATEGORIES.get(normalize_domain(d) or d.lower(), "")
        for d in old_lock.domains
    }
    old_categories.discard("")
    if old_categories:
        for d in new_domains:
            nd = normalize_domain(d) or d.lower()
            cat = _SPECIFIC_DOMAIN_CATEGORIES.get(nd, "")
            if cat and cat in old_categories:
                return True, f"same_category:{cat}"

    # Rule C — semantic signal: user_text contains category hints that match
    # the new domain's known category (domain-hint in objective)
    for d in new_domains:
        nd = normalize_domain(d) or d.lower()
        cat = _SPECIFIC_DOMAIN_CATEGORIES.get(nd, "")
        if not cat:
            continue
        signals = _CATEGORY_TEXT_SIGNALS.get(cat, frozenset())
        if signals and any(sig in text_lower for sig in signals):
            return True, f"semantic_category_signal:{cat}"

    _log_info(
        "domain_transition_rejected",
        old_domain=old_lock.serialize(),
        new_domains=sorted(normalize_domain(d) or d.lower() for d in new_domains)[:5],
        reason="no_rule_matched",
    )
    return False, "transition_not_authorized"


def _is_hijack_attempt(domain: str, user_text: str, action_type: str) -> bool:
    """Return True if proposing this domain as the first lock is semantically wrong.

    Only fires when:
    1. The domain is in our known-specific category map.
    2. The combined user_text + action_type has ZERO signals for that category.

    Deliberately narrow — avoids false positives on open-ended tasks.
    """
    category = _SPECIFIC_DOMAIN_CATEGORIES.get(domain.lower(), "")
    if not category:
        return False  # unknown domain → assume legitimate, don't block
    signals = _CATEGORY_TEXT_SIGNALS.get(category, frozenset())
    if not signals:
        return False  # no signals defined → can't validate, allow
    text_lower = (user_text + " " + action_type).lower()
    return not any(sig in text_lower for sig in signals)


# ── Redirect escape detection ─────────────────────────────────────────────────
# Browser skills may expose the final effective URL in their output text.
# We scan for common patterns to catch redirect-based domain escapes.
_EFFECTIVE_URL_RE = re.compile(
    r'(?:current[_ ]?url|navigated[_ ]?to|redirected[_ ]?to|final[_ ]?url'
    r'|effective[_ ]?url|landed[_ ]?on|page[_ ]?url)[:\s]+([^\s\n"\'<>]+)',
    re.IGNORECASE,
)
_RAW_URL_IN_OUTPUT_RE = re.compile(
    r'\bURL:\s*(https?://[^\s\n"\'<>]+)',
    re.IGNORECASE,
)


def _extract_effective_url(output: str) -> str:
    """Extract the effective/final URL from browser skill output text."""
    if not output:
        return ""
    for pat in (_EFFECTIVE_URL_RE, _RAW_URL_IN_OUTPUT_RE):
        m = pat.search(output)
        if m:
            url = m.group(1).rstrip(".,;)/")
            if "://" in url:
                return url
    return ""


def check_redirect_escape(results: list, lock: "DomainLock | None") -> tuple[bool, str]:
    """Post-execution check: verify browser results didn't land outside the domain lock.

    Scans browser skill output for effective URL markers and validates against lock.
    Same-domain/subdomain/www redirects are allowed.
    Returns (violated, replan_message).
    """
    if not lock:
        return False, ""
    for result in results:
        if getattr(result, "skill_name", "") != "browser":
            continue
        output = getattr(result, "output", "") or ""
        effective_url = _extract_effective_url(output)
        if not effective_url:
            continue
        effective_domain = _extract_domain(effective_url)
        if effective_domain and not lock.allows(effective_domain):
            replan_msg = (
                f"⛔ [REDIRECT ESCAPE DETECTED] The browser navigated from the locked domain "
                f"({lock.serialize()}) and landed on '{effective_domain}' via redirect. "
                f"This is a domain violation.\n\n"
                f"You MUST replan:\n"
                f"- Navigate only within: {lock.serialize()}\n"
                f"- Do NOT follow external redirect chains\n"
                f"- If the target requires an external domain, report that to the user."
            )
            _log_warn(
                "domain_redirect_blocked",
                old_domain=lock.serialize(),
                new_domain=effective_domain,
                reason="redirect_escaped_domain_lock",
                lock_mode=lock.mode,
            )
            return True, replan_msg
    return False, ""


def extract_domains_from_text(text: str) -> list[str]:
    """Extract domain references from free-form user text.

    Handles:
    - Full URLs: https://biobiochile.cl/page → biobiochile.cl
    - www-prefixed: www.biobiochile.cl → biobiochile.cl (without www)
    - Bare TLD-qualified domains: biobiochile.cl → biobiochile.cl
    - Subdomains: news.biobiochile.cl → news.biobiochile.cl (kept for lock matching)

    Returns list of normalized domains (lowercase, no protocol, no path, no trailing dot).
    First item is highest-priority candidate.
    """
    if not text:
        return []
    domains: list[str] = []
    seen: set[str] = set()

    def _add(d: str) -> None:
        d = d.lower().rstrip("/. ")
        if d and d not in seen:
            seen.add(d)
            domains.append(d)

    # Full URLs first (highest confidence)
    for m in _INVENTED_URL_RE.finditer(text):
        d = _extract_domain(m.group(0))
        if d:
            _add(d)

    # Bare TLD-qualified domains
    for m in _BARE_DOMAIN_RE.finditer(text):
        d = m.group(1).lower()
        _add(d)

    return domains


def _build_domain_lock(
    domains: list,
    source: str,
    confirmed: bool = False,
    anchor_domain: str = "",
) -> "DomainLock":
    """Build a DomainLock from a list of domain strings.

    All domains are root-normalized before storage so that subdomains,
    www prefixes, and ports are stripped.  Logs each normalization when
    the input differs from the normalized form.

    anchor_domain: explicit anchor to carry forward.  When empty and source is
    "user_text", the first normalized domain is auto-set as anchor.
    """
    normed: list[str] = []
    for d in domains:
        if not d:
            continue
        n = normalize_domain(d) or d.lower()
        if n != d.lower():
            _log_info("domain_normalized", original=d, normalized=n)
        normed.append(n)
    fs = frozenset(normed)
    if not fs:
        raise ValueError("_build_domain_lock: empty domain list")
    mode = "multi" if len(fs) > 1 else "single"
    # enforcement: skill_call-derived locks are exploratory (system-inferred);
    # all other sources (user_text, intent, override, persisted) are strict (user-derived).
    enforcement = "exploratory" if source == "skill_call" else "strict"
    # Anchor: auto-set from first domain when source is user_text and no anchor provided.
    if not anchor_domain and source == "user_text" and normed:
        anchor_domain = normed[0]
        _log_info("anchor_domain_set", anchor_domain=anchor_domain, source=source)
    return DomainLock(domains=fs, mode=mode, source=source, confirmed=confirmed,
                      enforcement=enforcement, anchor_domain=anchor_domain)


def _update_domain_lock(
    current_lock: "DomainLock | None",
    new_domains: list,
    signal_source: str,          # "intent" | "user_text" | "skill_call"
    user_text: str = "",         # original user request — used for transition validation
) -> "tuple[DomainLock | None, str]":
    """Evaluate whether to update, replace, or keep the current domain lock.

    Phase 3 — Intelligent Domain Lock Adaptation.

    CASE 1 — strict confirmed: immutable; user must explicitly signal intent change.
    CASE 2 — exploratory confirmed: replace only when transition is valid
             (user_text names the domain, or same-category). See _is_domain_transition_valid.
    CASE 3 — multi-domain confirmed: CLOSED SET — no automatic merging.
             New domain added only if user_text explicitly names it.
    Otherwise (provisional): always replaceable.

    Returns (lock, reason_str).
    """
    if not new_domains:
        return current_lock, "no_new_domains"
    if current_lock is None:
        new_lock = _build_domain_lock(new_domains, signal_source, confirmed=True)
        return new_lock, "new_lock_created"

    # CASE 3 (checked first): multi-domain confirmed — CLOSED SET
    # Fix 2: do NOT auto-merge; only user_text-explicit domains may be added.
    if current_lock.mode == "multi" and current_lock.confirmed:
        new_set = frozenset(d.lower() for d in new_domains if d)
        novel = new_set - current_lock.domains
        if not novel:
            # All requested domains already in the lock — no change needed
            return current_lock, "multi_domain_already_contains"
        # Check which novel domains user_text explicitly authorizes
        text_lower = (user_text or "").lower()
        user_explicit = frozenset(d for d in novel if d in text_lower)
        if user_explicit:
            merged = current_lock.domains | user_explicit
            new_lock = DomainLock(
                domains=merged,
                mode="multi",
                source=signal_source,
                confirmed=True,
                enforcement=current_lock.enforcement,
                anchor_domain=current_lock.anchor_domain,  # preserve anchor across merges
            )
            _log_info(
                "domain_lock_updated",
                old=current_lock.serialize(),
                new=new_lock.serialize(),
                reason="multi_domain_user_explicit_add",
                signal_source=signal_source,
                added=sorted(user_explicit),
            )
            return new_lock, "multi_domain_user_explicit_add"
        # Novel domains not authorized — reject; closed set remains intact
        _log_info(
            "domain_lock_ignored_due_to_strict",
            current=current_lock.serialize(),
            signal_source=signal_source,
            rejected_domains=sorted(novel),
            reason="multi_domain_closed",
        )
        return current_lock, "multi_domain_closed"

    # CASE 1: strict confirmed (single-domain) — immutable regardless of new signal
    if current_lock.confirmed and current_lock.enforcement == "strict":
        _log_info(
            "domain_transition_rejected",
            old_domain=current_lock.serialize(),
            new_domains=[normalize_domain(d) or d.lower() for d in new_domains][:5],
            reason="strict_confirmed_lock_immutable",
            signal_source=signal_source,
        )
        return current_lock, "strict_lock_unchanged"

    # CASE 2: exploratory confirmed — replace only when transition is valid
    # Fix 1: validate via _is_domain_transition_valid before allowing replacement.
    if current_lock.confirmed and current_lock.enforcement == "exploratory":
        if signal_source in ("intent", "user_text"):
            allowed, trans_reason = _is_domain_transition_valid(
                current_lock, new_domains, user_text
            )
            if allowed:
                old_ser = current_lock.serialize()
                # user_text signal → new anchor auto-set; intent signal → preserve old anchor
                _carry_anchor = "" if signal_source == "user_text" else current_lock.anchor_domain
                new_lock = _build_domain_lock(new_domains, signal_source, confirmed=True,
                                              anchor_domain=_carry_anchor)
                _log_info(
                    "domain_lock_replaced",
                    old=old_ser,
                    new=new_lock.serialize(),
                    reason="exploratory_replaced_by_strong_signal",
                    signal_source=signal_source,
                    transition_reason=trans_reason,
                )
                return new_lock, "exploratory_replaced"
            # Transition not valid — keep current exploratory lock
            _log_info(
                "domain_lock_ignored_due_to_strict",
                current=current_lock.serialize(),
                signal_source=signal_source,
                new_domains=list(new_domains)[:5],
                reason=f"exploratory_transition_invalid:{trans_reason}",
            )
            return current_lock, "exploratory_transition_invalid"
        # skill_call signal against exploratory confirmed — no-op
        return current_lock, "exploratory_skill_signal_ignored"

    # Provisional (unconfirmed) — always replaceable
    _carry_anchor = "" if signal_source == "user_text" else current_lock.anchor_domain
    new_lock = _build_domain_lock(new_domains, signal_source, confirmed=False,
                                  anchor_domain=_carry_anchor)
    return new_lock, "provisional_replaced"


# ── Provisional lock confirmation ──────────────────────────────────────────────
# A provisional lock (source="skill_call", confirmed=False) becomes confirmed
# only after earning it via repeat consistency or useful evidence.

# Minimum browser output length to count as "useful evidence" of real content.
_USEFUL_EVIDENCE_MIN_CHARS = 80


def _has_useful_evidence(result) -> bool:
    """Return True if a browser skill result contains substantive page content."""
    if not getattr(result, "success", False):
        return False
    output = (getattr(result, "output", "") or "").strip()
    return len(output) >= _USEFUL_EVIDENCE_MIN_CHARS


def maybe_confirm_lock(
    lock: "DomainLock",
    results: list,
    proposed_domain: str = "",
) -> "tuple[DomainLock, str]":
    """Try to confirm a provisional domain lock after a round of execution.

    Confirmation rules (deterministic, no LLM):
      A. Repeat consistency — a later round proposes or navigates the same domain.
      B. Useful evidence   — execution on this domain returned substantive content.

    Returns (confirmed_lock, reason_str) if confirmed, else (original_lock, "").
    Always a no-op when lock is already confirmed.
    """
    if lock.confirmed:
        return lock, ""

    # Rule A: same domain proposed again in the next round
    if proposed_domain and lock.allows(proposed_domain):
        confirmed = lock.confirm()
        _log_info(
            "domain_lock.confirmed",
            domain=confirmed.serialize(),
            reason="repeat_consistency",
            proposed=proposed_domain,
        )
        return confirmed, "repeat_consistency"

    # Rule B: execution returned substantive content from this domain
    for result in results:
        if getattr(result, "skill_name", "") != "browser":
            continue
        if _has_useful_evidence(result):
            confirmed = lock.confirm()
            _log_info(
                "domain_lock.confirmed",
                domain=confirmed.serialize(),
                reason="useful_evidence",
                output_len=len((getattr(result, "output", "") or "").strip()),
            )
            return confirmed, "useful_evidence"

    return lock, ""


def validate_tool_against_domain_lock(
    tool_name: str,
    tool_args: dict,
    active_lock: "DomainLock | None",
) -> "tuple[bool, str, list[str]]":
    """H6: Enforce domain lock for non-browser tools that can reach external networks.

    Covers: web_search, python_exec, shell.

    Returns:
        (allowed: bool, reason: str, detected_domains: list[str])

    Design principles:
    - Fail-closed: if network access is detected but no domain is extractable → BLOCK.
    - No lock active → always ALLOWED (no constraints to enforce).
    - Exception during extraction → BLOCKED with safe fallback reason.
    """
    if not active_lock:
        return True, "no_lock", []

    try:
        # ── web_search ────────────────────────────────────────────────────────
        if tool_name == "web_search":
            # Collect all string values from args (query, q, search_query, etc.)
            raw_text = " ".join(str(v) for v in tool_args.values() if isinstance(v, str))

            detected: list[str] = []

            # site: directives are the clearest domain signal
            for m in _WEBSEARCH_SITE_RE.finditer(raw_text):
                d = _extract_domain("https://" + m.group(1))
                if d:
                    detected.append(d)

            # Also check inline URLs in the query
            for m in _INVENTED_URL_RE.finditer(raw_text):
                d = _extract_domain(m.group(0))
                if d:
                    detected.append(d)

            if not detected:
                # No site: prefix and no URL — generic query; cannot verify target domain.
                # Fail closed: any unfocused search could escape the domain lock.
                return (
                    False,
                    "web_search_no_domain: query has no site: directive or URL — "
                    "cannot verify it stays within domain lock",
                    [],
                )

            # Verify every detected domain is allowed
            violating = [d for d in detected if not active_lock.allows(d)]
            if violating:
                return (
                    False,
                    f"web_search_domain_violation: query targets {violating} "
                    f"which violates lock {active_lock.serialize()}",
                    detected,
                )
            return True, "web_search_allowed", detected

        # ── python_exec ───────────────────────────────────────────────────────
        if tool_name == "python_exec":
            code = ""
            for k in ("code", "script", "source", "program"):
                if k in tool_args and isinstance(tool_args[k], str):
                    code = tool_args[k]
                    break
            if not code:
                # No code to inspect — treat as safe
                return True, "python_exec_no_code", []

            has_net = bool(_PYEXEC_NET_RE.search(code))
            url_matches = _PYEXEC_URL_RE.findall(code)
            detected = [_extract_domain(u) for u in url_matches]
            detected = [d for d in detected if d]

            if not has_net and not detected:
                # Pure computation — no network access detected
                return True, "python_exec_no_network", []

            if has_net and not detected:
                # Network access detected but domain is dynamic/not extractable — fail closed
                return (
                    False,
                    "python_exec_unsafe_network: network access detected but no URL "
                    "domain extractable — cannot verify against domain lock",
                    [],
                )

            violating = [d for d in detected if not active_lock.allows(d)]
            if violating:
                return (
                    False,
                    f"python_exec_domain_violation: code accesses {violating} "
                    f"which violates lock {active_lock.serialize()}",
                    detected,
                )
            return True, "python_exec_allowed", detected

        # ── shell ─────────────────────────────────────────────────────────────
        if tool_name == "shell":
            cmd = ""
            for k in ("command", "cmd", "shell", "script", "bash"):
                if k in tool_args and isinstance(tool_args[k], str):
                    cmd = tool_args[k]
                    break
            if not cmd:
                return True, "shell_no_command", []

            has_net_tool = bool(_SHELL_NET_TOOLS_RE.search(cmd))
            if not has_net_tool:
                # No network-capable command found — treat as safe
                return True, "shell_no_network_tool", []

            detected: list[str] = []

            # Extract URL-scheme URLs
            for m in _SHELL_URL_RE.finditer(cmd):
                d = _extract_domain(m.group(0))
                if d:
                    detected.append(d)

            if not detected:
                # Try bare hostname arguments (e.g., "ping biobiochile.cl")
                for m in _SHELL_HOST_ARG_RE.finditer(cmd):
                    d = _extract_domain("https://" + m.group(1))
                    if d:
                        detected.append(d)

            if not detected:
                # Network tool present but domain not extractable — fail closed
                return (
                    False,
                    "shell_unsafe_network: network command detected but domain not "
                    "extractable — cannot verify against domain lock",
                    [],
                )

            violating = [d for d in detected if not active_lock.allows(d)]
            if violating:
                return (
                    False,
                    f"shell_domain_violation: command targets {violating} "
                    f"which violates lock {active_lock.serialize()}",
                    detected,
                )
            return True, "shell_allowed", detected

        # ── http_request — CRIT-1 Fix ─────────────────────────────────────────
        # Direct URL-bearing HTTP client: any URL must be within the domain lock.
        # No URL present → fail closed (can't verify target without one).
        if tool_name == "http_request":
            url = tool_args.get("url", "").strip()
            if not url:
                return (
                    False,
                    "http_request_no_url: no URL argument present — cannot verify "
                    "target against domain lock",
                    [],
                )
            d = _extract_domain(url)
            if not d:
                return (
                    False,
                    f"http_request_unextractable_domain: could not extract domain "
                    f"from URL '{url[:80]}' — fail closed under active lock",
                    [],
                )
            if not active_lock.allows(d):
                return (
                    False,
                    f"http_request_domain_violation: URL targets '{d}' which violates "
                    f"lock {active_lock.serialize()}",
                    [d],
                )
            return True, "http_request_allowed", [d]

        # ── fetch_url — CRIT-1 Fix ────────────────────────────────────────────
        # Lightweight HTML fetcher: same domain lock enforcement as http_request.
        # Mirrors the normalization in FetchUrlSkill.execute() (adds https:// prefix).
        if tool_name == "fetch_url":
            url = tool_args.get("url", "").strip()
            if not url:
                return (
                    False,
                    "fetch_url_no_url: no URL argument present — cannot verify "
                    "target against domain lock",
                    [],
                )
            # Normalize the same way the skill itself does
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            d = _extract_domain(url)
            if not d:
                return (
                    False,
                    f"fetch_url_unextractable_domain: could not extract domain "
                    f"from URL '{url[:80]}' — fail closed under active lock",
                    [],
                )
            if not active_lock.allows(d):
                return (
                    False,
                    f"fetch_url_domain_violation: URL targets '{d}' which violates "
                    f"lock {active_lock.serialize()}",
                    [d],
                )
            return True, "fetch_url_allowed", [d]

    except Exception as _vt_err:
        # Fail closed on any unexpected error during extraction
        return (
            False,
            f"validate_tool_exception_fail_closed: {str(_vt_err)[:80]}",
            [],
        )

    # Tool not in the controlled set — no domain restriction applies
    return True, "tool_not_domain_controlled", []


# ── HIGH-2: Domain lock persistence helpers ───────────────────────────────────
# Serializes/deserializes DomainLock to/from a JSON blob for Redis storage.
# Only CONFIRMED locks are persisted (provisional locks are too speculative to
# carry forward to the next turn).

import json as _json

_DOMAIN_LOCK_REDIS_TTL = 60    # 1 minute of inactivity clears the cross-turn lock
                                # (was 600s — caused multi-turn topic contamination after
                                #  a single browser failure: subsequent unrelated turns
                                #  loaded + auto-refreshed the stale lock indefinitely)
_DOMAIN_LOCK_KEY_PREFIX = "domain_lock:"


def _lock_to_json(lock: "DomainLock") -> str:
    """Serialize a DomainLock to a JSON string suitable for Redis storage."""
    return _json.dumps({
        "domains": sorted(lock.domains),
        "mode": lock.mode,
        "source": "persisted",          # source becomes "persisted" on reload
        "confirmed": True,              # only confirmed locks are stored
        "enforcement": lock.enforcement,
        "anchor_domain": lock.anchor_domain,
    })


def _lock_from_json(data: str) -> "DomainLock | None":
    """Deserialize a DomainLock from a JSON string. Returns None if invalid."""
    try:
        obj = _json.loads(data)
        domains = frozenset(str(d).strip() for d in obj.get("domains", []) if d)
        if not domains:
            return None
        return DomainLock(
            domains=domains,
            mode=str(obj.get("mode", "single")),
            source="persisted",
            confirmed=True,
            enforcement=str(obj.get("enforcement", "strict")),
            anchor_domain=str(obj.get("anchor_domain", "")),
        )
    except Exception:
        return None


async def persist_domain_lock(redis_url: str, chat_id: str, lock: "DomainLock | None") -> None:
    """Save a confirmed DomainLock to Redis.  No-op if lock is None or unconfirmed."""
    if not lock or not lock.confirmed or not redis_url or not chat_id:
        return
    try:
        import redis.asyncio as _aioredis
        _r = _aioredis.from_url(redis_url, decode_responses=True)
        try:
            key = f"{_DOMAIN_LOCK_KEY_PREFIX}{chat_id}"
            await _r.setex(key, _DOMAIN_LOCK_REDIS_TTL, _lock_to_json(lock))
            _log_info(
                "domain_lock.persisted",
                chat_id=chat_id[:20],
                domain=lock.serialize(),
                ttl=_DOMAIN_LOCK_REDIS_TTL,
            )
        finally:
            await _r.aclose()
    except Exception as _pe:
        _log_warn("domain_lock.persist_failed", error=str(_pe)[:80])


async def load_domain_lock(redis_url: str, chat_id: str) -> "DomainLock | None":
    """Load a persisted DomainLock from Redis.  Returns None on miss or error."""
    if not redis_url or not chat_id:
        return None
    try:
        import redis.asyncio as _aioredis
        _r = _aioredis.from_url(redis_url, decode_responses=True)
        try:
            key = f"{_DOMAIN_LOCK_KEY_PREFIX}{chat_id}"
            raw = await _r.get(key)
        finally:
            await _r.aclose()
        if not raw:
            return None
        lock = _lock_from_json(raw)
        if lock:
            _log_info(
                "domain_lock.loaded",
                chat_id=chat_id[:20],
                domain=lock.serialize(),
            )
        return lock
    except Exception as _le:
        _log_warn("domain_lock.load_failed", error=str(_le)[:80])
        return None


async def clear_domain_lock(
    redis_url: str,
    chat_id: str,
    reason: str = "explicit_clear",
    old_domain: str = "",
) -> None:
    """Remove a persisted domain lock.

    Args:
      reason    — short string logged with the clear event (e.g. "new_turn_domain_mismatch")
      old_domain — the domain string that was cleared (for the log entry)
    """
    if not redis_url or not chat_id:
        return
    try:
        import redis.asyncio as _aioredis
        _r = _aioredis.from_url(redis_url, decode_responses=True)
        try:
            await _r.delete(f"{_DOMAIN_LOCK_KEY_PREFIX}{chat_id}")
        finally:
            await _r.aclose()
        _log_info(
            "domain_lock_cleared",
            old_domain=old_domain or "(unknown)",
            new_domain="(none)",
            reason=reason,
        )
    except Exception:
        pass


# Verbs/nouns that mean the user is talking about a DIFFERENT subsystem entirely
# (task scheduling, email, package tracking, agent management, etc.).
# When any of these appear in a new turn, the previous browser lock is stale.
# These intents do not depend on the previously locked domain at all.
_NON_BROWSER_INTENT_SIGNALS = (
    # Task / scheduling
    "tarea", "tareas", "schedule", "scheduled", "programada", "programar",
    "ajusta la tarea", "ajustar la tarea", "modificar la tarea", "edit task",
    "task_manager", "cron", "diariamente", "diario", "cada dia", "cada día",
    "cada hora", "every hour", "every day",
    # Reminders
    "recordatorio", "recordatorios", "reminder", "reminders", "recuérdame", "recuerda me",
    # Email
    "correo", "envíame un correo", "enviar correo", "mándame un correo",
    "send email", "email me", "gmail", "@",
    # Package tracking
    "rastrear", "rastreo", "tracking", "paquete", "package", "envio", "envío",
    "encomienda", "courier",
    # Agent management
    "crear agente", "crear sub-agente", "agent_manager", "delete agent",
    # General "ajusta/cambia/hazlo/quítalo" with no URL → likely meta-control, not browser
    "ajusta", "ajustar", "cambia", "cambiar", "actualiza", "modifica",
    "elimina", "borra", "quita", "remove", "delete",
)

# Greeting / acknowledgement patterns that should always clear stale browser locks
_NEW_TURN_TOPIC_RESET = (
    "hazme", "házme", "hazlo", "házlo", "ahora", "perfecto", "ok", "listo",
    "hola", "gracias",
)


def validate_lock_for_reuse(
    lock: "DomainLock",
    user_text: str,
) -> "tuple[DomainLock | None, str]":
    """Validate a persisted lock against a new turn's user text.

    Called when loading a cross-turn lock before reusing it.  Prevents a lock
    earned in a previous task from silently constraining an unrelated new task.

    Decision logic (in priority order):
      1. Any locked domain appears literally in user_text         → KEEP (explicit reuse)
      2. Lock domain's category matches user_text category signals → KEEP (topically same)
      3. New turn signals a non-browser intent (task/email/etc.) → CLEAR
      4. User_text names a DIFFERENT domain                       → CLEAR (new task)
      5. No domain signal in user_text at all                     → CLEAR (default permissive)
                                                                    (was: keep — too lax;
                                                                     caused multi-turn
                                                                     contamination)

    Returns (lock_or_None, reason_str).
    """
    if not lock:
        return None, "no_lock"
    if not user_text:
        return lock, "no_user_text"

    text_lower = user_text.lower()

    # 1. Explicit domain reference in new user text → keep
    for d in lock.domains:
        if d in text_lower:
            return lock, "reuse_ok_explicit"

    # 2. Category signals still present in user text → keep
    for d in lock.domains:
        cat = _SPECIFIC_DOMAIN_CATEGORIES.get(d, "")
        if cat:
            signals = _CATEGORY_TEXT_SIGNALS.get(cat, frozenset())
            if signals and any(sig in text_lower for sig in signals):
                return lock, "reuse_ok_category_signal"

    # 3. New turn signals a non-browser intent → clear
    if any(sig in text_lower for sig in _NON_BROWSER_INTENT_SIGNALS):
        _log_info(
            "domain_lock_cleared",
            old_domain=lock.serialize(),
            new_domain="(non-browser intent)",
            reason="new_turn_non_browser_intent",
        )
        return None, "cleared:new_turn_non_browser_intent"

    # 4. User text names a DIFFERENT domain → clear, new task starting
    new_domains = extract_domains_from_text(user_text)
    if new_domains:
        _log_info(
            "domain_lock_cleared",
            old_domain=lock.serialize(),
            new_domain=",".join(new_domains[:3]),
            reason="new_turn_different_domain",
        )
        return None, "cleared:new_turn_different_domain"

    # 5. No domain signal at all → clear (permissive default).
    #    The previous "keep on no signal" behavior caused short follow-ups like
    #    "hazlo", "perfecto", "gracias" to silently extend a stale lock for
    #    another 600s, blocking subsequent unrelated browser navigations.
    if any(tok in text_lower.split() for tok in _NEW_TURN_TOPIC_RESET):
        _log_info(
            "domain_lock_cleared",
            old_domain=lock.serialize(),
            new_domain="(short reply)",
            reason="new_turn_short_reply_no_signal",
        )
        return None, "cleared:short_reply_no_signal"

    # Otherwise keep — likely a continuation of the same flow
    return lock, "reuse_ok_no_signal"


def pre_execution_check(
    skill_calls: list,
    action_intent,                      # _ActionIntent | _InferredIntent — NEVER None at call site
    user_text: str = "",                # original user request (for domain extraction)
    domain_lock_override=None,          # DomainLock | str | None — persistent lock from ctx
    last_confirmed_domain: str = "",    # B1 fix — follow-up domain lock
) -> PreExecResult:
    """Hard pre-execution validation for all skill calls.

    Priority 2: Universal Domain Lock (hardened).

    Lock derivation priority:
      (1) persistent override  — already confirmed in a previous round
      (2) intent domain        — from named_site_url or action_target URL
      (3) user text            — all domains explicitly named by user
                                  → multi-domain lock when multiple present
      (4) first browser call   — provisional lock; validated for semantic fit
      (5) no lock              — open-ended task; allow without premature lock

    Checks in order:
      1. Tool hard-block (wrong skill family for task type)
      2. Universal domain lock (ALL browser navigate calls)
         — multi-domain lock allows any domain in the set
         — provisional lock blocked if domain category mismatches task
      3. Browser sub-action match

    Returns PreExecResult with active_domain_lock=DomainLock for caller to persist.
    Raises ValueError if action_intent is None (caller contract violation).
    """
    if action_intent is None:
        raise ValueError(
            "pre_execution_check: action_intent must not be None — "
            "use infer_intent_from_text() first"
        )

    action_type = getattr(action_intent, "action_type", "") or ""
    intent_target = getattr(action_intent, "action_target", "") or ""
    intent_named_url = getattr(action_intent, "named_site_url", "") or ""
    intent_domain = _extract_domain(intent_named_url) or _extract_domain(intent_target)

    # ── Normalise override to DomainLock ──────────────────────────────────────
    # Accept DomainLock or legacy str.  Preserve confirmed/provisional state.
    active_lock: DomainLock | None = None
    _is_provisional_override = False          # True → override is provisional, replaceable
    if isinstance(domain_lock_override, DomainLock) and domain_lock_override:
        if domain_lock_override.confirmed:
            # Confirmed — keep as-is, preserving enforcement; mark source as "override"
            active_lock = DomainLock(
                domains=domain_lock_override.domains,
                mode=domain_lock_override.mode,
                source="override",
                confirmed=True,
                enforcement=domain_lock_override.enforcement,
                anchor_domain=domain_lock_override.anchor_domain,
            )
        else:
            # Provisional from a previous round — carry forward but allow replacement.
            # Rule A: if the new skill calls navigate to the same domain → confirm now.
            _proposed_nav = ""
            for _sc in skill_calls:
                if _sc.skill_name == "browser":
                    _sc_args = _sc.arguments if isinstance(
                        getattr(_sc, "arguments", None), dict
                    ) else {}
                    if _sc_args.get("action", "").lower() == "navigate":
                        _proposed_nav = _extract_domain(_sc_args.get("url", ""))
                        break
            if _proposed_nav and domain_lock_override.allows(_proposed_nav):
                # Repeat consistency — confirm before proceeding
                active_lock = DomainLock(
                    domains=domain_lock_override.domains,
                    mode=domain_lock_override.mode,
                    source=domain_lock_override.source,
                    confirmed=True,
                    enforcement=domain_lock_override.enforcement,
                    anchor_domain=domain_lock_override.anchor_domain,
                )
                _log_info(
                    "domain_lock.confirmed",
                    domain=active_lock.serialize(),
                    reason="repeat_consistency",
                    proposed=_proposed_nav,
                )
            else:
                active_lock = DomainLock(
                    domains=domain_lock_override.domains,
                    mode=domain_lock_override.mode,
                    source=domain_lock_override.source,
                    confirmed=False,
                    enforcement=domain_lock_override.enforcement,
                    anchor_domain=domain_lock_override.anchor_domain,
                )
                _is_provisional_override = True
    elif isinstance(domain_lock_override, str) and domain_lock_override:
        # Legacy str path — treat as confirmed
        active_lock = _build_domain_lock([domain_lock_override], "override", confirmed=True)

    # ── Priority 2: intent domain (replaces provisional or exploratory confirmed) ─
    # Intent/user sources are always confirmed immediately.
    if intent_domain:
        if active_lock is None:
            active_lock = _build_domain_lock([intent_domain], "intent", confirmed=True)
        elif _is_provisional_override and not active_lock.allows(intent_domain):
            _old_ser = active_lock.serialize()
            active_lock = _build_domain_lock([intent_domain], "intent", confirmed=True)
            _is_provisional_override = False
            _log_info(
                "domain_lock.replaced",
                old=_old_ser, new=active_lock.serialize(),
                reason="intent_supersedes_provisional",
            )
        elif (active_lock.confirmed and active_lock.enforcement == "exploratory"
              and not active_lock.allows(intent_domain)):
            # Phase 3: exploratory confirmed lock — intent signal can replace it (if valid)
            active_lock, _upd_reason = _update_domain_lock(
                active_lock, [intent_domain], "intent", user_text=user_text
            )
            _is_provisional_override = False

    # ── Priority 3: user text domains (multi-domain; replaces provisional/exploratory) ─
    _check_user_text = (
        active_lock is None
        or _is_provisional_override
        or (active_lock.confirmed and active_lock.enforcement == "exploratory")
    )
    if _check_user_text and user_text:
        text_domains = extract_domains_from_text(user_text)
        if text_domains:
            new_lock = _build_domain_lock(text_domains, "user_text", confirmed=True)
            if active_lock is None:
                active_lock = new_lock
            elif _is_provisional_override and not all(
                active_lock.allows(d) for d in new_lock.domains
            ):
                _log_info(
                    "domain_lock.replaced",
                    old=active_lock.serialize(), new=new_lock.serialize(),
                    reason="user_text_supersedes_provisional",
                )
                active_lock = new_lock
                _is_provisional_override = False
            elif (active_lock.confirmed and active_lock.enforcement == "exploratory"
                  and not all(active_lock.allows(d) for d in new_lock.domains)):
                # Phase 3: exploratory confirmed — user_text signal can replace it (if valid)
                active_lock, _upd_reason = _update_domain_lock(
                    active_lock, list(text_domains), "user_text", user_text=user_text
                )
                _is_provisional_override = False

    # ── Priority 4: first browser navigate call (provisional) ─────────────────
    # Fires when: (a) no lock exists yet, OR (b) current provisional can be replaced.
    # Hijack protection: domains with category mismatch vs user_text are blocked.
    provisional_hijack_blocked = False
    provisional_hijack_domain = ""
    if active_lock is None or _is_provisional_override:
        for _call in skill_calls:
            if _call.skill_name != "browser":
                continue
            _args = _call.arguments if isinstance(
                getattr(_call, "arguments", None), dict
            ) else {}
            if _args.get("action", "").lower() == "navigate" and _args.get("url"):
                _d = _extract_domain(_args["url"])
                if not _d:
                    break
                if active_lock is None:
                    # No lock yet — hijack check applies
                    if _is_hijack_attempt(_d, user_text, action_type):
                        provisional_hijack_blocked = True
                        provisional_hijack_domain = _d
                    else:
                        active_lock = _build_domain_lock([_d], "skill_call", confirmed=False)
                        _log_info(
                            "domain_lock.provisional_created",
                            domain=active_lock.serialize(),
                            action_type=action_type or "(none)",
                        )
                elif _is_provisional_override and not active_lock.allows(_d):
                    # Provisional override — consider replacement
                    if not _is_hijack_attempt(_d, user_text, action_type):
                        _old_ser = active_lock.serialize()
                        active_lock = _build_domain_lock([_d], "skill_call", confirmed=False)
                        _log_info(
                            "domain_lock.replaced",
                            old=_old_ser, new=active_lock.serialize(),
                            reason="new_provisional_candidate",
                        )
                    # If hijack: keep old provisional; Check 2 will block the navigation
                break

    if active_lock:
        _log_info(
            "domain_lock.activated",
            domain=active_lock.serialize(),
            mode=active_lock.mode,
            source=active_lock.source,
            confirmed=active_lock.confirmed,
            action_type=action_type or "(none)",
        )

    # ── Validate each skill call ───────────────────────────────────────────────
    blocked_skills: list[str] = []
    violation_type = ""
    violation_detail = ""
    hard_blocked = _HARD_BLOCKED_TOOLS.get(action_type, frozenset())

    # Hijack check fires before per-call loop
    if provisional_hijack_blocked:
        blocked_skills.append("browser")
        violation_type = "hijack"
        violation_detail = (
            f"Navigation to '{provisional_hijack_domain}' was blocked: "
            f"this domain does not match the task context ('{action_type or user_text[:60]}'). "
            f"Choose a domain appropriate for the user's request."
        )
        _log_warn(
            "domain_lock.hijack_blocked",
            domain=provisional_hijack_domain,
            action_type=action_type or "(none)",
            user_text_preview=(user_text or "")[:60],
        )

    # Phase 3 — pre-compute user URL signals once for the URL-substitution check.
    _user_well_domains, _user_bad_url_tokens = _extract_url_signals_from_user_text(user_text or "")
    # B1 fix: when the user message has no URL, treat the last confirmed
    # domain (saved after a successful explicit screenshot) as the implicit
    # target. This stops the "haz lo mismo / sácale más capturas" path from
    # silently navigating to whatever's hot in active_flow.
    if not _user_well_domains and last_confirmed_domain:
        _user_well_domains = {last_confirmed_domain.lower()}
        _log_info(
            "url_substitution.followup_lock",
            last_confirmed_domain=last_confirmed_domain,
            user_text_preview=(user_text or "")[:60],
        )
    _URL_BEARING_SKILLS = frozenset({
        "browser", "fetch_url", "scrape", "http_request", "browser_screenshot_full_page",
    })
    if user_text and (_user_well_domains or _user_bad_url_tokens):
        _log_info(
            "url_substitution.precheck",
            user_text_preview=(user_text or "")[:80],
            user_well=sorted(_user_well_domains)[:3],
            user_bad=sorted(_user_bad_url_tokens)[:3],
            n_calls=len(skill_calls),
            skills=[c.skill_name for c in skill_calls],
        )

    for call in skill_calls:
        if call.skill_name in blocked_skills:
            continue  # already blocked
        skill_name = call.skill_name
        args = call.arguments if isinstance(getattr(call, "arguments", None), dict) else {}

        # ── Check 0: URL substitution guard (Phase 3) ─────────────────────────
        # If the user's text mentions one or more URLs/domains AND the LLM is
        # calling a URL-bearing skill with a URL whose domain ISN'T in that
        # set — the LLM is silently substituting. Block hard. The replan
        # message asks the LLM to confirm with the user instead.
        if skill_name in _URL_BEARING_SKILLS and (_user_well_domains or _user_bad_url_tokens):
            _call_url = args.get("url", "") or args.get("target", "") or ""
            if _call_url:
                _call_dom = _extract_domain(_call_url)
                if _call_dom and _user_well_domains and not _domain_matches_user(
                    _call_dom, _user_well_domains
                ):
                    blocked_skills.append(skill_name)
                    violation_type = "url_substitution"
                    violation_detail = (
                        f"⛔ [PRE-EXECUTION BLOCK] URL substitution detected.\n"
                        f"User text references: {sorted(_user_well_domains)[:3]} "
                        + (f"(plus malformed tokens: {sorted(_user_bad_url_tokens)[:2]})\n"
                           if _user_bad_url_tokens else "\n")
                        + f"Your call uses: '{_call_dom}' — NOT in the user's set.\n"
                        f"Do NOT substitute or 'auto-correct' URLs. If the user's URL is malformed, "
                        f"ASK them to confirm. Reply with a clarifying question instead of making a tool call."
                    )
                    _log_warn(
                        "url_substitution.blocked",
                        skill=skill_name,
                        call_domain=_call_dom,
                        user_well_domains=sorted(_user_well_domains)[:3],
                        user_bad_tokens=sorted(_user_bad_url_tokens)[:3],
                    )
                    continue
                # Pure-malformed case: user text only contains a malformed token,
                # no clean domain — call MUST be empty/abandoned, never invented.
                if (not _user_well_domains) and _user_bad_url_tokens and _call_dom:
                    blocked_skills.append(skill_name)
                    violation_type = "url_substitution"
                    violation_detail = (
                        f"⛔ [PRE-EXECUTION BLOCK] URL substitution from malformed input.\n"
                        f"User text contains malformed URL tokens: {sorted(_user_bad_url_tokens)[:2]}.\n"
                        f"Your call invented '{_call_dom}'. Do NOT auto-correct typos.\n"
                        f"Reply asking the user to confirm the correct URL."
                    )
                    _log_warn(
                        "url_substitution.blocked_typo",
                        skill=skill_name,
                        call_domain=_call_dom,
                        user_bad_tokens=sorted(_user_bad_url_tokens)[:3],
                    )
                    continue

        # ── Check 1: Hard-blocked tool ────────────────────────────────────────
        if skill_name in hard_blocked:
            blocked_skills.append(skill_name)
            violation_type = "wrong_tool"
            violation_detail = (
                f"'{skill_name}' is not allowed for task type '{action_type}'. "
                f"Use the browser skill instead."
            )
            continue

        # ── Check 2: Universal domain lock ────────────────────────────────────
        # active_lock.allows() handles multi-domain sets and subdomain rules.
        # H4 Fix: check ALL browser actions that carry a URL, not only "navigate".
        # fill/click/submit with off-domain URLs are bypass vectors if unchecked.
        _BROWSER_URL_ACTIONS = frozenset({"navigate", "fill", "click", "submit", "capture"})
        if skill_name == "browser" and active_lock:
            call_action = args.get("action", "").lower()
            call_url = args.get("url", "")
            if call_url and call_action in _BROWSER_URL_ACTIONS:
                call_domain = _extract_domain(call_url)
                if call_domain and not active_lock.allows(call_domain):
                    blocked_skills.append(skill_name)
                    violation_type = "domain_drift"
                    violation_detail = (
                        f"Browser action '{call_action}' to '{call_domain}' blocked. "
                        f"Active domain lock: '{active_lock.serialize()}' "
                        f"(mode: {active_lock.mode}, source: {active_lock.source}). "
                        f"Stay within {active_lock.serialize()} to complete the task."
                    )
                    _log_warn(
                        "domain_lock.blocked_mismatch",
                        expected=active_lock.serialize(),
                        actual=call_domain,
                        action=call_action,
                        mode=active_lock.mode,
                        source=active_lock.source,
                        action_type=action_type or "(none)",
                    )
                    continue

        # ── Check 2b: H6 + CRIT-1 — Non-browser tool domain lock ────────────
        # web_search / python_exec / shell / http_request / fetch_url must all
        # respect the active domain lock.  Fail-closed on exception.
        if skill_name in (
            "web_search", "python_exec", "shell", "http_request", "fetch_url"
        ) and active_lock:
            _vt_allowed, _vt_reason, _vt_domains = validate_tool_against_domain_lock(
                skill_name, args, active_lock
            )
            if not _vt_allowed:
                blocked_skills.append(skill_name)
                violation_type = "domain_lock_tool_violation"
                violation_detail = (
                    f"⛔ [PRE-EXECUTION BLOCK] '{skill_name}' violates domain lock. "
                    f"Reason: {_vt_reason}. "
                    f"Active lock: '{active_lock.serialize()}' "
                    f"(mode: {active_lock.mode}, source: {active_lock.source}). "
                    f"Reformulate the call to stay within the locked domain."
                )
                _log_warn(
                    "domain_lock.tool_violation",
                    tool_name=skill_name,
                    detected_domains=_vt_domains,
                    active_lock=active_lock.serialize(),
                    lock_source=active_lock.source,
                    reason=_vt_reason[:120],
                )
                continue

        # ── Check 3: Browser sub-action match ─────────────────────────────────
        if skill_name == "browser" and action_type in _ALLOWED_BROWSER_ACTIONS:
            call_action = args.get("action", "").lower()
            allowed_actions = _ALLOWED_BROWSER_ACTIONS[action_type]
            if call_action and call_action not in allowed_actions:
                blocked_skills.append(skill_name)
                violation_type = "wrong_action"
                violation_detail = (
                    f"Browser action '{call_action}' is not allowed for task type '{action_type}'. "
                    f"Allowed actions: {sorted(allowed_actions)}."
                )
                continue

    lock_source = active_lock.source if active_lock else ""

    if not blocked_skills:
        _log_info(
            "pre_execution.allowed",
            action_type=action_type or "(none)",
            skills=[c.skill_name for c in skill_calls],
            active_lock=active_lock.serialize() if active_lock else "(none)",
            lock_mode=active_lock.mode if active_lock else "(none)",
        )
        return PreExecResult(
            blocked=False,
            reason="allowed",
            violation_type="",
            blocked_skills=[],
            replan_message="",
            active_domain_lock=active_lock,
            domain_lock_source=lock_source,
        )

    # Build replan message
    lock_desc = active_lock.serialize() if active_lock else "(none)"
    replan_msg = (
        f"⛔ [PRE-EXECUTION BLOCK] The following action was blocked before execution:\n"
        f"Skills blocked: {blocked_skills}\n"
        f"Violation: {violation_detail}\n\n"
        f"You MUST revise your approach:\n"
        f"- Active domain lock: {lock_desc} (source: {lock_source or 'unknown'})\n"
        f"- Use only tools appropriate for task type: {action_type or '(unknown)'}\n"
        f"- Generate a corrected action that stays within the locked domain."
    )

    _log_warn(
        "pre_execution_guard.blocked",
        violation_type=violation_type,
        blocked_skills=blocked_skills,
        action_type=action_type or "(none)",
        active_lock=lock_desc,
        lock_source=lock_source or "(none)",
        detail=violation_detail[:120],
    )

    return PreExecResult(
        blocked=True,
        reason=violation_detail,
        violation_type=violation_type,
        blocked_skills=blocked_skills,
        replan_message=replan_msg,
        active_domain_lock=active_lock,
        domain_lock_source=lock_source,
    )


# ── Priority 1: Failure-Path Gate ─────────────────────────────────────────────
# Validates responses when execution was attempted but did NOT succeed.
# Symmetric to the success-path validation in enforce_response_contract().
# Fires regardless of response length — length is NEVER a reason to skip.
# Fires regardless of action_intent presence — results alone are sufficient signal.

# ── Pattern: Explicit success claims ──────────────────────────────────────────
# Phrases that directly claim completion when execution failed.
_FAILURE_SUCCESS_CLAIM_RE = re.compile(
    r"(?:"
    r"(?:tiene|hay|existe|there\s+is|there\s+are)\s+"
    r"(?:disponibilidad|disponible[s]?|availability|available)\b"
    r"|(?:la\s+cita|the\s+appointment|el\s+turno|the\s+slot)\s+"
    r"(?:está|es|is|fue|was|quedó|has\s+been)\s+"
    r"(?:confirmada?|agendada?|programada?|reservada?|confirmed|scheduled|booked)"
    r"|(?:encontré|encontramos|I\s+found|we\s+found|hallé|obtuve|extraje)\s+"
    r"(?:los?|las?|un|una|the|a|an)\s+"
    r"(?:horarios?|citas?|turnos?|resultados?\s+de\s+disponibilidad|schedule[s]?|appointment[s]?|slot[s]?)"
    r")",
    re.IGNORECASE,
)

# ── Pattern: Implicit success claims ──────────────────────────────────────────
# Softer phrasing that implies success without stating it explicitly.
# Extended to cover: request-was-processed forms, quedó-registered forms,
# process-was-successful forms, and data-was-sent forms.
_IMPLICIT_SUCCESS_RE = re.compile(
    r"(?:"
    r"ya\s+está\s+(?:listo|disponible|completado|hecho|configurado|generado|procesado)"
    r"|se\s+(?:completó|realizó|generó|ejecutó|procesó|finalizó|envió)\s+"
    r"(?:correctamente|exitosamente|con\s+éxito|satisfactoriamente|successfully)"
    r"|el\s+proceso\s+(?:finalizó|terminó|se\s+completó|ha\s+finalizado)"
    r"|todo\s+quedó\s+(?:configurado|listo|completado|procesado|registrado)"
    r"|ya\s+tienes?\s+(?:disponible|listo|acceso|tu\s+cita)"
    r"|la\s+captura\s+(?:fue|está|quedó)\s+(?:generada?|lista?|completada?|disponible)"
    r"|(?:tu|su|la)\s+(?:solicitud|reserva|cita|pedido|petición)\s+"
    r"(?:fue|ha\s+sido)\s+(?:procesada?|registrada?|confirmada?|enviada?|recibida?|aceptada?)"
    r"|quedó\s+(?:registrado?a?|confirmado?a?|agendado?a?|reservado?a?|guardado?a?)"
    r"|el\s+(?:agendamiento|registro|proceso)\s+"
    r"(?:fue|es|está|ha\s+sido)\s+(?:exitoso?|confirmado?|completado?|correcto?)"
    r"|los?\s+datos?\s+(?:fueron|han\s+sido)\s+(?:enviados?|guardados?|registrados?|procesados?)"
    r")",
    re.IGNORECASE,
)

# ── Pattern: Invented clinic/business schedule hours ──────────────────────────
# e.g. "de 8am a 7pm", "lunes a viernes de 9 a 18h"
_INVENTED_SCHEDULE_RE = re.compile(
    r"(?:"
    r"(?:de|from)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|h\b|hs\b|hrs?\b)?"
    r"\s+(?:a|to|hasta)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|h\b|hs\b|hrs?\b)"
    r"|(?:lunes\s+a\s+viernes|monday\s+(?:to|through)\s+friday)"
    r"\s+(?:de|from|entre|between)?\s*\d{1,2}"
    r")",
    re.IGNORECASE,
)

# ── Pattern: Generic availability claims ──────────────────────────────────────
# Broader than _FAILURE_SUCCESS_CLAIM_RE — catches less explicit forms.
_GENERIC_AVAILABILITY_RE = re.compile(
    r"(?:"
    r"hay\s+(?:horas?|citas?|turnos?|cupos?|espacios?)\s+disponibles?"
    r"|existen?\s+(?:horas?|citas?|turnos?|cupos?|espacios?)"
    r"|hay\s+disponibilidad\b"
    r"|quedan?\s+(?:horas?|citas?|cupos?|espacios?|lugares?)"
    r"|puedes?\s+agendar\s+(?:para|el|la|un|una|mañana|hoy|esta\s+semana)"
    r")",
    re.IGNORECASE,
)

# ── Pattern: Specific appointment time references ─────────────────────────────
# Only flagged when combined with scheduling context (see Check 7 below).
# "el sitio falló a las 10am" is NOT blocked — no scheduling context.
# "puedes agendar a las 10am" IS blocked — has scheduling context.
_TIME_SPECIFIC_RE = re.compile(
    r"(?:"
    r"(?:mañana|tomorrow|lunes|martes|miércoles|jueves|viernes|"
    r"el\s+(?:próximo|siguiente)\s+\w+)\s+a\s+las?\s+\d{1,2}(?::\d{2})?"
    r"|a\s+las?\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)"
    r"|entre\s+(?:las?\s+)?\d{1,2}\s+y\s+(?:las?\s+)?\d{1,2}\s+horas?"
    r")",
    re.IGNORECASE,
)

# ── Pattern: Domain generalizations (BLOCKED) ─────────────────────────────────
# LLM falls back to invented general domain knowledge after failure.
# Deliberately narrow to avoid blocking operational suggestions:
#   BLOCKED: "normalmente en clínicas..." / "para agendar en línea puedes ir..."
#   ALLOWED: "puedes intentar nuevamente" / "puedo intentar otra estrategia"
_GENERAL_ADVICE_PIVOT_RE = re.compile(
    r"(?:"
    r"(?:normalmente|normally|generalmente|generally|por\s+lo\s+general|typically|usually)"
    r"\s+(?:en|in)\s+(?:las?|los?|estas?|the)\s+"
    r"(?:clínicas?|hospitales?|centros?\s+médicos?|consultorios?|clinics?|hospitals?|farmacias?)"
    r"|(?:para\s+agendar|to\s+schedule|para\s+reservar|to\s+book)"
    r"\s+(?:(?:una\s+)?cita|(?:an\s+)?appointment)\s+(?:en\s+línea|online|puedes|you\s+can)"
    r"|(?:puedes|you\s+can)\s+(?:ir|acceder|contactar|llamar|go|access|contact|call)"
    r"\s+(?:directamente|directly)\s+(?:a\s+la?|to\s+the)\s+"
    r"(?:sección|section|página|page|portal|sitio\s+web|website)"
    r")",
    re.IGNORECASE,
)

# ── Pattern: Named institutions (for claim-without-evidence check) ─────────────
_NAMED_INSTITUTION_RE = re.compile(
    r"\b(?:Clínica|Hospital|Centro\s+(?:Médico|de\s+Salud)|"
    r"Farmacia|Instituto\s+Nacional|Consultorio)\s+"
    r"[A-ZÁÉÍÓÚÑ][A-Za-záéíóúñ]{2,25}",
)

# ── Pattern: Invented confirmation/booking IDs ────────────────────────────────
# Confirmation/booking IDs with actual values are ALWAYS hallucinations on
# a failed execution — the system never obtained a real confirmation.
# Requires an actual value (alphanumeric ID) after the keyword to avoid
# false positives on "No pude obtener el número de confirmación".
_INVENTED_CONFIRMATION_RE = re.compile(
    r"(?:"
    # "número/código de confirmación es: AB123" or "número de confirmación: AB123"
    r"(?:número|código|folio|id|referencia)\s+de\s+"
    r"(?:confirmación|reserva|cita|booking|referencia)"
    r"(?:\s+\w+)?\s*[:=]\s*[A-Z][A-Z0-9\-]{2,}"
    # "número de confirmación es AB123" (separator without colon)
    r"|(?:número|código|folio|id|referencia)\s+de\s+"
    r"(?:confirmación|reserva|cita|booking|referencia)"
    r"\s+(?:es|fue|será)\s+[A-Z][A-Z0-9\-]{2,}"
    r"|(?:confirmation|booking|reference)\s+(?:number|code|id)"
    r"\s*[:=]\s*[A-Z0-9][A-Z0-9\-]{2,}"
    r")",
    re.IGNORECASE,
)

# ── Partial evidence quality helpers ─────────────────────────────────────────
# Used to select the most informative line from raw skill output.
# Filters HTML/JSON structure noise; prefers navigation/page-state keywords.
_HTML_NOISE_RE = re.compile(r'[<>{}\[\]|\\]{2,}|^\s*[<>{}]', re.MULTILINE)
_NAV_STATE_KW_RE = re.compile(
    r'\b(?:naveg|navigat|load|redirect|login|auth|página|page|'
    r'formulario|form|error|block|acceso|portal|sitio|url|'
    r'session|cookie|forbidden|denied|require[sd]?|timeout)\b',
    re.IGNORECASE,
)


def _select_best_partial(output: str, max_chars: int = 200) -> str:
    """Select the most informative snippet from raw skill output.

    Filters raw HTML/JSON structure lines. Prefers lines with
    navigation/page-state keywords over generic content.
    Falls back to first clean line if no keyword match found.
    """
    lines = [l.strip() for l in output.split('\n') if l.strip() and len(l.strip()) > 8]
    clean = [l for l in lines if not _HTML_NOISE_RE.search(l)]
    if not clean:
        return (lines[0] if lines else output.strip())[:max_chars]

    # Score: nav/state keywords = 2, anything else = 1
    def _score(line: str) -> int:
        return 2 if _NAV_STATE_KW_RE.search(line) else 1

    return max(clean, key=_score)[:max_chars]


def _entity_tokens_in_evidence(entity_text: str, evidence: EvidenceCheck) -> bool:
    """Check whether all proper-noun tokens from entity_text appear in evidence.

    Uses the untruncated all_output_tokens set — immune to the 2000-char window.
    Returns True (entity is in evidence) or False (entity is invented).
    Falls back to substring check on extracted_text if token set is empty.
    """
    if evidence.all_output_tokens:
        tokens = frozenset(
            m.lower() for m in _PROPER_TOKEN_RE.findall(entity_text)
        )
        # Empty token set = entity has no distinguishing proper nouns → skip check
        return not tokens or tokens.issubset(evidence.all_output_tokens)
    # Fallback for cases where token set wasn't populated
    return entity_text.strip() in evidence.extracted_text


def validate_failure_response(
    response: str,
    action_type: str,
    evidence: EvidenceCheck,
) -> tuple[bool, str]:
    """Validate that a failure-path response is honest and grounded.

    Nine checks in order. Any failure returns immediately with a reason code.

    Checks 1-3: explicit success claims, invented schedules, domain advice pivot.
    Check  4:   doctor + availability hallucination (token-based evidence check).
    Checks 5-6: implicit success claims, generic availability claims.
    Check  7:   specific time references in scheduling context.
    Checks 8-9: institution claim without evidence, invented confirmation IDs.

    Returns (valid: bool, reason: str).
    """
    if not response or not response.strip():
        return False, "empty_failure_response"

    # Check 1: No explicit success claims when execution failed
    if _FAILURE_SUCCESS_CLAIM_RE.search(response):
        return False, "claimed_success_on_failure_path"

    # Check 2: No invented schedules/hours
    if _INVENTED_SCHEDULE_RE.search(response):
        return False, "invented_schedule_on_failure_path"

    # Check 3: No domain-generalization pivot
    # (operational suggestions like "puedes intentar nuevamente" are NOT blocked)
    if _GENERAL_ADVICE_PIVOT_RE.search(response):
        return False, "general_advice_pivot_on_failure_path"

    # Check 4: Doctor + availability/time hallucination
    # Uses token-based evidence check (untruncated) — immune to the 2000-char window.
    doctor_match = _INVENTED_DOCTOR_RE.search(response)
    if doctor_match:
        has_availability = _INVENTED_AVAILABILITY_RE.search(response)
        has_time = _TIME_RE.search(response)
        if (has_availability or has_time) and not _entity_tokens_in_evidence(
            doctor_match.group(0), evidence
        ):
            return False, "invented_doctor_availability_on_failure_path"

    # Check 5: No implicit success claims
    # ("ya está listo", "se completó", "su solicitud fue procesada", etc.)
    if _IMPLICIT_SUCCESS_RE.search(response):
        _log_warn("failure_path.implicit_claim_detected", preview=response[:80])
        return False, "implicit_success_on_failure_path"

    # Check 6: No generic availability claims
    # ("hay horas disponibles", "hay disponibilidad", "puedes agendar para mañana")
    if _GENERIC_AVAILABILITY_RE.search(response):
        _log_warn("failure_path.generic_availability_detected", preview=response[:80])
        return False, "generic_availability_on_failure_path"

    # Check 7: No specific appointment time claims combined with scheduling context.
    # Standalone time refs ("el sitio falló a las 10am") are NOT blocked.
    if _TIME_SPECIFIC_RE.search(response):
        has_scheduling_ctx = (
            "agendar" in response.lower()
            or "cita" in response.lower()
            or "appointment" in response.lower()
            or "horario" in response.lower()
            or bool(_GENERIC_AVAILABILITY_RE.search(response))
        )
        if has_scheduling_ctx:
            return False, "time_specific_claim_on_failure_path"

    # Check 8: Named institution in success/availability context not found in evidence.
    # Defense-in-depth — fires when success context slipped past Checks 1-6.
    # Uses token-based evidence check (untruncated).
    inst_match = _NAMED_INSTITUTION_RE.search(response)
    if inst_match:
        has_success_ctx = bool(
            _GENERIC_AVAILABILITY_RE.search(response)
            or _IMPLICIT_SUCCESS_RE.search(response)
            or _FAILURE_SUCCESS_CLAIM_RE.search(response)
        )
        if has_success_ctx and not _entity_tokens_in_evidence(
            inst_match.group(0), evidence
        ):
            _log_warn(
                "failure_path.claim_without_evidence",
                entity=inst_match.group(0)[:60],
                preview=response[:80],
            )
            return False, "claim_without_evidence"

    # Check 9: Invented confirmation/booking ID with actual value.
    # These are ALWAYS hallucinations on a failed execution.
    if _INVENTED_CONFIRMATION_RE.search(response):
        return False, "invented_confirmation_on_failure_path"

    return True, "ok"


def _extract_partial_evidence(results: list) -> tuple[list[str], list[str]]:
    """Extract partial findings and failure reasons from execution results.

    Returns (partial_findings, failure_reasons).
    Partial findings come from:
      - Successful results with any non-trivial output
      - Failed results whose output contains page-state observations
        (login wall, redirect, error page — verified observations, not invented facts)

    Uses _select_best_partial() to filter HTML/JSON noise and prefer
    navigation/state keywords over raw page content.
    """
    partial_findings: list[str] = []
    failure_reasons: list[str] = []

    for r in results:
        if not hasattr(r, "success"):
            continue
        out = (r.output or "").strip()
        err = ((r.error or "") if hasattr(r, "error") else "").strip()

        if r.success and out and len(out) > 5:
            # Verified partial success — select best informative line
            partial_findings.append(_select_best_partial(out))
        elif not r.success:
            detail = err or out
            if detail and len(detail) > 5:
                failure_reasons.append(detail[:150])
            # Also capture output from a failed result that contains page-state info
            # (e.g. "Reached login wall at /agenda" — browser got there before failing)
            if out and out != detail and len(out) > 10:
                partial_findings.append(_select_best_partial(out))

    return partial_findings, failure_reasons


def build_failure_path_response(
    action_type: str,
    results: list,
    artifacts: dict,
    ref_id: str,
    evidence: EvidenceCheck,
) -> str:
    """Build a constrained, factual failure response from verified execution evidence.

    STRICT RULE: If partial evidence exists it MUST be included — never dropped.
    Evidence is clearly labelled as incomplete, never as success.
    Never invents domain facts, schedules, or availability.
    """
    partial_findings, failure_reasons = _extract_partial_evidence(results)

    ref_part = f" **{ref_id}**" if ref_id else ""

    base_messages: dict[str, str] = {
        "browser_package_check": f"No pude completar el rastreo del paquete{ref_part}.",
        "browser_form_workflow": f"No pude completar el proceso solicitado{ref_part}.",
        "browser_web_workflow":  f"No pude extraer la información solicitada{ref_part}.",
        "browser_navigation":    f"No pude completar la navegación al sitio{ref_part}.",
        "email_send":            f"No pude enviar el correo{ref_part}.",
    }
    base = base_messages.get(action_type, f"No se pudo completar la operación{ref_part}.")

    # STRICT: partial evidence MUST be included when it exists — never dropped
    if partial_findings:
        base += f"\n\nEvidencia parcial (incompleta):\n{partial_findings[0]}"
        _log_info(
            "failure_path.partial_evidence_forced",
            action_type=action_type,
            findings_count=len(partial_findings),
        )

    # Include verified failure reason extracted from execution output
    if failure_reasons:
        base += f"\n\nMotivo del error: {failure_reasons[0]}"
    else:
        type_hints: dict[str, str] = {
            "browser_package_check": (
                "El portal de rastreo no devolvió resultados o bloqueó el acceso."
            ),
            "browser_form_workflow": (
                "El sitio puede requerir autenticación o haber cambiado su estructura."
            ),
            "browser_web_workflow": (
                "El sitio puede tener protección anti-bot o requerir JavaScript."
            ),
            "browser_navigation": (
                "El sitio no respondió o bloqueó el acceso automatizado."
            ),
            "email_send": (
                "Verifica tu configuración de correo e inténtalo de nuevo."
            ),
        }
        hint = type_hints.get(action_type, "")
        if hint:
            base += f" {hint}"

    base += " ¿Quieres intentarlo de nuevo o con un método alternativo?"
    return base


def enforce_failure_path_contract(
    response: str,
    action_type: str,
    results: list,
    artifacts: dict,
    ref_id: str = "",
    user_text: str = "",
) -> ControlResult:
    """Enforce behavioral contract on failure-path responses.

    Gate condition: len(results) > 0 AND NOT terminal_success.
    Fires regardless of action_intent presence — results alone are sufficient signal.
    Never raises — degrades gracefully on internal errors.

    Logs: failure_path.response_rejected, failure_path.deterministic_fallback_used,
          failure_path.partial_evidence_forced, failure_path.implicit_claim_detected,
          failure_path.generic_availability_detected, failure_path.claim_without_evidence
    """
    if not response or not response.strip():
        ev = EvidenceCheck(False, False, "", set())
        fallback = build_failure_path_response(action_type, results, artifacts, ref_id, ev)
        return ControlResult(False, "empty_response", fallback, True)

    # Extract verifiable evidence from execution outputs (includes all_output_tokens)
    try:
        evidence = extract_evidence_summary(results, artifacts)
    except Exception as _e:
        _log_warn("failure_path.evidence_extract_failed", error=str(_e)[:60])
        evidence = EvidenceCheck(False, False, "", set())

    # Validate the failure response (9 checks)
    try:
        valid, reason = validate_failure_response(response, action_type, evidence)
    except Exception as _e:
        _log_warn(
            "failure_path.validation_exception_fail_closed",
            error=str(_e)[:80],
            action_type=action_type,
            response_preview=response[:60],
        )
        # Fail-CLOSED: a validation exception must never approve unvalidated content.
        # Returning the raw LLM response here would allow hallucinated output through.
        _safe_fb = (
            "No pude validar de forma segura la respuesta generada tras el fallo de ejecución. "
            "Para evitar entregar información incorrecta, prefiero no afirmar resultados no verificados."
        )
        return ControlResult(False, "validation_exception_fail_closed", _safe_fb, True)

    if valid:
        return ControlResult(True, "failure_path_valid", response)

    # Response failed validation — build deterministic constrained failure response
    _log_warn(
        "failure_path.response_rejected",
        reason=reason,
        action_type=action_type,
        response_length=len(response),
        preview=response[:120],
    )

    fallback = build_failure_path_response(action_type, results, artifacts, ref_id, evidence)

    _log_info(
        "failure_path.deterministic_fallback_used",
        action_type=action_type,
        reason=reason,
        fallback_length=len(fallback),
    )

    return ControlResult(False, reason, fallback, True)


# ── Priority 3: Success-Path Sufficiency Gate ──────────────────────────────────
# Fires ONLY when terminal_success=True (execution claimed to succeed).
# Validates that the response is genuinely grounded, not speculative or thin.
#
# Checks in order:
#   1. Execution sufficiency  — was enough actually extracted for this task type?
#   2. Weak language          — speculative/hedging phrases in a success response
#   3. Strong unsupported     — "el sistema indica..." without real evidence
#   4. Completeness overreach — "captura completa" without scroll evidence
#   5. Entity grounding       — named entities in response absent from evidence
#
# On failure → build_insufficient_execution_response() provides a grounded fallback.

# ── Weak / speculative language patterns ──────────────────────────────────────
_WEAK_LANGUAGE_RE = re.compile(
    r'\b(?:'
    r'parece\s+que|parece\s+ser|parece\s+(?:haber|tener|estar|mostrar)|al\s+parecer|'
    r'según\s+lo\s+que\s+se\s+ve|por\s+lo\s+que\s+se\s+puede\s+ver|'
    r'probablemente|posiblemente|'
    r'podría\s+(?:ser|haber|tener|tratarse|indicar|significar)|'
    r'quizás|quizá|tal\s+vez|puede\s+que|es\s+posible\s+que|'
    r'aparentemente|según\s+parece|'
    r'seems?\s+to\s+(?:be|have|show)|probably|possibly|'
    r'might\s+(?:be|have)|perhaps|it\s+appears?\s+(?:that|to)'
    r')\b',
    re.IGNORECASE,
)

# ── Strong claim markers — assertive phrasing about system/page output ─────────
_STRONG_CLAIM_RE = re.compile(
    r'\b(?:'
    r'el\s+sistema\s+(?:indica|muestra|confirma|dice|señala|reporta)|'
    r'la\s+(?:página|web)\s+(?:muestra|indica|confirma|dice|reporta)|'
    r'el\s+sitio\s+(?:indica|muestra|confirma|dice)|'
    r'the\s+(?:system|page|site|portal)\s+(?:shows?|indicates?|confirms?|reports?)'
    r')\b',
    re.IGNORECASE,
)

# ── Completeness claim patterns ────────────────────────────────────────────────
_COMPLETENESS_CLAIM_RE = re.compile(
    r'\b(?:'
    r'captura\s+completa|todo\s+el\s+sitio|página\s+completa|'
    r'full\s+(?:page|screenshot|capture)|complete\s+(?:page|capture|screenshot)|'
    r'entire\s+(?:page|site)'
    r')\b',
    re.IGNORECASE,
)

# ── Tracking / form evidence markers ──────────────────────────────────────────
_TRACKING_STATUS_RE = re.compile(
    r'\[TRACK_STATUS:|estado[:\s]|status[:\s]|en\s+tránsito|in\s+transit|'
    r'delivered|entregado|out\s+for\s+delivery|en\s+camino',
    re.IGNORECASE,
)
_FORM_CONFIRM_RE = re.compile(
    r'\[FORM_STATUS:\s*SUCCESS\]|confirmación|confirmation|'
    r'enviado\s+exitosamente|successfully\s+submitted|'
    r'número\s+de\s+(?:confirmación|orden|pedido)|order\s+number|'
    r'su\s+(?:solicitud|cita|reserva)\s+(?:fue|ha\s+sido)\s+(?:confirmada?|agendada?|registrada?)',
    re.IGNORECASE,
)

# Minimum meaningful content length for general browsing tasks.
_MIN_CONTENT_CHARS = 80


def _detect_weak_language(response: str) -> tuple[bool, str]:
    """Return (found, matched_phrase) if speculative/hedging language found."""
    m = _WEAK_LANGUAGE_RE.search(response)
    if m:
        return True, f"weak_speculative_language: '{m.group(0)[:50]}'"
    return False, "ok"


def _detect_unsupported_strong_claim(
    response: str, evidence: "EvidenceCheck"
) -> tuple[bool, str]:
    """Detect assertive claims about page/system output when evidence is thin."""
    if not _STRONG_CLAIM_RE.search(response):
        return False, "ok"
    content = evidence.extracted_text.strip()
    if len(content) < _MIN_CONTENT_CHARS:
        return True, "strong_claim_without_evidence"
    return False, "ok"


def _detect_unsupported_completeness_claim(
    response: str, evidence: "EvidenceCheck", artifacts: dict
) -> tuple[bool, str]:
    """Detect 'complete capture' claims when scroll evidence is absent."""
    if not _COMPLETENESS_CLAIM_RE.search(response):
        return False, "ok"
    shot_count = len(artifacts.get("screenshots", []))
    has_scroll = bool(re.search(r'\bscroll\b', evidence.extracted_text, re.IGNORECASE))
    if shot_count <= 1 and not has_scroll:
        return True, "completeness_claim_without_full_evidence"
    return False, "ok"


def has_sufficient_evidence(
    results: list, action_type: str, artifacts: dict | None = None
) -> tuple[bool, str]:
    """Check that execution produced enough real output for the task type.

    Per-type requirements:
      browser_package_check   — tracking status marker in output
      browser_form_workflow   — form confirmation marker in output
      browser_navigation      — screenshot artifact OR ≥80 chars of page content
      browser_web_workflow    — ≥80 chars of meaningful extracted content
      (default)               — any non-empty output
    """
    if artifacts is None:
        artifacts = {}
    combined = ""
    for r in results:
        if getattr(r, "skill_name", "") == "browser":
            combined += (getattr(r, "output", "") or "") + "\n"
    # Also include non-browser skill output for completeness
    for r in results:
        if getattr(r, "skill_name", "") != "browser":
            combined += (getattr(r, "output", "") or "") + "\n"

    if action_type == "browser_package_check":
        if not _TRACKING_STATUS_RE.search(combined):
            return False, "no_tracking_status_in_results"

    elif action_type == "browser_form_workflow":
        if not _FORM_CONFIRM_RE.search(combined):
            return False, "no_form_confirmation_in_results"

    elif action_type in ("browser_navigation", "browser_web_workflow"):
        has_screenshot = bool(artifacts.get("screenshots"))
        content_ok = len(combined.strip()) >= _MIN_CONTENT_CHARS
        if not has_screenshot and not content_ok:
            return False, "insufficient_content_extracted"

    else:
        # Generic: needs some output
        if not combined.strip():
            return False, "no_execution_output"

    return True, "ok"


def response_matches_evidence(
    response_text: str, evidence: "EvidenceCheck"
) -> tuple[bool, str]:
    """Check that named entities in response are grounded in execution evidence.

    Uses token-based membership (like failure-path gate) — immune to length truncation.
    Only flags when ALL proper-noun tokens in response are absent from evidence
    AND evidence has zero tokens (nothing was actually extracted).

    Conservative — avoids false positives on generic responses.
    """
    resp_tokens = frozenset(
        t.lower() for t in _PROPER_TOKEN_RE.findall(response_text) if len(t) >= 4
    )
    if not resp_tokens:
        return True, "ok"
    if not evidence.all_output_tokens:
        # No tokens extracted from ANY execution output — all claims are ungrounded
        if len(resp_tokens) >= 2:
            sample = ", ".join(sorted(resp_tokens)[:3])
            return False, f"named_entities_without_execution_evidence: {sample}"
        return True, "ok"
    # Evidence has some tokens — check overlap
    missing = resp_tokens - evidence.all_output_tokens
    present = resp_tokens & evidence.all_output_tokens
    # Only flag when ALL entities are missing (no overlap at all)
    if len(missing) >= 2 and not present:
        sample = ", ".join(sorted(missing)[:3])
        return False, f"response_entities_not_in_evidence: {sample}"
    return True, "ok"


def build_insufficient_execution_response(
    action_type: str,
    results: list,
    artifacts: dict,
    user_text: str,
    evidence: "EvidenceCheck",
) -> str:
    """Build a grounded, honest response when execution was insufficient.

    Always includes:
    - What was attempted
    - What was missing
    - Any partial findings from evidence
    - Actionable suggestion (not generic "try again")
    """
    screenshots = artifacts.get("screenshots", [])
    shot_part = ""
    if screenshots:
        lines = "\n".join(f"![screenshot]({p})" for p in screenshots[:2])
        shot_part = f"\n\n{lines}"

    # Extract best available partial content
    partial = ""
    for r in results:
        out = (getattr(r, "output", "") or "").strip()
        if out and len(out) >= 30:
            # Filter HTML noise — take first substantive lines
            lines = [l.strip() for l in out.splitlines()
                     if l.strip() and not l.strip().startswith("<")
                     and len(l.strip()) > 10]
            if lines:
                partial = "\n".join(lines[:4])[:300]
                break

    partial_part = f"\n\nContenido parcial obtenido:\n{partial}" if partial else ""

    type_messages: dict[str, str] = {
        "browser_package_check": (
            "No pude obtener el estado de seguimiento completo. "
            "El sitio cargó pero no devolvió datos de rastreo verificables."
            f"{partial_part}"
            "\n\nSugerencia: verifica que el código de seguimiento sea correcto "
            "e intenta directamente en el sitio del transportista."
        ),
        "browser_form_workflow": (
            "La navegación se completó pero no se recibió confirmación del formulario. "
            "Es posible que la solicitud no haya sido enviada."
            f"{partial_part}{shot_part}"
            "\n\nSugerencia: verifica en el sitio si la solicitud aparece registrada."
        ),
        "browser_navigation": (
            "Navegué al sitio pero no pude extraer contenido suficiente para responder. "
            f"{partial_part}{shot_part}"
            "\n\nSugerencia: intenta con una URL más específica "
            "o solicita una sección concreta de la página."
        ),
        "browser_web_workflow": (
            "Ejecuté la tarea pero los resultados obtenidos son insuficientes "
            "para dar una respuesta completa."
            f"{partial_part}{shot_part}"
            "\n\nSugerencia: especifica qué información exacta necesitas "
            "o prueba con una fuente alternativa."
        ),
    }

    return type_messages.get(
        action_type,
        f"La ejecución se completó parcialmente pero no se obtuvieron datos suficientes."
        f"{partial_part}{shot_part}"
    )


# ── Phase 1: ObjectiveSpec evidence validator ─────────────────────────────────
def validate_against_spec(
    response_text: str,
    results: list,
    spec: "object | None",
) -> "tuple[bool, str]":
    """Validate execution output against an ObjectiveSpec's required_evidence.

    Returns (passed: bool, reason: str).
    passed=True  → all required_evidence items satisfied (or spec absent/empty)
    passed=False → at least one required_evidence item not found in corpus

    Evidence corpus = response_text + all SkillResult / dict outputs from results.
    Each required_evidence item is an OR-group pattern: "tokenA|tokenB".
    ALL items must match (AND across items, OR within each item).

    Fail-open for malformed regex patterns (skip that item rather than block).
    Backward compat: returns True when spec is None or has no required_evidence.
    """
    if spec is None:
        return True, ""

    evidence_list = getattr(spec, "required_evidence", None) or []
    if not evidence_list:
        return True, ""

    # Build corpus: response + all result outputs
    corpus = response_text or ""
    for r in (results or []):
        if isinstance(r, dict):
            corpus += "\n" + (r.get("output") or "")
        else:
            corpus += "\n" + (getattr(r, "output", None) or "")

    missing = []
    for item in evidence_list:
        try:
            if not re.search(item, corpus, re.IGNORECASE):
                missing.append(item[:60])
        except re.error:
            # Invalid regex pattern — skip (fail-open: don't block on bad patterns)
            continue

    if missing:
        return False, f"required_evidence not satisfied: {missing}"
    return True, ""


def enforce_success_path_contract(
    response: str,
    action_type: str,
    results: list,
    artifacts: dict,
    user_text: str = "",
    ref_id: str = "",
    objective_spec: "object | None" = None,
) -> ControlResult:
    """Priority 3: Success-path sufficiency and grounding gate.

    Fires ONLY when terminal_success=True (caller's responsibility).
    Does NOT modify failure-path (Priority 1) or domain lock (Priority 2) behavior.

    Checks in order (first failure wins):
      1. Execution sufficiency  — was enough actually extracted?
      2. Weak speculative lang  — parece, probablemente, etc.
      3. Strong unsupported     — el sistema indica... without real content
      4. Completeness overreach — captura completa without scroll
      5. Entity grounding       — named entities absent from all evidence

    Returns ControlResult. On failure, builds a grounded fallback response.
    Never raises.
    """
    if not response or not response.strip():
        return ControlResult(True, "empty_response_skip", response or "")

    # Gather evidence (reuse existing extractor)
    try:
        evidence = extract_evidence_summary(results, artifacts)
    except Exception as _e:
        _log_warn("success_path.evidence_extract_failed", error=str(_e)[:60])
        evidence = EvidenceCheck(False, False, "", set())

    _log_info(
        "success_path.gate_entered",
        action_type=action_type or "(none)",
        result_count=len(results),
        has_output=evidence.has_real_output,
        has_screenshot=evidence.screenshot_present,
    )

    # ── Check 0: ObjectiveSpec required_evidence gate (Phase 1) ──────────────
    # Runs before existing checks. If spec is absent or has no required_evidence,
    # this is a no-op (backward compatible). Fail-closed only when evidence tokens
    # are explicitly declared and none of them appear in the execution corpus.
    if objective_spec is not None:
        _spec_ok, _spec_reason = validate_against_spec(response, results, objective_spec)
        if not _spec_ok:
            _log_warn(
                "success_path.rejected",
                reason="objective_spec_evidence_missing",
                detail=_spec_reason,
                action_type=action_type or "(none)",
                objective=getattr(objective_spec, "objective", "")[:80],
            )
            fallback = build_insufficient_execution_response(
                action_type, results, artifacts, user_text, evidence
            )
            _log_info("success_path.fallback_used", reason=_spec_reason[:80])
            return ControlResult(False, "objective_spec_evidence_missing", fallback, True)

    # ── Check 1: Execution sufficiency ────────────────────────────────────────
    sufficient, suff_reason = has_sufficient_evidence(results, action_type, artifacts)
    if not sufficient:
        _log_warn(
            "success_path.rejected",
            reason="insufficient_data_for_intent",
            detail=suff_reason,
            action_type=action_type or "(none)",
        )
        fallback = build_insufficient_execution_response(
            action_type, results, artifacts, user_text, evidence
        )
        _log_info("success_path.fallback_used", reason=suff_reason)
        return ControlResult(False, "insufficient_data_for_intent", fallback, True)

    # ── Check 2: Weak speculative language ────────────────────────────────────
    weak, weak_reason = _detect_weak_language(response)
    if weak:
        _log_warn(
            "success_path.rejected",
            reason="weak_speculative_response",
            detail=weak_reason,
            action_type=action_type or "(none)",
        )
        fallback = build_insufficient_execution_response(
            action_type, results, artifacts, user_text, evidence
        )
        _log_info("success_path.fallback_used", reason=weak_reason)
        return ControlResult(False, "weak_speculative_response", fallback, True)

    # ── Check 3: Strong claim without evidence ────────────────────────────────
    strong, strong_reason = _detect_unsupported_strong_claim(response, evidence)
    if strong:
        _log_warn(
            "success_path.rejected",
            reason="strong_claim_without_evidence",
            detail=strong_reason,
            action_type=action_type or "(none)",
        )
        fallback = build_insufficient_execution_response(
            action_type, results, artifacts, user_text, evidence
        )
        _log_info("success_path.fallback_used", reason=strong_reason)
        return ControlResult(False, "strong_claim_without_evidence", fallback, True)

    # ── Check 4: Completeness claim without full evidence ─────────────────────
    overreach, over_reason = _detect_unsupported_completeness_claim(response, evidence, artifacts)
    if overreach:
        _log_warn(
            "success_path.rejected",
            reason="completeness_claim_without_full_evidence",
            detail=over_reason,
            action_type=action_type or "(none)",
        )
        fallback = build_insufficient_execution_response(
            action_type, results, artifacts, user_text, evidence
        )
        _log_info("success_path.fallback_used", reason=over_reason)
        return ControlResult(False, "completeness_claim_without_full_evidence", fallback, True)

    # ── Check 5: Response entities not in evidence ────────────────────────────
    aligned, align_reason = response_matches_evidence(response, evidence)
    if not aligned:
        _log_warn(
            "success_path.rejected",
            reason="response_entities_not_in_evidence",
            detail=align_reason,
            action_type=action_type or "(none)",
        )
        fallback = build_insufficient_execution_response(
            action_type, results, artifacts, user_text, evidence
        )
        _log_info("success_path.fallback_used", reason=align_reason)
        return ControlResult(False, "response_entities_not_in_evidence", fallback, True)

    _log_info(
        "success_path.gate_entered",
        action_type=action_type or "(none)",
        result="approved",
    )
    return ControlResult(True, "success_path_valid", response)


# ── C3 Fix: Exhaustion-Path Guard ─────────────────────────────────────────────
# Fires when the execution loop exits WITHOUT terminal state detection.
# This covers round exhaustion and wall-clock timeouts where the LLM was never
# able to confirm task completion.
#
# Phase 8 (and Priority 3) require action_terminal_detected=True — they never
# fire on the exhaustion path. Priority 1 covers the case where results exist,
# but cannot fire when action_all_results is empty (e.g. all rounds pre-blocked).
#
# This guard fills the gap: ensures no hallucinated completion claims reach
# the user regardless of whether results were collected.
#
# Checks:
#   1. Verified-outcome assertions ("pude obtener", "según la página", etc.)
#   2. Task-completion assertions ("completado exitosamente", "task completed", etc.)
#
# On failure → deterministic honest-exhaustion fallback. Fail-closed on exception.

_EXHAUSTION_VERIFIED_CLAIM_RE = re.compile(
    r"(?:"
    # "I managed to obtain / verify / confirm / access / complete" — negated forms excluded.
    # (?<!no\s) is a fixed-width lookbehind (3 chars); with IGNORECASE covers No/NO/no.
    r"(?<!no\s)(?:pude|logré|conseguí|he)\s+(?:completar|verificar|obtener|confirmar|acceder\s+(?:a|al?)|encontrar)"
    # "According to the page / site / data obtained"
    r"|(?:según\s+(?:la\s+(?:página|web|sitio)|los\s+datos?\s+(?:obtenidos?|del\s+sitio)))"
    # "The page / site shows / indicates / confirms"
    r"|(?:la\s+(?:página|web)\s+(?:muestra|indica|dice|confirma|mostró|indicó))"
    r"|(?:el\s+sitio\s+(?:muestra|indica|dice|confirma|mostró|indicó))"
    # Explicit success completion phrases
    r"|(?:(?:la\s+)?(?:operación|tarea|solicitud|transacción|proceso|formulario)\s+"
    r"(?:fue|se\s+ha|quedó)\s+(?:completad|enviado|procesado|realizad|registrad|exitoso))"
    r"|(?:task\s+completed|operación\s+completada|operación\s+exitosa|exitosamente)"
    # "The status is X" / "The price is $X"
    r"|(?:el\s+estado\s+(?:es|fue|está)\s+\w)"
    r"|(?:el\s+precio\s+(?:es|de|fue|está)\s+[\$\d])"
    r")",
    re.IGNORECASE,
)


def enforce_exhaustion_path_contract(
    response: str,
    action_type: str,
    results: list,
    user_text: str = "",
) -> "ControlResult":
    """Guard for exhaustion / non-terminal loop exits.

    Fires when execution was attempted but terminal state was NEVER detected
    (round exhaustion, wall-clock timeout, all rounds pre-blocked).

    Blocks hallucinated completion claims. Fail-closed on exception.
    """
    if not response or not response.strip():
        _fallback = (
            "No pude completar la tarea solicitada. "
            "Se agotaron los intentos de ejecución sin obtener confirmación del resultado."
        )
        _log_info("exhaustion_path.empty_response_replaced", action_type=action_type or "(none)")
        return ControlResult(False, "exhaustion_empty_response", _fallback, True)

    try:
        # Check for hallucinated verified-outcome or completion claims
        _match = _EXHAUSTION_VERIFIED_CLAIM_RE.search(response)
        if _match:
            _log_warn(
                "exhaustion_path.completion_claim_blocked",
                action_type=action_type or "(none)",
                match=_match.group(0)[:60],
                response_preview=response[:100],
            )
            # Build grounded fallback from any partial evidence available
            _ev = EvidenceCheck(False, False, "", frozenset())
            try:
                _ev = extract_evidence_summary(results, {})
            except Exception:
                pass
            _fallback = build_failure_path_response(action_type, results, {}, "", _ev)
            return ControlResult(False, "exhaustion_completion_claim", _fallback, True)

        # Response is appropriately non-assertive — allow through
        _log_info(
            "exhaustion_path.response_accepted",
            action_type=action_type or "(none)",
            response_length=len(response),
        )
        return ControlResult(True, "exhaustion_path_accepted", response)

    except Exception as _e:
        _log_warn(
            "exhaustion_path.validation_exception_fail_closed",
            error=str(_e)[:80],
            action_type=action_type or "(none)",
        )
        _fallback = (
            "No pude completar la tarea solicitada. "
            "Se agotaron los intentos de ejecución sin confirmación del resultado."
        )
        return ControlResult(False, "exhaustion_exception_fail_closed", _fallback, True)
