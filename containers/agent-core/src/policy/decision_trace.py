"""Decision trace — one structured "why-did-this-happen" record per request.

Lightweight, on purpose. Does NOT capture chain-of-thought or sensitive
content (no full prompts, no full skill outputs). Captures:

  • request_id          — uuid (so dashboard can list traces)
  • path                — telegram | dashboard | scheduled_task | goal_executor
  • request_tier        — simple | normal | complex
  • detected_intent     — explicit | context_allowed | inferred_blocked | non_side_effect
  • allowed_skills      — list[str] (skill names that ran)
  • blocked_skills      — list[{skill, reason}]
  • guard_actions       — list[str] (e.g. "schedule_honesty:strip", "side_effect_text:email")
  • language            — detected user language code
  • timestamps          — start_ts, end_ts, latency_ms
  • notes               — free-form short hints for operator reading

Storage: Redis key `decision_trace:{request_id}` with TTL 7d, JSON-encoded.
A small index list `decision_trace:index` holds the most recent 200 IDs.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class DecisionTrace:
    request_id: str
    path: str = ""                    # "telegram" | "dashboard" | "goal_executor" | "scheduled_task"
    chat_id: str = ""
    user_text_hash: str = ""          # sha1[:12] of user text (no raw content)
    request_tier: str = "normal"
    detected_language: str = "en"
    detected_intent: str = ""         # primary intent label of the turn
    allowed_skills: list = field(default_factory=list)        # ["browser", "task_manager"]
    blocked_skills: list = field(default_factory=list)        # [{"skill": "...", "reason": "..."}]
    guard_actions: list = field(default_factory=list)         # ["schedule_honesty:strip", ...]
    notes: list = field(default_factory=list)
    start_ts: float = field(default_factory=time.time)
    end_ts: float = 0.0
    latency_ms: int = 0

    def add_blocked(self, skill: str, reason: str) -> None:
        self.blocked_skills.append({"skill": skill, "reason": reason})

    def add_guard(self, action: str) -> None:
        self.guard_actions.append(action)

    def add_note(self, note: str) -> None:
        # Short notes only — keep traces lightweight
        if note and len(note) < 200:
            self.notes.append(note)

    def attach_response_guard(self, guard_trace: dict) -> None:
        """Merge the response_guard trace dict into guard_actions list."""
        sched = (guard_trace or {}).get("schedule_honesty") or {}
        if sched.get("applied"):
            self.add_guard(
                f"schedule_honesty:strip[{sched.get('claimed_time','?')}]"
                + (":real_create" if sched.get("had_real_create") else ":no_create")
            )
        sideeff = (guard_trace or {}).get("side_effect_text") or {}
        for r in sideeff.get("rewrites") or []:
            self.add_guard(f"side_effect_text:{r}")
        lang = (guard_trace or {}).get("language_consistency") or {}
        if lang.get("applied"):
            self.add_guard(f"language_consistency:swaps={lang.get('swaps', 0)}")
        ann = (guard_trace or {}).get("action_announcer") or {}
        for fam in ann.get("stripped_families") or []:
            self.add_guard(f"action_announcer:strip[{fam}]")
        for fam in ann.get("scrubbed_failures") or []:
            self.add_guard(f"action_announcer:scrub_failure[{fam}]")
        for fam in ann.get("actions_rendered") or []:
            self.add_guard(f"action_announcer:render[{fam}]")

    def finalize(self) -> dict:
        self.end_ts = time.time()
        self.latency_ms = int((self.end_ts - self.start_ts) * 1000)
        return asdict(self)


def new_trace(
    *,
    path: str,
    chat_id: str = "",
    user_text: str = "",
    request_tier: str = "normal",
    detected_language: str = "en",
) -> DecisionTrace:
    """Create a fresh DecisionTrace for a request. Caller stamps fields then
    calls record_trace() at the end."""
    import hashlib
    text_hash = ""
    if user_text:
        text_hash = hashlib.sha1(
            user_text.strip().encode("utf-8", errors="ignore")
        ).hexdigest()[:12]
    return DecisionTrace(
        request_id=uuid.uuid4().hex,
        path=path,
        chat_id=str(chat_id or ""),
        user_text_hash=text_hash,
        request_tier=request_tier,
        detected_language=detected_language,
    )


async def record_trace(redis_url: Optional[str], trace: DecisionTrace) -> None:
    """Persist a finalized trace to Redis. Fail-safe: errors are swallowed."""
    if not redis_url or not trace:
        return
    try:
        import redis.asyncio as aioredis
        payload = trace.finalize()
        body = json.dumps(payload, ensure_ascii=False)
        r = aioredis.from_url(redis_url, decode_responses=True, socket_connect_timeout=1)
        try:
            await r.set(f"decision_trace:{trace.request_id}", body, ex=7 * 86400)
            await r.lpush("decision_trace:index", trace.request_id)
            await r.ltrim("decision_trace:index", 0, 199)
        finally:
            await r.aclose()
    except Exception:
        # Never let trace writes break a real response.
        pass


# ── One-line entry-point integration ────────────────────────────────────


from contextlib import asynccontextmanager


@asynccontextmanager
async def with_trace(
    redis_url: Optional[str],
    *,
    path: str,
    chat_id: str = "",
    user_text: str = "",
    request_tier: str = "normal",
    detected_language: str = "en",
):
    """Async context manager that creates, yields, and persists a trace.

    Usage::

        async with with_trace(self.redis_url, path="autonomous",
                              chat_id="default") as trace:
            trace.add_note("evaluating world state")
            # ... do work ...
            trace.add_guard("autonomous:goal_created")

    Guarantees the trace is recorded even if the body raises — the
    exception propagates after recording.
    """
    trace = new_trace(
        path=path,
        chat_id=chat_id,
        user_text=user_text,
        request_tier=request_tier,
        detected_language=detected_language,
    )
    try:
        yield trace
    finally:
        # Fire-and-forget — caller's loop owns the lifecycle.
        try:
            await record_trace(redis_url, trace)
        except Exception:
            pass
