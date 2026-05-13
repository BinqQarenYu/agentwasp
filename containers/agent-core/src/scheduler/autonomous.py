"""Autonomous Goal Generator — Agent Wasp's proactive awareness system.

Runs every 30 minutes. Gathers the current state of the world (system health,
active tasks, recent errors, user context) and asks the LLM:
  "Given this situation, is there anything I should do proactively?"

If yes, creates a GoalOrchestrator goal autonomously — without the user asking.

Examples of what this detects and acts on:
  - Disk usage >85% → creates a cleanup goal
  - Scheduler job failing repeatedly → creates a diagnostic goal
  - User hasn't been active for unusual time → sends a check-in
  - No crypto price task running but user historically asked daily → remind them
  - Goal failed 3x in a row → notify user and pause it
  - Memory usage critical → cleanup goal

This is what separates a reactive tool from a genuinely proactive agent.

Dedup layer (action signature memory):
  Before creating a goal, the LLM-proposed action text is normalized + hashed
  into a signature.  If a goal with that same signature has failed within the
  last 24h (and the failure is *not* transient — i.e. not a timeout / network /
  rate limit), the new goal is skipped and the event is logged.  This breaks
  hourly loops where the LLM keeps re-suggesting the same sandbox-violating
  action it can't actually execute.
"""

import hashlib
import json
import re
import time
import structlog
import psutil
from datetime import datetime, timezone
from uuid import uuid4

import redis.asyncio as aioredis
from sqlalchemy import text

from ..config import now_local
from ..db.session import async_session
from ..events.bus import EventBus
from ..events.types import EventType
from ..models.manager import ModelManager
from ..models.types import Message, ModelRequest
from ..utils.safe_notify import safe_notify

logger = structlog.get_logger()

AUTONOMOUS_STATE_KEY = "agent:autonomous_state"
LAST_ACTIVE_KEY = "agent:last_active"
MIN_INTERVAL_BETWEEN_GOALS = 3600  # Don't create more than 1 autonomous goal per hour
MAX_GOALS_PER_DAY = 5

# ── Action dedup (signature → outcome cache) ─────────────────────────────────
ACTION_SIGNATURE_KEY_PREFIX = "autonomous:action:"   # hash(normalized_action) → {status, ts, reason}
GOAL_SIGNATURE_KEY_PREFIX = "autonomous:goal:"        # goal_id → signature (cleared on sweep)
ACTION_DEDUP_TTL = 86400                              # 24 hours


# Patterns that classify a failure as TRANSIENT — these MUST NOT be deduped,
# the action could legitimately succeed on retry.
_TRANSIENT_PATTERNS = re.compile(
    r"\b(?:timeout|timed\s+out|connection\s+(?:reset|refused|aborted)|"
    r"network|dns|temporary|rate[\s-]?limit|429|502|503|504|"
    r"unavailable|throttl|retry\s+later)\b",
    re.IGNORECASE,
)

# Patterns that confirm a failure is PERSISTENT (sandbox / restriction / missing path).
# When matched we know retrying the same action will fail again.
_PERSISTENT_PATTERNS = re.compile(
    r"\b(?:outside\s+the\s+allowed|not\s+allowed|permission\s+denied|"
    r"forbidden|restricted|sandbox|skill\.restricted|"
    r"file\s+not\s+found|no\s+such\s+file|invalid\s+path|"
    r"path\s+(?:traversal|outside)|exit\s+code:\s*[1-9])\b",
    re.IGNORECASE,
)


def _normalize_action_text(action: str) -> str:
    """Normalize an LLM-proposed action string for stable hashing.

    Strips dynamic tokens (timestamps, dates, UUIDs, raw numbers) that would
    cause two semantically-identical actions to hash differently.
    """
    if not action:
        return ""
    s = action.lower().strip()
    # ISO timestamps
    s = re.sub(r"\b\d{4}-\d{2}-\d{2}[t\s]\d{2}:\d{2}:\d{2}\S*\b", "<ts>", s)
    # Wall-clock times
    s = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", "<time>", s)
    # Dates (slash form)
    s = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", "<date>", s)
    # UUIDs
    s = re.sub(r"\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b", "<uuid>", s)
    # Raw numbers (percentages, sizes) — unify so "85%" and "92%" collapse together
    s = re.sub(r"\b\d+(?:\.\d+)?\s*(?:%|gb|mb|kb|s|ms|h)?\b", "<n>", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ── Lightweight context extraction for signature precision ──────────────────
#
# The signature combines three orthogonal axes:
#   1. action_type — coarse verb intent (file_read, shell, web, analysis, …)
#   2. target_hint — the concrete resource the action operates on (path, URL
#      domain, quoted entity).  Extracted from the raw text so two actions
#      worded the same but pointing at different resources don't collide.
#   3. normalized_text — existing normalization (timestamps/UUIDs/numbers
#      collapsed) for stability across cosmetic variation.
#
# Targets are matched in the order of specificity below: most precise first.
_TARGET_EXTRACTORS: list[tuple[re.Pattern, "callable"]] = [
    # URL → "url:<host>"  (path/query intentionally dropped — domain is the
    # right granularity; query strings are too volatile)
    (re.compile(r"https?://([^/\s)]+)", re.I),
     lambda m: f"url:{m.group(1).lower()}"),
    # Absolute Unix path
    (re.compile(r"(?<![\w/])(/[a-z0-9._\-/]{2,})", re.I),
     lambda m: f"path:{m.group(1).lower()}"),
    # Email address — the local+domain together, since same domain different
    # local part is still a different recipient
    (re.compile(r"\b([a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,})\b", re.I),
     lambda m: f"email:{m.group(1).lower()}"),
    # Double-quoted entity — common when LLM wraps a target in quotes
    (re.compile(r'"([^"\n]{2,80})"'),
     lambda m: f"quoted:{m.group(1).strip().lower()}"),
    (re.compile(r"'([^'\n]{2,80})'"),
     lambda m: f"quoted:{m.group(1).strip().lower()}"),
]


# Coarse action-type keywords (English + Spanish).  Order matters: a more
# specific intent (file_read) is matched before a more general one (analysis).
_ACTION_TYPE_KEYWORDS: list[tuple[str, re.Pattern]] = [
    ("file_read",   re.compile(r"\b(?:read|leer|cat|tail|head|view|inspect|examinar|inspeccionar)\b", re.I)),
    ("file_write",  re.compile(r"\b(?:write|escribir|create\s+file|crear\s+archivo|save|guardar|append)\b", re.I)),
    ("file_delete", re.compile(r"\b(?:delete|borrar|remove|eliminar|\brm\b|unlink)\b", re.I)),
    ("shell",       re.compile(r"\b(?:shell|run\s+command|ejecutar\s+comando|execute\s+command|bash|sh\b)\b", re.I)),
    ("web",         re.compile(r"\b(?:browse|navigate|navegar|visit|fetch|http|website|sitio\s+web)\b", re.I)),
    ("email",       re.compile(r"\b(?:email|gmail|mail|enviar\s+correo|send\s+(?:an?\s+)?email)\b", re.I)),
    ("search",      re.compile(r"\b(?:search\s+(?:the\s+)?web|web_search|buscar\s+en|google|duckduckgo)\b", re.I)),
    ("monitor",     re.compile(r"\b(?:monitor|monitorear|watch|observe|seguir|track\b)\b", re.I)),
    ("notify",      re.compile(r"\b(?:notify|notificar|alert|alertar|warn|advertir|avisar)\b", re.I)),
    ("clean",       re.compile(r"\b(?:clean(?:up)?|limpiar|free\s+space|liberar|prune|purge|vac+um)\b", re.I)),
    # analysis is intentionally last — it's the catch-all for "investigate /
    # diagnose / review" verbs that don't carry a more concrete intent
    ("analysis",    re.compile(r"\b(?:analy[sz]e|analizar|diagnose|diagnosticar|investigate|investigar|review|revisar|check|chequear|audit)\b", re.I)),
]


def _extract_target_hint(action: str) -> str:
    """Best-effort: extract a stable target token from the raw action text.

    Returns "" when no obvious target is present (then the signature falls
    back to action_type + normalized_text, which is fine for general advice).
    """
    if not action:
        return ""
    for pattern, formatter in _TARGET_EXTRACTORS:
        m = pattern.search(action)
        if m:
            return formatter(m)[:80]
    return ""


def _infer_action_type(action: str) -> str:
    """Coarse classification of the action's intent. Returns 'generic' if no match."""
    if not action:
        return "generic"
    for label, pattern in _ACTION_TYPE_KEYWORDS:
        if pattern.search(action):
            return label
    return "generic"


def _action_signature(action: str) -> str:
    """Context-aware signature for an LLM-proposed action.

    Combines coarse action_type + target_hint + normalized_text so that:
      - "Read /var/log/error.log" and "Read /etc/passwd" hash differently
        (same wording, different target → DIFFERENT signature)
      - "Read /var/log/error.log at 14:00" and "Read /var/log/error.log at 15:30"
        hash identically (same wording + target, only timestamp differs)
      - Actions without a concrete target still hash by type + text, so
        general suggestions ("clean up disk") remain dedupable.
    """
    norm = _normalize_action_text(action)
    if not norm:
        return ""
    action_type = _infer_action_type(action)
    target = _extract_target_hint(action)
    composite = f"{action_type}|{target}|{norm}"
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()[:16]


def _is_transient_failure(reason: str) -> bool:
    """Classify a failure reason as transient (don't dedupe) vs persistent.

    Returns True only when the failure looks like a transient/retryable error.
    Unknown reasons → False (be permissive, allow retry).
    """
    if not reason:
        return False
    if _TRANSIENT_PATTERNS.search(reason):
        return True
    return False


def _is_persistent_failure(reason: str) -> bool:
    """True when failure is clearly persistent (sandbox / missing path / restricted)."""
    if not reason:
        return False
    return bool(_PERSISTENT_PATTERNS.search(reason))

# Situational detection thresholds
DISK_ALERT_THRESHOLD = 85.0    # %
MEMORY_ALERT_THRESHOLD = 90.0  # %
CPU_ALERT_THRESHOLD = 95.0     # %
GOAL_FAILURE_THRESHOLD = 3     # consecutive failures before alerting


class AutonomousGoalGeneratorJob:
    """Evaluates world state and generates proactive goals when warranted."""

    def __init__(
        self,
        model_manager: ModelManager,
        redis_url: str,
        bus: EventBus,
        notify_chat_id: str = "",
        goal_orchestrator=None,
    ):
        self.model_manager = model_manager
        self.redis_url = redis_url
        self.bus = bus
        self.notify_chat_id = notify_chat_id
        self.goal_orchestrator = goal_orchestrator

    async def __call__(self) -> str:
        from ..policy import with_trace
        async with with_trace(
            self.redis_url, path="autonomous",
            chat_id=self.notify_chat_id, user_text="autonomous_evaluation",
        ) as _trace:
            return await self._run(_trace)

    async def _run(self, _trace=None) -> str:
        def _g(label: str):
            if _trace is not None:
                _trace.add_guard(label)

        # Skip if CPI is high — user-facing work takes priority
        from ..agent.cpi import is_high as _cpi_high
        if await _cpi_high(self.redis_url):
            logger.info("autonomous.cpi_throttled")
            _g("autonomous:cpi_throttled")
            return "Autonomous: skipped (CPI high)"

        # Sweep pending goal→signature mappings: if a previously-created
        # autonomous goal has reached a terminal state, record the outcome
        # against its signature for future dedup checks.
        try:
            await self._sweep_pending_goal_outcomes()
        except Exception:
            logger.exception("autonomous.sweep_error")

        # Rate limit: don't run if we already created a goal recently
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            state_raw = await r.get(AUTONOMOUS_STATE_KEY)
        finally:
            await r.aclose()

        now = time.time()
        state = {}
        if state_raw:
            try:
                state = json.loads(state_raw)
            except Exception:
                pass

        last_goal_time = state.get("last_goal_at", 0)
        goals_today = state.get("goals_today", 0)
        last_goal_date = state.get("last_goal_date", "")
        today = now_local().strftime("%Y-%m-%d")

        if last_goal_date != today:
            goals_today = 0
            state["goals_today"] = 0  # Persist reset into state dict (fixes counter-never-resets bug)

        if goals_today >= MAX_GOALS_PER_DAY:
            _g("autonomous:daily_limit")
            return f"Autonomous: daily limit reached ({goals_today} goals today)"

        if now - last_goal_time < MIN_INTERVAL_BETWEEN_GOALS:
            remaining = int((MIN_INTERVAL_BETWEEN_GOALS - (now - last_goal_time)) / 60)
            _g("autonomous:rate_limited")
            return f"Autonomous: rate limited, next check in {remaining}m"

        # Gather world state
        world_state = await self._gather_world_state()

        # Check for immediate/critical situations that don't need LLM
        critical_action = self._check_critical(world_state)
        if critical_action:
            await self._notify(critical_action["message"])
            await self._update_state(state, today, now)
            _g(f"autonomous:critical[{critical_action['type']}]")
            return f"Autonomous: critical action taken — {critical_action['type']}"

        # Ask LLM if there's anything proactive to do
        if not self.model_manager.active_model:
            _g("autonomous:no_model")
            return "Autonomous: no model available"

        action = await self._evaluate_with_llm(world_state)
        if not action:
            _g("autonomous:no_action_needed")
            return "Autonomous: no proactive action needed"

        # Dedup: skip if this exact action recently failed with a persistent error
        signature = _action_signature(action)
        if signature:
            try:
                prior = await self._lookup_signature(signature)
            except Exception:
                prior = None
            if prior and prior.get("status") == "failed":
                logger.info(
                    "autonomous.goal_skipped_duplicate",
                    signature=signature,
                    reason=str(prior.get("reason", ""))[:120],
                    original_failed_at=prior.get("ts", 0),
                    action_preview=action[:120],
                )
                # Light learning signal: surface the suppression in state so
                # downstream (dream / integrity) can see that autonomy is
                # learning rather than silently failing.
                state["last_skip_signature"] = signature
                state["last_skip_reason"] = "duplicate_failed_action"
                state["last_skip_at"] = now
                await self._persist_state_only(state)
                return f"Autonomous: skipped (action previously failed, signature={signature[:8]})"

        # Execute the proactive action — create a real goal if orchestrator is available
        if self.goal_orchestrator:
            try:
                goal = await self.goal_orchestrator.create_goal(
                    objective=action,
                    chat_id=self.notify_chat_id,
                    autonomy_mode=None,
                    priority=3,
                    source="autonomous",
                )
                logger.info("autonomous.goal_created", goal_id=goal.id[:8], action=action[:80])
                # Record goal_id → signature so the next run's sweep can
                # update the signature outcome based on goal terminal state.
                if signature:
                    try:
                        await self._remember_goal_signature(goal.id, signature)
                    except Exception:
                        logger.debug("autonomous.signature_remember_skip")
                await self._notify(f"[Proactive action]\n{action}")
            except Exception:
                logger.exception("autonomous.goal_create_failed")
                await self._notify(action)
        else:
            await self._notify(action)
        await self._update_state(state, today, now)
        logger.info("autonomous.goal_generated", action=action[:100])
        _g("autonomous:goal_created")
        return f"Autonomous: generated proactive action — {action[:80]}"

    async def _gather_world_state(self) -> dict:
        """Collect current system and agent state."""
        state = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "system": {},
            "agent": {},
            "tasks": [],
            "recent_errors": [],
            "goal_failures": [],
            "user_inactive_hours": 0,
        }

        # System metrics
        try:
            import asyncio as _asyncio
            disk = psutil.disk_usage("/")
            mem = psutil.virtual_memory()
            cpu = await _asyncio.to_thread(psutil.cpu_percent, interval=0.5)
            state["system"] = {
                "disk_percent": disk.percent,
                "disk_free_gb": round(disk.free / (1024**3), 1),
                "memory_percent": mem.percent,
                "cpu_percent": cpu,
            }
        except Exception:
            pass

        # User inactivity
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            last_active_str = await r.get(LAST_ACTIVE_KEY)
            if last_active_str:
                inactive = (time.time() - float(last_active_str)) / 3600
                state["user_inactive_hours"] = round(inactive, 1)

            # Active custom tasks
            tasks_raw = await r.hgetall("custom_tasks")
            for _, t_json in tasks_raw.items():
                try:
                    t = json.loads(t_json)
                    state["tasks"].append({
                        "name": t.get("name", ""),
                        "status": t.get("status", ""),
                        "last_run": t.get("last_run", ""),
                        "failure_count": t.get("failure_count", 0),
                    })
                except Exception:
                    pass
            await r.aclose()
        except Exception:
            pass

        # Recent errors from audit log
        try:
            async with async_session() as session:
                result = await session.execute(
                    text("""
                        SELECT action, error, COUNT(*) as cnt
                        FROM audit_log
                        WHERE error IS NOT NULL
                        AND timestamp > NOW() - INTERVAL '2 hours'
                        GROUP BY action, error
                        ORDER BY cnt DESC
                        LIMIT 5
                    """)
                )
                for row in result.fetchall():
                    state["recent_errors"].append({
                        "action": row[0],
                        "error": str(row[1])[:100],
                        "count": row[2],
                    })

                # Goal failures
                result2 = await session.execute(
                    text("""
                        SELECT action, COUNT(*) as cnt
                        FROM audit_log
                        WHERE event_type LIKE 'goal.%'
                        AND error IS NOT NULL
                        AND timestamp > NOW() - INTERVAL '6 hours'
                        GROUP BY action
                        HAVING COUNT(*) >= 2
                        ORDER BY cnt DESC
                        LIMIT 3
                    """)
                )
                for row in result2.fetchall():
                    state["goal_failures"].append({"action": row[0], "count": row[1]})
        except Exception:
            pass

        return state

    def _check_critical(self, world_state: dict) -> dict | None:
        """Check for immediately critical situations without needing LLM."""
        sys = world_state.get("system", {})

        if sys.get("disk_percent", 0) > 95:
            return {
                "type": "disk_critical",
                "message": (
                    f"ALERTA: Disco al {sys['disk_percent']:.0f}% de capacidad "
                    f"({sys.get('disk_free_gb', '?')}GB libre). "
                    "Considera limpiar archivos grandes o logs antiguos."
                ),
            }

        if sys.get("memory_percent", 0) > 95:
            return {
                "type": "memory_critical",
                "message": f"ALERTA: Memoria RAM al {sys['memory_percent']:.0f}%. El sistema puede volverse inestable.",
            }

        # Repeated task failures
        for task in world_state.get("tasks", []):
            if task.get("failure_count", 0) >= GOAL_FAILURE_THRESHOLD:
                return {
                    "type": "task_failing",
                    "message": f"La tarea '{task['name']}' ha fallado {task['failure_count']} veces consecutivas. Puede necesitar revisión.",
                }

        return None

    async def _evaluate_with_llm(self, world_state: dict) -> str | None:
        """Ask LLM to evaluate the world state and decide on proactive action."""
        sys = world_state.get("system", {})
        tasks = world_state.get("tasks", [])
        errors = world_state.get("recent_errors", [])
        inactive_h = world_state.get("user_inactive_hours", 0)

        # Skip if nothing interesting
        disk_ok = sys.get("disk_percent", 0) < DISK_ALERT_THRESHOLD
        mem_ok = sys.get("memory_percent", 0) < MEMORY_ALERT_THRESHOLD
        no_errors = len(errors) == 0
        not_too_inactive = inactive_h < 12

        if disk_ok and mem_ok and no_errors and not_too_inactive:
            # Nothing obviously wrong — skip LLM call to save tokens
            return None

        # Pull most recent dream reflection so dream insights influence proactive
        # action choices (closes the dream→autonomous loop with no new system).
        dream_hint = ""
        try:
            from sqlalchemy import select
            from ..db.models import DreamLog
            async with async_session() as _ds:
                _row = (await _ds.execute(
                    select(DreamLog.reflection, DreamLog.improvements_json)
                    .order_by(DreamLog.created_at.desc())
                    .limit(1)
                )).first()
                if _row:
                    _refl = (_row[0] or "")[:300]
                    _imp = _row[1] or []
                    if isinstance(_imp, list) and _imp:
                        _top_imp = "; ".join(str(i)[:100] for i in _imp[:2])
                        dream_hint = f"\nLatest nightly reflection: {_refl}\nProposed improvements: {_top_imp}"
                    elif _refl:
                        dream_hint = f"\nLatest nightly reflection: {_refl}"
        except Exception:
            pass

        world_summary = f"""Current system state:
- Disk: {sys.get('disk_percent', '?')}% used ({sys.get('disk_free_gb', '?')}GB free)
- RAM: {sys.get('memory_percent', '?')}% used
- CPU: {sys.get('cpu_percent', '?')}%
- User inactive: {inactive_h}h
- Active tasks: {len(tasks)} ({', '.join(t['name'] for t in tasks[:3]) or 'none'})
- Recent errors (2h): {len(errors)} ({'; '.join(e['action'] for e in errors[:3]) or 'none'}){dream_hint}"""

        prompt = f"""{world_summary}

Is there anything you should do proactively NOW to help the user or keep the system healthy?

Respond ONLY if there is something concrete and useful to do. If everything is fine, respond exactly: NO_ACTION

If something useful is warranted, respond with a short message (2-3 sentences, in the user's language — default English) that you would send to the user explaining what you observed and what you could do. Be concrete.
Do NOT mention that you are "autonomous" — just describe the situation and the action."""

        try:
            messages = [
                Message(role="system", content="You are a proactive AI agent monitoring system health."),
                Message(role="user", content=prompt),
            ]
            response = await self.model_manager.generate(ModelRequest(messages=messages))
            content = response.content.strip()

            if "NO_ACTION" in content or len(content) < 20:
                return None

            return content
        except Exception:
            logger.exception("autonomous.llm_error")
            return None

    async def _notify(self, message: str) -> None:
        """Send a proactive notification to the user via Telegram."""
        if not self.notify_chat_id or not self.bus:
            return
        try:
            await safe_notify(
                self.bus,
                str(self.notify_chat_id),
                f"[Proactive action]\n{message}",
                source="autonomous_goal_generator",
            )
        except Exception:
            logger.exception("autonomous.notify_error")

    async def _update_state(self, state: dict, today: str, now: float) -> None:
        """Update the autonomous state in Redis."""
        state["last_goal_at"] = now
        state["last_goal_date"] = today
        # Use the already-reset counter from state (reset was persisted in __call__ on day rollover)
        state["goals_today"] = state.get("goals_today", 0) + 1
        logger.info(
            "autonomous.state_updated",
            goals_today=state["goals_today"],
            max=MAX_GOALS_PER_DAY,
            date=today,
        )
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await r.set(AUTONOMOUS_STATE_KEY, json.dumps(state))
        finally:
            await r.aclose()

    async def _persist_state_only(self, state: dict) -> None:
        """Persist state without bumping goals_today (used when skipping)."""
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await r.set(AUTONOMOUS_STATE_KEY, json.dumps(state))
        finally:
            await r.aclose()

    # ── Action signature dedup helpers ────────────────────────────────────────

    async def _lookup_signature(self, signature: str) -> dict | None:
        """Return prior outcome for an action signature, or None if unseen."""
        if not signature:
            return None
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            raw = await r.get(f"{ACTION_SIGNATURE_KEY_PREFIX}{signature}")
        finally:
            await r.aclose()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def _remember_goal_signature(self, goal_id: str, signature: str) -> None:
        """Map a freshly-created goal_id to its action signature for later sweep."""
        if not goal_id or not signature:
            return
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await r.setex(
                f"{GOAL_SIGNATURE_KEY_PREFIX}{goal_id}",
                ACTION_DEDUP_TTL,
                signature,
            )
        finally:
            await r.aclose()

    async def _record_signature_outcome(
        self,
        signature: str,
        status: str,
        reason: str = "",
    ) -> None:
        """Persist outcome (failed/success) for a signature with 24h TTL."""
        if not signature:
            return
        payload = {
            "status": status,
            "ts": time.time(),
            "reason": (reason or "")[:200],
        }
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await r.setex(
                f"{ACTION_SIGNATURE_KEY_PREFIX}{signature}",
                ACTION_DEDUP_TTL,
                json.dumps(payload),
            )
        finally:
            await r.aclose()

    async def _sweep_pending_goal_outcomes(self) -> None:
        """Walk pending goal→signature mappings and update the signature cache.

        For each tracked goal_id:
          - If goal is in COMPLETED state → record signature as 'success'.
          - If goal is in FAILED state → inspect failure reason; record as
            'failed' only when the reason is persistent (sandbox / restricted /
            invalid path / non-zero exit). Transient errors leave no mark so
            the action can be retried later.
          - If goal still in flight → leave mapping in place.
          - If goal cannot be loaded (deleted) → drop the mapping.
        """
        if not self.goal_orchestrator:
            return

        # Need the same Redis instance the orchestrator uses to load goals.
        from ..goal_orchestrator.store import load_goal as _load_goal
        from ..goal_orchestrator.types import GoalState as _GS

        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            cursor = 0
            scanned = 0
            recorded_failed = 0
            recorded_success = 0
            dropped_unknown = 0
            kept_inflight = 0
            while True:
                cursor, keys = await r.scan(
                    cursor=cursor,
                    match=f"{GOAL_SIGNATURE_KEY_PREFIX}*",
                    count=200,
                )
                for key in keys:
                    scanned += 1
                    goal_id = key[len(GOAL_SIGNATURE_KEY_PREFIX):]
                    signature = await r.get(key)
                    if not signature:
                        continue
                    goal = await _load_goal(r, goal_id)
                    if goal is None:
                        # Goal vanished — drop mapping, leave signature untouched.
                        await r.delete(key)
                        dropped_unknown += 1
                        continue
                    if goal.state == _GS.COMPLETED:
                        await self._record_signature_outcome(signature, "success")
                        await r.delete(key)
                        recorded_success += 1
                    elif goal.state == _GS.FAILED:
                        # Extract a failure reason from the goal's stability /
                        # task graph.  Fall back to last task error.
                        reason = ""
                        try:
                            stab = getattr(goal, "stability", None)
                            if stab is not None:
                                reason = getattr(stab, "last_intervention", "") or ""
                            if not reason and getattr(goal, "task_graph", None):
                                for task in goal.task_graph.tasks:
                                    if getattr(task, "error", None):
                                        reason = str(task.error)
                                        break
                        except Exception:
                            reason = ""
                        if _is_transient_failure(reason):
                            # Transient: leave signature unmarked so it can be
                            # retried later.  Drop mapping.
                            await r.delete(key)
                            kept_inflight += 1
                            logger.info(
                                "autonomous.failure_classified_transient",
                                goal_id=goal_id[:8],
                                signature=signature,
                                reason=reason[:120],
                            )
                        else:
                            # Persistent (or unknown but non-transient) — dedupe.
                            await self._record_signature_outcome(
                                signature, "failed", reason=reason
                            )
                            await r.delete(key)
                            recorded_failed += 1
                            logger.info(
                                "autonomous.signature_recorded_failed",
                                goal_id=goal_id[:8],
                                signature=signature,
                                reason=reason[:120],
                                persistent_match=_is_persistent_failure(reason),
                            )
                    else:
                        kept_inflight += 1
                if cursor == 0:
                    break
            if scanned:
                logger.info(
                    "autonomous.sweep_complete",
                    scanned=scanned,
                    recorded_failed=recorded_failed,
                    recorded_success=recorded_success,
                    dropped_unknown=dropped_unknown,
                    kept_inflight=kept_inflight,
                )
        finally:
            await r.aclose()
