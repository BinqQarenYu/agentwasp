"""
WASP Phase 4 — Evidence-Based Execution Memory (Hardened)
==========================================================
Stores ONLY verified successful executions as reusable patterns.
Patterns are retrieved before execution and injected as GUIDANCE ONLY —
the LLM must still plan and execute; pre_execution_check still applies.

Improvements applied (Phase 4 hardening):
  Imp-1: Initial usage_count=1, success_count=1, failure_count=0
  Imp-2: success_rate computed from success_count / (success_count + failure_count)
  Imp-3: Structured objective signature (intent + domain + goal_type + text fallback)
  Imp-4: Staleness decay in scoring (days since last use)

Storage: Redis (key-per-pattern + intent-type index set)
All operations are async and fail-open; exceptions never crash callers.
"""
from __future__ import annotations

import re
import json
import time
import hashlib
import structlog

logger = structlog.get_logger()

# ── ID / noise patterns to normalize out of signatures ───────────────────────
_ID_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b'), "<id>"),
    (re.compile(r'https?://\S+'), "<url>"),
    (re.compile(r'\b[A-Za-z]{1,3}\d{5,}[A-Za-z0-9]*\b'), "<id>"),
    (re.compile(r'\b[A-Z0-9]{8,}\b'), "<id>"),
    (re.compile(r'\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b'), "<date>"),
    (re.compile(r'\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b'), "<date>"),
    (re.compile(r'\b\d{4,}\b'), "<num>"),
]

_STOP_WORDS = frozenset({
    "the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "or",
    "with", "from", "by", "is", "are", "was", "been", "be", "it", "this",
    "that", "me", "my", "please", "can", "you", "i", "we", "its", "will",
    "output", "must", "contain", "required", "evidence", "present",
})

# ── Improvement 3: goal_type keyword mapping ──────────────────────────────────
# Maps known-intent keywords to a canonical goal_type label.
# Checked against the normalized objective text in order; first match wins.
_GOAL_TYPE_KEYWORDS: list[tuple[str, frozenset]] = [
    ("tracking_status",  frozenset(["track", "rastrear", "tracking", "shipment",
                                    "package", "paquete", "rastreo", "envio"])),
    ("send_message",     frozenset(["send", "enviar", "email", "correo", "mensaje",
                                    "notify", "notificar", "mail"])),
    ("book_appointment", frozenset(["book", "agendar", "reservar", "appointment",
                                    "cita", "schedule", "hora"])),
    ("search_results",   frozenset(["search", "buscar", "find", "encontrar",
                                    "lookup", "consultar", "query"])),
    ("form_submission",  frozenset(["form", "fill", "submit", "register", "signup",
                                    "formulario", "registrar"])),
    ("price_check",      frozenset(["price", "precio", "cost", "costo", "rate",
                                    "tarifa", "quote", "cotizar"])),
    ("login_auth",       frozenset(["login", "sign", "auth", "acceder", "ingresar",
                                    "iniciar"])),
    ("data_extraction",  frozenset(["extract", "scrape", "download", "obtener",
                                    "extraer", "retrieve", "get", "fetch"])),
]


def _derive_goal_type(text: str) -> str:
    """Map normalized objective text to a canonical goal_type label.

    Returns "" if no keyword cluster matches — signals open-ended task.
    """
    words = frozenset(text.lower().split())
    for goal_type, keywords in _GOAL_TYPE_KEYWORDS:
        if keywords.intersection(words):
            return goal_type
    return ""


def _generate_objective_signature(objective_spec) -> str:
    """Normalize task identity: strip IDs, preserve intent structure.

    Returns the text-based signature (keyword fingerprint).
    Use _build_structured_sig() when the full structured form is needed.
    """
    if objective_spec is None:
        return ""

    parts: list[str] = []
    obj = (getattr(objective_spec, "objective", "") or "").strip()
    if obj:
        parts.append(obj)
    done_when = getattr(objective_spec, "done_when", []) or []
    for dw in (done_when or [])[:3]:
        s = str(dw).strip()
        if s:
            parts.append(s)

    if not parts:
        return ""

    text = " ".join(parts).lower()

    for pattern, replacement in _ID_PATTERNS:
        text = pattern.sub(replacement, text)

    text = re.sub(r"\s+", " ", text).strip()

    words = [
        w for w in text.split()
        if len(w) >= 3
        and w not in _STOP_WORDS
        and not w.startswith("<")
    ]

    sig = " ".join(words[:8])
    return sig if sig else ""


def _build_structured_sig(
    intent_type: str,
    objective_spec,
    active_domain_lock,
) -> dict:
    """Build the structured signature for a pattern (Improvement 3).

    Fields:
      intent   — action_type from ActionIntent (used as primary index key)
      domain   — primary domain from DomainLock, "" if none
      goal     — canonical goal_type derived from objective text, "" if none
      text     — normalized text signature (fallback for Jaccard similarity)

    Matching priority (in find_pattern):
      1. intent_type  — enforced by Redis index key (only same-intent patterns searched)
      2. domain       — compatibility check (existing logic)
      3. goal_type    — exact match → structural bonus +0.20 added to sim score
      4. text         — Jaccard fallback (existing logic)
    """
    text_sig = _generate_objective_signature(objective_spec)

    # Primary domain: first entry from DomainLock if available
    domains = sorted(getattr(active_domain_lock, "domains", None) or [])
    primary_domain = domains[0] if domains else ""

    # Goal type from objective text
    obj_text = (getattr(objective_spec, "objective", "") or "").lower()
    goal_type = _derive_goal_type(obj_text) if obj_text else ""

    return {
        "intent": intent_type,
        "domain": primary_domain,
        "goal": goal_type,
        "text": text_sig,
    }


def _sig_similarity(sig_a: str, sig_b: str) -> float:
    """Jaccard similarity between the keyword sets of two text signatures."""
    words_a = frozenset(sig_a.lower().split())
    words_b = frozenset(sig_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def _score_pattern(
    query_sig: dict,
    stored: dict,
    query_domains: frozenset,
) -> float:
    """Compute the final match score for a candidate pattern (Improvement 3+4).

    Score = text_similarity × success_rate × staleness_decay

    Structural goal_type match adds +0.20 bonus to text_similarity (capped at 1.0).
    Staleness decay: max(0.5, 1 - days_since_last_use / 30).
    """
    # ── Text similarity (base) ────────────────────────────────────────────────
    sim = _sig_similarity(
        query_sig.get("text", ""),
        stored.get("objective_signature", ""),
    )

    # ── Improvement 3: goal_type structural bonus ─────────────────────────────
    q_goal = query_sig.get("goal", "")
    s_structured = stored.get("sig_structured", {}) or {}
    s_goal = s_structured.get("goal", "")
    if q_goal and s_goal and q_goal == s_goal:
        sim = min(1.0, sim + 0.20)

    # ── Improvement 4: staleness decay ───────────────────────────────────────
    last_used = stored.get("last_used_at", time.time())
    days_idle = max(0.0, (time.time() - last_used) / 86400.0)
    decay = max(0.5, 1.0 - (days_idle / 30.0))

    # ── Success rate (from explicit counts when available) ────────────────────
    sc = stored.get("success_count", None)
    fc = stored.get("failure_count", None)
    if sc is not None and fc is not None and (sc + fc) > 0:
        sr = sc / (sc + fc)
    else:
        sr = stored.get("success_rate", 1.0)

    return sim * sr * decay


def _recompute_success_rate(p: dict) -> float:
    """Compute success_rate from explicit counts (Improvement 2)."""
    sc = p.get("success_count", 0)
    fc = p.get("failure_count", 0)
    total = sc + fc
    return sc / total if total > 0 else 1.0


def _strip_url_params(url: str) -> str:
    """Remove query string, fragment, and specific path ID segments."""
    if not url:
        return ""
    url = re.sub(r'\?.*$', '', url)
    url = re.sub(r'#.*$', '', url)
    url = re.sub(r'/[A-Za-z0-9]{12,}(?=/|$)', '/<id>', url)
    url = re.sub(r'/\d{4,}(?=/|$)', '/<id>', url)
    return url


def _normalize_step(step: dict) -> dict:
    """Reduce an action_history entry to its reusable structural form."""
    return {
        "action": step.get("action", ""),
        "url": _strip_url_params(step.get("url", "") or ""),
        "success": step.get("success", False),
    }


def _backfill_counts(p: dict) -> dict:
    """Backward-compat: derive success_count/failure_count for patterns stored
    before Improvement 2. Mutates p in-place, returns it."""
    if "success_count" not in p:
        uc = p.get("usage_count", 1)
        sr = p.get("success_rate", 1.0)
        p["success_count"] = max(1, round(uc * sr))
        p["failure_count"] = max(0, uc - p["success_count"])
    if "created_at" not in p:
        p["created_at"] = p.get("last_used_at", time.time())
    return p


# ── Main class ────────────────────────────────────────────────────────────────

class ExecutionMemory:
    """Evidence-based execution memory: store verified patterns, retrieve for reuse."""

    MIN_SUCCESS_RATE: float = 0.70
    MIN_USAGE_COUNT: int = 2
    SIMILARITY_THRESHOLD: float = 0.55
    MAX_PATTERNS_PER_INTENT: int = 20
    PATTERN_TTL: int = 86400 * 30          # 30 days

    _KEY_PREFIX = "exec_pattern:"
    _IDX_PREFIX = "exec_patterns_idx:"

    def _pattern_key(self, pattern_id: str) -> str:
        return f"{self._KEY_PREFIX}{pattern_id}"

    def _idx_key(self, intent_type: str) -> str:
        return f"{self._IDX_PREFIX}{intent_type}"

    def _make_pattern_id(self, intent_type: str, sig: str) -> str:
        return hashlib.sha256(f"{intent_type}:{sig}".encode()).hexdigest()[:16]

    async def store_pattern(
        self,
        redis_url: str,
        intent_type: str,
        objective_spec,
        action_history: list,
        action_all_results: list,
        active_domain_lock,
    ) -> None:
        """Store or update a verified successful execution pattern.

        Improvement 1: New patterns start with usage_count=1, success_count=1, failure_count=0.
        Improvement 2: On update, success_count += 1; success_rate recomputed from counts.
        Improvement 3: sig_structured stored alongside text signature.
        Improvement 4: created_at set on first store.
        """
        if not redis_url or not intent_type:
            return
        try:
            import redis.asyncio as _aioredis

            text_sig = _generate_objective_signature(objective_spec)
            if not text_sig:
                return

            structured_sig = _build_structured_sig(intent_type, objective_spec, active_domain_lock)

            domains = sorted(getattr(active_domain_lock, "domains", None) or [])
            tools_used = sorted({
                r.skill_name for r in (action_all_results or [])
                if getattr(r, "success", False)
            })
            steps = [
                _normalize_step(s) for s in (action_history or []) if s.get("success")
            ]
            if not steps:
                return

            evidence_required = list(getattr(objective_spec, "required_evidence", []) or [])
            pattern_id = self._make_pattern_id(intent_type, text_sig)

            _r = _aioredis.from_url(redis_url, decode_responses=True)
            try:
                existing_raw = await _r.get(self._pattern_key(pattern_id))
                if existing_raw:
                    p = json.loads(existing_raw)
                    _backfill_counts(p)              # backward compat
                    # Improvement 2: increment success_count, recompute rate
                    p["success_count"] = p.get("success_count", 1) + 1
                    p["usage_count"] = p.get("usage_count", 1) + 1
                    p["success_rate"] = _recompute_success_rate(p)
                    p["last_used_at"] = time.time()
                    p["sig_structured"] = structured_sig  # keep up to date
                    await _r.setex(
                        self._pattern_key(pattern_id), self.PATTERN_TTL, json.dumps(p)
                    )
                    logger.info(
                        "execution_memory.pattern_stored",
                        action="updated",
                        pattern_id=pattern_id,
                        intent_type=intent_type,
                        sig=text_sig[:80],
                        success_rate=round(p["success_rate"], 3),
                        success_count=p["success_count"],
                        failure_count=p.get("failure_count", 0),
                        usage_count=p["usage_count"],
                    )
                else:
                    idx_size = await _r.scard(self._idx_key(intent_type))
                    if idx_size >= self.MAX_PATTERNS_PER_INTENT:
                        return

                    now = time.time()
                    p = {
                        "id": pattern_id,
                        "objective_signature": text_sig,
                        "sig_structured": structured_sig,    # Improvement 3
                        "intent_type": intent_type,
                        "domains": domains,
                        "steps": steps[:10],
                        "tools_used": tools_used,
                        "evidence_required": evidence_required,
                        # Improvement 1: start from verified success, not zero
                        "success_rate": 1.0,
                        "usage_count": 1,
                        "success_count": 1,
                        "failure_count": 0,
                        "created_at": now,                   # Improvement 4
                        "last_used_at": now,
                    }
                    await _r.setex(
                        self._pattern_key(pattern_id), self.PATTERN_TTL, json.dumps(p)
                    )
                    await _r.sadd(self._idx_key(intent_type), pattern_id)
                    await _r.expire(self._idx_key(intent_type), self.PATTERN_TTL)
                    logger.info(
                        "execution_memory.pattern_stored",
                        action="new",
                        pattern_id=pattern_id,
                        intent_type=intent_type,
                        sig=text_sig[:80],
                        goal=structured_sig.get("goal", ""),
                        steps=len(steps),
                        tools=tools_used,
                    )
            finally:
                await _r.aclose()
        except Exception as _e:
            logger.warning("execution_memory.store_failed", error=str(_e)[:120])

    async def find_pattern(
        self,
        redis_url: str,
        intent_type: str,
        objective_spec,
        active_domain_lock,
    ) -> "dict | None":
        """Find best matching pattern; returns None on any failure (fail-open)."""
        if not redis_url or not intent_type:
            return None
        try:
            import redis.asyncio as _aioredis
            query_sig = _build_structured_sig(intent_type, objective_spec, active_domain_lock)
            if not query_sig.get("text"):
                return None
            query_domains = frozenset(getattr(active_domain_lock, "domains", None) or [])
            _r = _aioredis.from_url(redis_url, decode_responses=True)
            try:
                pattern_ids = await _r.smembers(self._idx_key(intent_type))
                if not pattern_ids:
                    return None
                best: dict | None = None
                best_score = 0.0
                for pid in pattern_ids:
                    raw = await _r.get(self._pattern_key(pid))
                    if not raw:
                        continue
                    p = json.loads(raw)
                    _backfill_counts(p)
                    sr = _recompute_success_rate(p)
                    if sr < self.MIN_SUCCESS_RATE:
                        continue
                    if p.get("usage_count", 0) < self.MIN_USAGE_COUNT:
                        continue
                    p_domains = frozenset(p.get("domains", []))
                    if query_domains and p_domains and not query_domains.intersection(p_domains):
                        continue
                    base_sim = _sig_similarity(
                        query_sig.get("text", ""), p.get("objective_signature", "")
                    )
                    if base_sim < self.SIMILARITY_THRESHOLD:
                        continue
                    score = _score_pattern(query_sig, p, query_domains)
                    if score > best_score:
                        best_score = score
                        best = p

                if best:
                    logger.info(
                        "execution_memory.pattern_reused",
                        pattern_id=best["id"],
                        intent_type=intent_type,
                        query_sig=query_sig.get("text", "")[:80],
                        stored_sig=best.get("objective_signature", "")[:80],
                        query_goal=query_sig.get("goal", ""),
                        stored_goal=(best.get("sig_structured") or {}).get("goal", ""),
                        score=round(best_score, 3),
                        success_rate=round(_recompute_success_rate(best), 3),
                        success_count=best.get("success_count"),
                        failure_count=best.get("failure_count"),
                        usage_count=best.get("usage_count"),
                    )
                    return best
                return None
            finally:
                await _r.aclose()
        except Exception as _e:
            logger.warning("execution_memory.find_failed", error=str(_e)[:120])
            return None

    async def record_failure(self, redis_url: str, pattern_id: str) -> None:
        """Increment failure_count; recompute success_rate (Imp-2)."""
        if not redis_url or not pattern_id:
            return
        try:
            import redis.asyncio as _aioredis
            _r = _aioredis.from_url(redis_url, decode_responses=True)
            try:
                raw = await _r.get(self._pattern_key(pattern_id))
                if not raw:
                    return
                p = json.loads(raw)
                _backfill_counts(p)
                old_sr = _recompute_success_rate(p)
                p["failure_count"] = p.get("failure_count", 0) + 1
                p["usage_count"] = p.get("usage_count", 1) + 1
                p["success_rate"] = _recompute_success_rate(p)
                p["last_used_at"] = time.time()
                await _r.setex(
                    self._pattern_key(pattern_id), self.PATTERN_TTL, json.dumps(p)
                )
                logger.info(
                    "execution_memory.pattern_score_updated",
                    pattern_id=pattern_id,
                    old_success_rate=round(old_sr, 3),
                    new_success_rate=round(p["success_rate"], 3),
                    success_count=p.get("success_count"),
                    failure_count=p["failure_count"],
                    below_threshold=p["success_rate"] < self.MIN_SUCCESS_RATE,
                )
            finally:
                await _r.aclose()
        except Exception as _e:
            logger.warning("execution_memory.record_failure_failed", error=str(_e)[:120])

    def format_hint(self, pattern: dict) -> str:
        """Format a matched pattern as LLM guidance text (GUIDANCE ONLY)."""
        steps = pattern.get("steps", [])
        tools = pattern.get("tools_used", [])
        sc = pattern.get("success_count")
        fc = pattern.get("failure_count")
        sr = _recompute_success_rate(pattern) if sc is not None else pattern.get("success_rate", 0.0)
        uc = pattern.get("usage_count", 1)
        sig = pattern.get("objective_signature", "")[:80]
        goal = (pattern.get("sig_structured") or {}).get("goal", "")

        step_lines = [
            f"  {i}. {s.get('action', '?')}" + (f"  →  {s['url']}" if s.get("url") else "")
            for i, s in enumerate(steps[:8], 1)
        ]

        return (
            "[EXECUTION PATTERN — GUIDANCE ONLY]\n"
            f"A similar task matched this pattern "
            f"(success_rate={sr:.0%}, {sc or uc} successes"
            + (f", {fc} failures" if fc else "")
            + f", used {uc}× previously"
            + (f", goal: {goal}" if goal else "")
            + ").\n"
            f"Matched on: \"{sig}\"\n"
            "Suggested approach:\n"
            + ("\n".join(step_lines) if step_lines else "  (no steps recorded)") + "\n"
            + (f"Tools involved: {', '.join(tools)}\n" if tools else "")
            + "↑ This is a HINT. You MUST plan and execute independently. "
            "Adapt these steps to the current task context. "
            "Pre-execution checks still apply."
        )


# ── Module-level singleton ────────────────────────────────────────────────────
execution_memory = ExecutionMemory()
