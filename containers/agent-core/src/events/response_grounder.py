"""Response Grounding Layer — Phase 7.

Validates that LLM responses are anchored to actual execution outputs.
Provides a deterministic response builder that constructs user-facing
responses from verified execution data without LLM improvisation.

Architecture:
  validate_grounding()         → check if response references actual results
  build_deterministic_response() → construct grounded response from raw data
  extract_result_facts()       → pull key facts from skill output lists

Called from _run_llm_loop post-loop, after the drift gate, before return.
Never raises — grounding failures log and fall back gracefully.

Check 5 — Weak-evidence rejection (strict grounding):
  Every success response must carry at least ONE of:
    A. A structured result marker  ([TRACK_STATUS:], [FORM_STATUS:], …)
    B. Numeric or date evidence    (prices, ISO dates, large numbers, IDs)
    C. Multi-token phrase          (≥4 whitespace-split words)
  Responses that satisfy none of the above are rejected regardless of fragment
  count.  Prevents bare single-word responses ("done", "example.com", "ok")
  and two-word fragments ("click here") from being accepted as grounded.

Check 6 — Generic weak-phrase filter:
  Responses that pass Check 5 purely on word count but contain ONLY boilerplate
  ("completed successfully", "task completed", …) are rejected unless they also
  carry numeric evidence, a structured marker, or a domain-specific keyword.
  Prevents "The process completed successfully" from being accepted as grounded.

Check 7 — Plain status-marker value validation:
  When a plain key:value marker is present (status:, result:, estado:, resultado:)
  the value after the colon is validated against _VALID_STATUS_VALUES.
  If the value is not whitelisted AND no numeric/date evidence is present → reject.
  Bracket-form markers ([TRACK_STATUS:]) are written by skills and bypass this check.
  Prevents "status: done" / "result: ok" from being accepted as grounded.

Check 8 — Intent evidence gate:
  When the user's original intent requires real-world data (price queries, tracking,
  status checks, balance lookups, weather, etc.) the skill results MUST contain
  extractable evidence (numeric value, URL, structured marker, or substantial output).
  If no evidence is found → reject (reason="evidence_missing").
  Pure explanatory / creative / greeting intents are exempt.

Check 9 — Anti-hallucination guard:
  When the response contains specific factual claims (prices, dates, delivery status,
  percentages, crypto amounts) AND the skill results contain no verifiable evidence,
  the response is rejected (reason="hallucination_no_evidence").
  Prevents the LLM from inventing facts when real data is unavailable.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)

# ── Fallback message ──────────────────────────────────────────────────────────
# Returned by build_deterministic_response() when there are no verifiable
# execution outputs and/or grounding cannot be established.
GROUNDING_INSUFFICIENT_EVIDENCE_MSG = (
    "I could not verify the result with sufficient evidence."
)

# ── Off-topic patterns (supplement to _DRIFT_DURING_ACTION_RE) ────────────────
# These catch responses that IGNORE execution results and pivot to unrelated topics.
_OFF_TOPIC_RE = re.compile(
    r"(?:"
    # Weather drift
    r"the\s+(?:current\s+)?weather\s+in\b"
    r"|(?:London|Paris|Berlin|Tokyo|Sydney|Chicago|Miami).{0,40}(?:weather|temperature|°)"
    # "here is some information about..." (generic LLM framing)
    r"|(?:here\s+is|here's)\s+(?:a\s+)?(?:brief\s+)?(?:summary\s+of\s+)?(?:some|the)\s+"
    r"(?:information|info|overview)\s+(?:about|on)\s+(?!your\s+package|the\s+tracking|the\s+result)"
    # "en general / generally speaking" — LLM pivoting to general knowledge
    r"|(?:en\s+general[,.]|generally\s+speaking[,.]|in\s+general[,.])"
    # "según estudios / according to studies/experts" — LLM citing fake research
    r"|(?:según\s+(?:estudios|expertos|investigaciones)|according\s+to\s+(?:studies|experts?|research))"
    r")",
    re.IGNORECASE,
)

# Marker tokens that must appear in response when execution succeeded
_TRACKING_RESULT_MARKERS = re.compile(
    r"(?:tránsito|in transit|entregado|delivered|paquete|package|"
    r"envío|shipment|seguimiento|tracking|estado|status|"
    r"rastreo|courier|expedición|ubicación|location)",
    re.IGNORECASE,
)

_SCREENSHOT_RESULT_MARKERS = re.compile(
    r"(?:captura|screenshot|imagen|image|pantalla|screen|adjunt|attach|"
    r"aquí\s+(?:tienes|está)|here\s+(?:is|are)|captured|tomé|tomamos)",
    re.IGNORECASE,
)

_SEARCH_RESULT_MARKERS = re.compile(
    r"(?:encontré|encontramos|found|results?|resultados?|información|information|"
    r"según|according|based\s+on|de\s+acuerdo|aquí\s+hay|here\s+(?:is|are))",
    re.IGNORECASE,
)

_FAILURE_MARKERS = re.compile(
    r"(?:no\s+pude|couldn'?t|could\s+not|failed|falló|falla|error|"
    r"problema|problem|imposible|unable|no\s+fue\s+posible|no\s+logré)",
    re.IGNORECASE,
)

# ── Check 5: Single-fragment hallucination prevention ─────────────────────────

# URL and bare-domain fragments in a response — things that look like links.
_FRAGMENT_RE = re.compile(
    r"https?://\S+"                                                   # full URL
    r"|www\.\S+"                                                      # www.domain
    r"|\b[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?"                      # bare domain
    r"\.(?:com|net|org|io|es|mx|co|info|biz|gov|edu|app|dev)\b",
    re.IGNORECASE,
)

# Structured result markers — bracket-enclosed skill labels OR plain key:value labels.
# Bracket form:  [TRACK_STATUS: delivered]
# Plain form:    Status: Delivered   /   Result: OK
_STRUCTURED_MARKER_RE = re.compile(
    r"\[(?:TRACK_STATUS|FORM_STATUS|ORDER_STATUS|RESULT|STATUS"
    r"|SUCCESS|FOUND|PARTIAL|FAILED|NOT_FOUND|DATA|INFO)\s*:"
    r"|\b(?:status|result|estado|resultado|tracking|entregado|delivered"
    r"|confirmed|confirmado|ubicación|location)\s*:\s*\S",
    re.IGNORECASE,
)

# Numeric and date evidence — prices, ISO dates, large numbers, tracking IDs.
_NUMERIC_DATE_EVIDENCE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"                   # ISO date  2024-01-15
    r"|\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\b"  # date      12/31/2024
    r"|\$\s*\d|\€\s*\d|\£\s*\d"                 # currency  $42
    r"|\b\d{1,3}(?:[,.]\d{3})+\b"               # thousands 1,234 or 1.234
    r"|\b[A-Z]{1,4}\d{6,}[A-Z]{0,3}\b"           # tracking  LY123456789CN
    r"|\b\d{2}:\d{2}(?::\d{2})?\b"              # time      14:32
    # Plain price values returned by APIs (no comma formatting): 43210.50 USD
    r"|\b\d{3,}(?:\.\d+)?\s*(?:USD|EUR|GBP|BTC|ETH|USDT|SOL|BNB|ADA|XRP|DOGE|MXN|ARS|CLP|COP)\b",
    re.IGNORECASE,
)

# ── Check 6: Generic weak-phrase filter ──────────────────────────────────────

# Boilerplate completions that carry no actual result content.
# English + Spanish equivalents — used by both Check 6 and the Check 7 refinement.
_GENERIC_WEAK_PHRASES: tuple[str, ...] = (
    # English
    "completed successfully",
    "process completed",
    "task completed",
    "operation successful",
    "done successfully",
    "successfully completed",
    "finished successfully",
    # Spanish equivalents
    "completado exitosamente",
    "proceso completado",
    "tarea completada",
    "operación exitosa",
    "operacion exitosa",
    "terminado exitosamente",
    "exitosamente completado",
)

# Domain-specific keywords that redeem an otherwise generic phrase.
# If the response mentions payment, a package, a form, etc., the phrase is
# anchored to a real task even if it reads like boilerplate.
_DOMAIN_KEYWORD_RE = re.compile(
    r"(?:tracking|shipment|package|paquete|order|pedido|"
    r"payment|pago|invoice|factura|amount|monto|precio|price|"
    r"form|formulario|email|correo|message|mensaje|"
    r"search|búsqueda|result|resultado|screenshot|captura|"
    r"file|archivo|download|upload|"
    r"login|autenticación|booking|reserva|confirmation|confirmación|"
    r"delivered|entregado|shipped|enviado|transit|tránsito)",
    re.IGNORECASE,
)

# ── Check 7: Plain status-marker value validation ─────────────────────────────

# Whitelist of recognised status values (English + Spanish equivalents).
# Checked as substrings so "completed at 2025-03-10" matches "completed".
_VALID_STATUS_VALUES: tuple[str, ...] = (
    # English
    "delivered", "shipped", "in transit", "confirmed",
    "completed", "success", "failed", "cancelled",
    # Spanish equivalents
    "entregado", "enviado", "en tránsito", "en transito",
    "confirmado", "completado", "exitoso", "fallido", "cancelado",
)

# Captures the value portion of a plain key:value status marker.
# Stops at sentence-ending punctuation or newline to avoid greedy over-capture.
_PLAIN_STATUS_RE = re.compile(
    r"\b(?:status|result|estado|resultado)\s*:\s*([^.!?\n]{1,80})",
    re.IGNORECASE,
)

_PUNCT_COLLAPSE_RE = re.compile(r"[^\w\s]")
_WS_COLLAPSE_RE    = re.compile(r"\s+")


def _normalize_for_phrase(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for phrase matching only."""
    t = _PUNCT_COLLAPSE_RE.sub(" ", text.lower())
    return _WS_COLLAPSE_RE.sub(" ", t).strip()


# Stop words filtered out before counting content words in the phrase check.
_PHRASE_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could", "this", "that",
    "these", "those", "here", "there", "it", "its", "i", "you", "he",
    "she", "we", "they", "at", "in", "on", "to", "for", "of", "with",
    "by", "from", "as", "not", "or", "and", "but", "if", "so", "yet",
    "nor", "your", "my", "their", "his", "her", "our", "also", "up",
    "out", "no", "just", "been", "than", "then", "now", "how",
})


def _count_fragments(response: str) -> int:
    """Count URL / domain fragments in a response string."""
    return len(_FRAGMENT_RE.findall(response))


def _has_sufficient_single_fragment_evidence(response: str) -> bool:
    """Return True if a single-fragment response carries enough supporting evidence.

    A single URL or domain alone ("Here is the result: example.com") is not
    sufficient.  The response must also satisfy at least one of:

      A. Structured marker  — [TRACK_STATUS: …], [FORM_STATUS: …], etc.
      B. Numeric/date data  — price, ISO date, time, large number, or tracking ID
      C. Multi-token phrase — >3 content words remain after stripping URL fragments
                              (stop words excluded, words must be ≥3 chars)
    """
    # A. Structured result marker
    if _STRUCTURED_MARKER_RE.search(response):
        return True

    # B. Numeric or date evidence
    if _NUMERIC_DATE_EVIDENCE_RE.search(response):
        return True

    # C. Multi-token content phrase: strip URL fragments, count non-stop content words.
    # Threshold: ≥3 content words after filtering — equivalent to ">3 total words"
    # when typical stop words are present (e.g. "was", "the", "at").
    clean = _FRAGMENT_RE.sub(" ", response)
    words = re.findall(r"\b\w+\b", clean)
    content_words = [
        w for w in words
        if w.lower() not in _PHRASE_STOP_WORDS and len(w) > 2
    ]
    return len(content_words) >= 3


# ── Check 8+9: Intent Evidence Gate + Anti-Hallucination ─────────────────────

# ── _requires_evidence: semantic group tables ─────────────────────────────────
# Substring-matched against normalized (lowercase, no-punct) intent text.
# Groups are frozensets of phrases; matched via `phrase in normalized_text`.

_STATUS_QUERIES: frozenset[str] = frozenset({
    # English
    "where is", "how is", "did it arrive", "has it arrived", "where's my",
    "when will it", "is it there", "has been delivered",
    "status", "tracking", "shipment", "delivery status", "package status",
    # Spanish
    "donde esta", "como va", "llego", "entregado", "ha llegado",
    "estado del", "rastreo", "seguimiento", "paquete",
})

_PRICE_QUERIES: frozenset[str] = frozenset({
    # English
    "price", "how much", "cost", "worth", "fee", "rate",
    "exchange rate", "balance",
    # Spanish
    "precio", "cuanto cuesta", "cuánto cuesta", "valor", "cotizacion",
    "cotización", "costo", "coste", "tarifa", "tasa", "saldo",
})

# Specific tradable assets — price queries involving these always need live data.
_PRICE_ASSETS: frozenset[str] = frozenset({
    "btc", "eth", "bitcoin", "ethereum", "crypto", "cripto",
    "usdt", "sol", "xrp", "bnb", "ada", "doge",
    "usd", "eur", "gbp", "dollar", "euro", "dolar",
})

_TIME_SENSITIVE: frozenset[str] = frozenset({
    # English
    "now", "currently", "current", "latest", "today", "right now",
    "at the moment", "live", "real time", "realtime",
    # Spanish
    "ahora", "actualmente", "actual", "ultimo", "último", "hoy",
    "en este momento", "en vivo",
})

_VERIFICATION: frozenset[str] = frozenset({
    # English
    "confirm", "verify", "check if", "is it true", "make sure",
    "validate", "is there",
    # Spanish
    "verifica", "verificar", "confirmar", "revisa si", "es cierto",
    "asegurar", "comprueba",
})

# Normalization helper — shared with _normalize_for_phrase but independent
_INTENT_PUNCT_RE = re.compile(r"[^\w\s]")
_INTENT_WS_RE    = re.compile(r"\s+")


def _normalize_intent(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace (for intent matching)."""
    t = _INTENT_PUNCT_RE.sub(" ", text.lower())
    return _INTENT_WS_RE.sub(" ", t).strip()


# Patterns that EXEMPT the request from evidence requirements.
# Checked first — greedy: if ANY evidence-free signal is found, no evidence needed.
_EVIDENCE_FREE_RE = re.compile(
    r"(?:"
    r"^(?:hi|hello|hola|hey|buenas|buenos días|buenas tardes|good morning)"
    r"|\bexplain\b|\bexplica\b|\bhow\s+does\b|\bcómo\s+funciona\b"   # explanations
    r"|\bwhat\s+is\s+(?:a\s+|an\s+|the\s+)?[a-z]+\??$"              # "what is X?" concept
    r"|\b(?:thanks|gracias|thank\s+you|de\s+nada)\b"                  # thanks
    r"|\b(?:create|write|generat|escrib|redacta|genera)\b"            # creative
    r"|\b(?:calculate|calcula|convert|convierte)\b"                   # local math/conversion
    r"|\b(?:help|ayuda|how\s+to|cómo\s+puedo)\b"                     # help requests
    r")",
    re.IGNORECASE,
)

# Patterns that indicate specific factual claims in the RESPONSE.
# Presence + no evidence → hallucination risk.
_FACTUAL_CLAIM_RE = re.compile(
    r"(?:"
    r"\$\s*[\d,]+(?:\.\d+)?"                                          # $1,234.56
    r"|€\s*[\d,]+(?:\.\d+)?|£\s*[\d,]+(?:\.\d+)?"                   # €/£ amounts
    r"|\b[\d,]+(?:\.\d+)?\s*(?:USD|EUR|BTC|ETH|USDT)\b"             # crypto/fiat
    r"|\b\d{4}-\d{2}-\d{2}\b"                                        # ISO date
    r"|\b(?:delivered|shipped|entregado|enviado|in\s+transit|en\s+tránsito)\b"
    r"|\b\d+(?:\.\d+)?%\b"                                           # percentages
    r"|\b(?:confirmed|approved|rejected|completed|approved)\b"        # action claims
    r")",
    re.IGNORECASE,
)

# Fallback used when evidence_missing or hallucination blocks the response.
EVIDENCE_MISSING_MSG = (
    "I couldn't verify that information yet. Let me check that for you."
)


@dataclass
class EvidenceState:
    """Tracks verifiable evidence extracted from skill execution results.

    Sufficiency is determined by evidence TYPE, not confidence score.
    Confidence is kept for logging only.

    Hierarchy:
      marker   → trusted structured result  (always sufficient)
      numeric  → price / date / ID          (always sufficient)
      url      → visited URL present        (sufficient when paired with any text)
      weak_text → long output only          (NEVER sufficient alone)
    """
    has_evidence: bool = False
    sources: List[str] = field(default_factory=list)
    confidence: float = 0.0    # 0.0=none  0.3=weak  0.6=url  0.9=numeric  1.0=marker

    # Typed evidence flags
    has_marker: bool = False
    has_numeric: bool = False
    has_url: bool = False
    has_weak_text: bool = False

    def is_sufficient(self) -> bool:
        """Rule-based sufficiency — weak_text alone is never enough."""
        if self.has_marker:
            return True
        if self.has_numeric:
            return True
        if self.has_url and (self.has_weak_text or self.has_numeric or self.has_marker):
            return True
        return False


def _requires_evidence(user_text: str) -> bool:
    """Return True when the intent requires verifiable external data to answer.

    Priority:
      1. Evidence-free signals (greetings, explanations, creative) → False
      2A. STATUS_QUERIES match → True
      2B. PRICE_QUERIES + (known asset OR TIME_SENSITIVE) → True
      2C. VERIFICATION match → True
      3. Fallback → False
    """
    if not user_text:
        return False
    # Priority 1: evidence-free first
    if _EVIDENCE_FREE_RE.search(user_text.strip()):
        return False
    # Normalize for group matching
    norm = _normalize_intent(user_text)
    # 2A: Status queries
    status_match = next((p for p in _STATUS_QUERIES if p in norm), None)
    if status_match:
        logger.info(
            "intent_evidence_detected matched_group=%r phrase=%r text=%r",
            "status", status_match, user_text[:60],
        )
        return True
    # 2B: Price queries (asset-specific or time-qualified)
    price_match = next((p for p in _PRICE_QUERIES if p in norm), None)
    if price_match:
        asset_match = next((a for a in _PRICE_ASSETS if a in norm), None)
        if asset_match:
            logger.info(
                "intent_evidence_detected matched_group=%r phrase=%r text=%r",
                "asset_price", price_match, user_text[:60],
            )
            return True
        time_match = next((t for t in _TIME_SENSITIVE if t in norm), None)
        if time_match:
            logger.info(
                "intent_evidence_detected matched_group=%r phrase=%r text=%r",
                "price_time", price_match, user_text[:60],
            )
            return True
    # 2C: Verification
    verify_match = next((p for p in _VERIFICATION if p in norm), None)
    if verify_match:
        logger.info(
            "intent_evidence_detected matched_group=%r phrase=%r text=%r",
            "verification", verify_match, user_text[:60],
        )
        return True
    return False


def extract_evidence_from_results(results: list) -> "EvidenceState":
    """Scan skill execution results for verifiable evidence.

    Confidence levels (logging only — sufficiency is rule-based via is_sufficient()):
      1.0 — structured result marker    ([TRACK_STATUS:], etc.)
      0.9 — numeric / date evidence     (prices, dates, tracking IDs)
      0.6 — URL reference               (a source URL was visited)
      0.3 — substantial skill output    (≥50 chars) — WEAK, never sufficient alone
    """
    sources: List[str] = []
    max_conf = 0.0
    has_marker = False
    has_numeric = False
    has_url = False
    has_weak_text = False

    for result in results:
        if not getattr(result, "success", True):
            continue                          # ignore failed skill calls
        output = (getattr(result, "output", "") or "").strip()
        if not output:
            continue

        if _STRUCTURED_MARKER_RE.search(output):
            sources.append("structured_marker")
            has_marker = True
            max_conf = max(max_conf, 1.0)

        m = _NUMERIC_DATE_EVIDENCE_RE.search(output)
        if m:
            sources.append(f"numeric:{m.group(0)[:20]}")
            has_numeric = True
            max_conf = max(max_conf, 0.9)

        m = _FRAGMENT_RE.search(output)
        if m:
            sources.append(f"url:{m.group(0)[:40]}")
            has_url = True
            max_conf = max(max_conf, 0.6)

        if len(output) >= 50 and not has_marker and not has_numeric:
            # Weak evidence: long text without a structured/numeric signal.
            # Tracked independently of URL — url+text combos use both flags.
            sources.append("weak_text")
            has_weak_text = True
            max_conf = max(max_conf, 0.3)
            if not has_url:
                # Only log when there's no URL (pure text noise is most suspicious)
                logger.info(
                    "weak_evidence_detected source=%r text_length=%d sample=%r",
                    "skill_output", len(output), output[:60],
                )

    return EvidenceState(
        has_evidence=len(sources) > 0,
        sources=sources[:6],
        confidence=max_conf,
        has_marker=has_marker,
        has_numeric=has_numeric,
        has_url=has_url,
        has_weak_text=has_weak_text,
    )


@dataclass
class GroundingResult:
    is_grounded: bool
    reason: str


def validate_grounding(
    response: str,
    action_type: str,
    results: list,
    domain: str,
    terminal_success: bool,
    artifacts: dict,
    user_text: str = "",
) -> GroundingResult:
    """Validate that a response is anchored to actual execution outputs.

    Returns GroundingResult with is_grounded=True when the response
    correctly references execution data.

    Checks:
    1. Off-topic content not injected
    2. For success: response references domain/result type keywords
    3. For failure: response acknowledges the failure
    4. For screenshots: response mentions artifact attachment
    """
    if not response or not response.strip():
        return GroundingResult(False, "empty_response")

    # Check 1: Off-topic injection
    if _OFF_TOPIC_RE.search(response):
        return GroundingResult(False, "off_topic_injection")

    # Check 2: Action-type specific grounding (Phase 8 — universal coverage)
    if action_type == "browser_package_check":
        if terminal_success:
            if not _TRACKING_RESULT_MARKERS.search(response):
                return GroundingResult(False, "tracking_success_not_referenced")
        else:
            if not _FAILURE_MARKERS.search(response):
                return GroundingResult(False, "tracking_failure_not_acknowledged")

    elif action_type in ("browser_form_workflow", "browser_web_workflow"):
        if terminal_success:
            # Reject very short responses only when they carry no evidence at all.
            # "Total: $42" (10 chars) and "Status: Delivered" (17 chars) must pass
            # because they contain numeric or structured evidence respectively.
            _short_no_evidence = (
                len(response.strip()) < 20
                and not _STRUCTURED_MARKER_RE.search(response)
                and not _NUMERIC_DATE_EVIDENCE_RE.search(response)
            )
            if _short_no_evidence:
                return GroundingResult(False, "success_response_too_short")
        else:
            if not _FAILURE_MARKERS.search(response):
                return GroundingResult(False, "workflow_failure_not_acknowledged")

    elif action_type == "browser_navigation":
        # Navigation: success response must reference page/content or screenshot
        screenshots = artifacts.get("screenshots", [])
        if terminal_success:
            if not _SCREENSHOT_RESULT_MARKERS.search(response) and not _SEARCH_RESULT_MARKERS.search(response):
                if not screenshots:
                    return GroundingResult(False, "navigation_no_content_referenced")
        else:
            if not _FAILURE_MARKERS.search(response):
                return GroundingResult(False, "navigation_failure_not_acknowledged")

    elif action_type == "email_send":
        if terminal_success:
            # Email sent: must mention email/sent/enviado
            _EMAIL_MARKERS = re.compile(
                r"(?:correo|email|mensaje|message|enviado|sent|envié|mail)",
                re.IGNORECASE,
            )
            if not _EMAIL_MARKERS.search(response):
                return GroundingResult(False, "email_sent_not_referenced")
        else:
            if not _FAILURE_MARKERS.search(response):
                return GroundingResult(False, "email_failure_not_acknowledged")

    # Check 3: Screenshot artifacts — if present AND success, response should reference visual output
    screenshots = artifacts.get("screenshots", [])
    if screenshots and terminal_success:
        if not _SCREENSHOT_RESULT_MARKERS.search(response):
            logger.info(
                "response_grounding.screenshot_not_mentioned",
                count=len(screenshots),
                preview=response[:60],
            )

    # Check 4: Universal failure honesty — any terminal failure needs acknowledgment
    if not terminal_success and results:
        any_failed = any(not getattr(r, "success", True) for r in results)
        if any_failed and not _FAILURE_MARKERS.search(response):
            return GroundingResult(False, "failure_not_acknowledged")

    # Check 5 + 6: Strict evidence requirements for all success responses.
    # Computed once here; both checks share these values.
    if terminal_success:
        _has_marker  = bool(_STRUCTURED_MARKER_RE.search(response))
        _has_numeric = bool(_NUMERIC_DATE_EVIDENCE_RE.search(response))
        _word_count  = len(response.split())
        _has_phrase  = _word_count >= 4

        # Check 5: Weak-evidence rejection
        # Must satisfy at least ONE of: structured marker / numeric evidence / ≥4 words.
        if not (_has_marker or _has_numeric or _has_phrase):
            logger.info(
                "response_rejected_weak_evidence detected_items=%d word_count=%d preview=%r",
                _count_fragments(response), _word_count, response[:80],
            )
            return GroundingResult(False, "single_fragment_insufficient_evidence")

        # Check 6: Generic weak-phrase filter
        # Responses that pass Check 5 only on word count but consist purely of
        # boilerplate ("completed successfully", "task completed", …) must also
        # carry numeric evidence, a structured marker, or a domain keyword.
        _norm = _normalize_for_phrase(response)
        for _phrase in _GENERIC_WEAK_PHRASES:
            if _phrase in _norm:
                _has_domain = bool(_DOMAIN_KEYWORD_RE.search(response))
                if not (_has_marker or _has_numeric or _has_domain):
                    logger.info(
                        "response_rejected_generic_phrase phrase=%r text=%r",
                        _phrase, response[:120],
                    )
                    return GroundingResult(False, "generic_phrase_no_evidence")
                break   # phrase matched but evidence present → accepted

        # Check 7: Plain status-marker value validation
        # If a plain "status: X" / "result: X" marker is present, validate X.
        # Bracket markers ([TRACK_STATUS:]) come from skills and are trusted — not checked here.
        _plain_m = _PLAIN_STATUS_RE.search(response)
        if _plain_m:
            _status_val = _plain_m.group(1).strip().lower()
            _val_ok = any(v in _status_val for v in _VALID_STATUS_VALUES)
            if not _val_ok and not _has_numeric:
                logger.info(
                    "response_rejected_invalid_status value=%r text=%r",
                    _plain_m.group(1).strip(), response[:120],
                )
                return GroundingResult(False, "invalid_status_value")

            # Check 7 refinement: valid status + generic weak phrase = boilerplate combo.
            # "status: completed successfully" passes whitelist ("completed") but is
            # still content-empty without numeric/date evidence.
            if _val_ok and not _has_numeric:
                _norm_val = _normalize_for_phrase(_status_val)
                for _gp in _GENERIC_WEAK_PHRASES:
                    if _gp in _norm_val:
                        logger.info(
                            "response_rejected_status_generic_combo value=%r text=%r",
                            _plain_m.group(1).strip(), response[:120],
                        )
                        return GroundingResult(False, "status_generic_combo_no_evidence")

    # Check 8: Intent evidence gate (requires user_text to classify intent).
    # When user_text is provided and terminal_success, check whether the intent
    # demands verifiable data and block if evidence is absent.
    if terminal_success and user_text:
        _ev = extract_evidence_from_results(results)
        _needs_ev = _requires_evidence(user_text)

        if _needs_ev:
            logger.info(
                "intent_requires_evidence intent=%r has_evidence=%s confidence=%.2f",
                user_text[:60], _ev.has_evidence, _ev.confidence,
            )

        if _needs_ev and not _ev.is_sufficient():
            logger.info(
                "evidence_missing_block intent=%r has_marker=%s has_numeric=%s"
                " has_url=%s has_weak_text=%s confidence=%.2f sources=%r",
                user_text[:60],
                _ev.has_marker, _ev.has_numeric, _ev.has_url,
                _ev.has_weak_text, _ev.confidence, _ev.sources,
            )
            return GroundingResult(False, "evidence_missing")

    # Check 9: Anti-hallucination guard — fires on terminal success regardless
    # of whether user_text is present. Autonomous/scheduled paths omit user_text
    # but must still be protected against fabricated factual claims.
    if terminal_success and _FACTUAL_CLAIM_RE.search(response):
        _ev9 = extract_evidence_from_results(results)
        if not _ev9.is_sufficient():
            logger.info(
                "hallucination_prevented intent=%r response=%r",
                (user_text or "<no-user-text>")[:60], response[:80],
            )
            return GroundingResult(False, "hallucination_no_evidence")

    return GroundingResult(True, "grounded")


def build_deterministic_response(
    results: list,
    action_type: str,
    ref_id: str,
    artifacts: dict,
    terminal_success: bool,
) -> str:
    """Build a factual user-facing response from execution data without LLM.

    Used as fallback when response_validator rejects the LLM output.
    Constructs a minimal but accurate response from verified skill outputs.
    """
    screenshots = artifacts.get("screenshots", [])
    shot_suffix = ""
    if screenshots:
        shot_lines = "\n".join(f"![captura]({p})" for p in screenshots[:3])
        shot_suffix = f"\n\n{shot_lines}"

    # ── Package tracking ──────────────────────────────────────────────────────
    if action_type == "browser_package_check":
        if terminal_success:
            track_data = _extract_tracking_data(results)
            code = ref_id or "el paquete"
            if track_data["status_line"]:
                resp = (
                    f"Resultado del rastreo para **{code}**:\n\n"
                    f"{track_data['status_line']}\n"
                )
                if track_data["details"]:
                    resp += f"\n{track_data['details'][:400]}"
            else:
                resp = f"Se obtuvo información de rastreo para **{code}**."
            if screenshots:
                resp += "\n\nSe tomó captura de pantalla como confirmación visual."
            return resp + shot_suffix
        else:
            from .handlers import _extract_failure_diagnostic
            diag = _extract_failure_diagnostic(results)
            code = ref_id or "el paquete"
            base = f"No pude rastrear **{code}**."
            if diag:
                base += f" {diag}"
            return base + " ¿Quieres intentarlo con otro método?"

    # ── Screenshots ───────────────────────────────────────────────────────────
    if action_type in ("browser_web_workflow", "browser_form_workflow", "browser_navigation"):
        if terminal_success and screenshots:
            resp = f"Tarea completada. Se capturaron {len(screenshots)} screenshot(s):"
            return resp + shot_suffix
        elif terminal_success:
            # Extract what was accomplished from results
            success_outputs = [
                (r.skill_name, (r.output or "")[:300])
                for r in results if r.success
            ]
            if success_outputs:
                last_name, last_out = success_outputs[-1]
                return f"Tarea completada ({last_name}). Resultado:\n{last_out[:300]}" + shot_suffix
            # No verifiable outputs — use the canonical insufficient-evidence message
            return GROUNDING_INSUFFICIENT_EVIDENCE_MSG
        else:
            from .handlers import _extract_failure_diagnostic
            diag = _extract_failure_diagnostic(results)
            base = "No pude completar la tarea solicitada."
            if diag:
                base += f" {diag}"
            return base + " ¿Quieres intentarlo de nuevo?"

    # ── Generic fallback ──────────────────────────────────────────────────────
    if terminal_success:
        success_outputs = [
            (r.skill_name, (r.output or "")[:200])
            for r in results if r.success
        ]
        if success_outputs:
            parts = [f"[{name}]: {out}" for name, out in success_outputs[:2]]
            return "Resultado:\n" + "\n".join(parts) + shot_suffix
        # No verifiable outputs — use the canonical insufficient-evidence message
        return GROUNDING_INSUFFICIENT_EVIDENCE_MSG
    else:
        from .handlers import _extract_failure_diagnostic
        diag = _extract_failure_diagnostic(results)
        return (
            "No se pudo completar la operación."
            + (f" {diag}" if diag else "")
        )


def _extract_tracking_data(results: list) -> dict:
    """Extract tracking status and details from browser skill results."""
    status_line = ""
    details = ""
    for r in results:
        if r.skill_name == "browser" and r.output:
            out = r.output
            for line in out.split("\n"):
                if "[TRACK_STATUS:" in line:
                    status_line = line.strip()
                    break
            # Extract tracking results section
            if "Tracking results for" in out:
                details = out.split("Tracking results for", 1)[1][:500].strip()
            elif status_line and not details:
                # Grab context around status line
                idx = out.find(status_line)
                if idx >= 0:
                    details = out[max(0, idx - 200):idx + 300].strip()
            if status_line:
                break
    return {"status_line": status_line, "details": details}


def extract_goal_domain(action_intent) -> str:
    """Extract the primary domain/target from an action intent for grounding checks."""
    if action_intent is None:
        return ""
    target = getattr(action_intent, "action_target", "") or ""
    # Extract domain from URL if present
    m = re.search(r"(?:https?://)?(?:www\.)?([a-z0-9-]+\.[a-z]{2,})", target, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return target.lower()[:40]
