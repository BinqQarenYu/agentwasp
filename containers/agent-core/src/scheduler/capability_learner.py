from __future__ import annotations
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()

MIN_OCCURRENCES = 3
MIN_SUCCESS_RATE = 0.90
_ALWAYS_SKIP_KEYS = frozenset({"chat_id", "user_id", "execution_id"})
_NUMERIC_RE = re.compile(r"^\-?\d+\.?\d*$")
# Args that should reference the PREVIOUS step's output (piped input)
_PIPELINE_INPUT_ARGS = frozenset({"json_text", "body", "content", "html", "text", "input", "source"})
_STOPWORDS = frozenset({
    "the", "and", "for", "are", "was", "were", "has", "have", "had", "this",
    "que", "con", "una", "las", "los", "por", "del", "para", "como", "esta",
    "esto", "este", "mes", "hay", "dia", "tarea", "programada", "ejecuta",
    "ahora", "now", "please", "can", "you", "me", "give", "get", "make",
})
_WORD_RE = re.compile(r"[a-záéíóúüñA-ZÁÉÍÓÚÜÑ]{3,}")


class CapabilityLearnerJob:
    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url

    async def __call__(self) -> str:
        try:
            from ..observability.tracer import load_recent_traces
            traces = await load_recent_traces(self._redis_url, limit=500)
            if not traces:
                return "CapabilityLearner: no traces yet"

            seq_buckets: dict[str, list[dict]] = defaultdict(list)
            for trace in traces:
                spans = trace.get("spans", [])
                if not spans:
                    continue
                seq_key = "\u2192".join(s["skill"] for s in spans)
                seq_buckets[seq_key].append(trace)

            promoted = 0
            for seq_key, bucket in seq_buckets.items():
                if len(bucket) < MIN_OCCURRENCES:
                    continue
                success_traces = [t for t in bucket if t.get("status") == "complete"]
                if len(success_traces) / len(bucket) < MIN_SUCCESS_RATE:
                    continue

                steps = self._extract_steps(success_traces)
                skill_names = list({s["skill"] for s in steps})
                cap_name = "_".join(skill_names[:4])

                # Combine skill names with semantic keywords from user messages
                user_kw = self._extract_user_keywords(success_traces)
                keywords = list(dict.fromkeys(skill_names + user_kw))  # deduplicated, ordered

                await self._upsert(cap_name, keywords, steps,
                                   len(success_traces), len(bucket) - len(success_traces))
                promoted += 1

            return f"CapabilityLearner: {len(traces)} traces, {promoted} capabilities promoted"
        except Exception as exc:
            logger.warning("capability_learner.error", error=str(exc)[:120])
            return f"CapabilityLearner: error — {str(exc)[:80]}"

    def _extract_steps(self, success_traces: list[dict]) -> list[dict]:
        if not success_traces:
            return []
        sample = success_traces[-1].get("spans", [])
        n = len(sample)
        per_step: list[dict[str, list[str]]] = [defaultdict(list) for _ in range(n)]
        for trace in success_traces:
            for i, span in enumerate(trace.get("spans", [])):
                if i >= n:
                    break
                for k, v in (span.get("args") or {}).items():
                    per_step[i][k].append(str(v))

        steps = []
        for i, span in enumerate(sample):
            args: dict[str, str] = {}
            for arg_key, values in per_step[i].items():
                if arg_key in _ALWAYS_SKIP_KEYS:
                    continue
                non_empty = [v for v in values if v and v != "None"]
                if not non_empty:
                    continue
                unique = set(non_empty)
                if len(unique) == 1:
                    args[arg_key] = non_empty[0]
                else:
                    # Variable value — store as template placeholder.
                    # "Pipeline input" args (json_text, body, etc.) reference the previous
                    # step's output: {{step_i_output}} where i = current 0-index (=1-indexed prev).
                    # Other variable args (extracted fields like btc_price) are stored in
                    # ExecutionContext by extract_fields skill directly under their name.
                    if arg_key in _PIPELINE_INPUT_ARGS:
                        args[arg_key] = f"{{{{step_{i}_output}}}}"
                    else:
                        args[arg_key] = f"{{{{{arg_key}}}}}"
            steps.append({
                "skill": span["skill"],
                "arg_hash": span.get("arg_hash", ""),
                "args": args,
            })
        return steps

    def _extract_user_keywords(self, traces: list[dict]) -> list[str]:
        """Extract semantic trigger words from user_text stored in each trace.

        For scheduled tasks, user_text contains "[TAREA PROGRAMADA: Task Name] ..."
        — we extract the task name portion for maximum relevance.
        """
        word_freq: dict[str, int] = defaultdict(int)
        for trace in traces:
            raw = trace.get("user_text", "")
            if not raw:
                continue
            # For scheduled tasks extract just the task-name portion
            m = re.search(r"\[TAREA PROGRAMADA:\s*([^\]]+)\]", raw, re.IGNORECASE)
            text_to_scan = m.group(1) if m else raw
            for word in _WORD_RE.findall(text_to_scan):
                w = word.lower()
                if w not in _STOPWORDS and not _NUMERIC_RE.match(w):
                    word_freq[w] += 1

        # Keep words that appear in at least half the traces (min 1), top 10
        threshold = max(1, len(traces) // 2)
        candidates = [(w, c) for w, c in word_freq.items() if c >= threshold]
        candidates.sort(key=lambda x: -x[1])
        return [w for w, _ in candidates[:10]]

    async def _upsert(self, name, keywords, steps, success_count, failure_count):
        now = time.time()
        payload = {
            "name": name, "trigger_keywords": keywords, "steps": steps,
            "success_count": success_count, "failure_count": failure_count,
            "last_updated": now, "last_used": now, "created_at": now,
        }
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            existing_raw = await r.get(f"capability:{name}")
            if existing_raw:
                existing = json.loads(existing_raw)
                payload["success_count"] += existing.get("success_count", 0)
                payload["failure_count"] += existing.get("failure_count", 0)
                payload["created_at"] = existing.get("created_at", now)
                payload["last_used"] = existing.get("last_used", now)
            await r.set(f"capability:{name}", json.dumps(payload), ex=86400 * 30)
            if keywords:
                await r.hset("capability:index", mapping={kw: name for kw in keywords})
        finally:
            await r.aclose()

        try:
            from ..db.session import async_session
            from ..db.models import Capability
            from sqlalchemy import select
            async with async_session() as session:
                result = await session.execute(select(Capability).where(Capability.name == name))
                cap = result.scalar_one_or_none()
                if cap:
                    cap.success_count = payload["success_count"]
                    cap.failure_count = payload["failure_count"]
                    cap.steps = steps
                    cap.trigger_keywords = keywords
                    cap.last_updated = datetime.now(timezone.utc)
                else:
                    cap = Capability(id=str(uuid4()), name=name, trigger_keywords=keywords,
                                     steps=steps, success_count=success_count, failure_count=failure_count,
                                     source_trace_ids=[], created_at=datetime.now(timezone.utc),
                                     last_updated=datetime.now(timezone.utc))
                    session.add(cap)
                await session.commit()
        except Exception as pg_exc:
            logger.debug("capability_learner.postgres_failed", error=str(pg_exc)[:100])
