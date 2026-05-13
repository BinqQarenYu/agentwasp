"""Anticipatory Simulation Engine — think before acting on risky operations.

Before executing PRIVILEGED or RESTRICTED skills, this engine uses the LLM
to mentally simulate the consequences. The simulation result is annotated
onto the skill output so the agent can self-correct if the simulation reveals
unexpected risks.

Simulations are cached in Redis (5 min) to avoid repeated LLM calls for
identical operations.
"""
from __future__ import annotations

import hashlib
import json

import structlog

logger = structlog.get_logger()

SIMULATION_CACHE_TTL = 300  # 5 minutes

# Skills that always require anticipatory simulation
_ALWAYS_SIMULATE = {"self_improve", "shell", "python_exec"}

# Argument patterns that escalate to simulation even for lower-capability skills
_DESTRUCTIVE_PATTERNS = [
    "rm ", "remove", "delete", "drop ", "format", "truncate", "destroy",
    "chmod", "chown", "sudo", "kill", "pkill", "reboot", "shutdown",
    "overwrite", "wipe", "--force", "-rf", "rmdir",
]


def _needs_simulation(skill_name: str, arguments: dict) -> bool:
    """Determine if a skill call should be simulated before execution."""
    if skill_name in _ALWAYS_SIMULATE:
        return True
    # Check arguments for destructive patterns
    args_str = " ".join(str(v) for v in arguments.values()).lower()
    return any(p in args_str for p in _DESTRUCTIVE_PATTERNS)


def _cache_key(skill_name: str, arguments: dict) -> str:
    """Generate a stable cache key for a skill+args combination."""
    canonical = json.dumps({"skill": skill_name, "args": arguments}, sort_keys=True)
    return f"sim:cache:{hashlib.md5(canonical.encode()).hexdigest()}"


async def simulate(
    skill_name: str,
    arguments: dict,
    model_manager=None,
    redis_url: str = "",
) -> str | None:
    """
    Simulate the execution of a skill call and return a prediction string.
    Returns None if simulation is skipped or fails.
    The returned string is appended to the skill output for LLM visibility.
    """
    if not _needs_simulation(skill_name, arguments):
        return None

    if model_manager is None:
        return None

    # Sanitize: remove internal keys before showing to LLM
    display_args = {k: v for k, v in arguments.items() if k not in ("chat_id", "user_id")}

    # Check cache
    cache_key = _cache_key(skill_name, display_args)
    if redis_url:
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(redis_url, decode_responses=True)
            try:
                cached = await r.get(cache_key)
                if cached:
                    logger.debug("anticipatory.cache_hit", skill=skill_name)
                    return cached
            finally:
                await r.aclose()
        except Exception:
            pass

    # Build simulation prompt
    args_display = json.dumps(display_args, ensure_ascii=False)[:600]
    prompt = f"""You are a pre-execution safety simulator. Analyze this agent action BEFORE it runs.

SKILL: {skill_name}
ARGUMENTS: {args_display}

Predict the consequences in 2-3 sentences. Be specific about:
1. What will actually happen (concrete effects)
2. Is it reversible? (yes/partially/no)
3. Any risk of data loss, side effects, or unintended consequences

Keep response under 100 words. Be direct and factual."""

    try:
        from ..models.types import Message, ModelRequest
        request = ModelRequest(messages=[
            Message(role="system", content="You are a pre-execution simulator. Respond concisely about what a skill call will do."),
            Message(role="user", content=prompt),
        ])
        response = await model_manager.generate(request)
        simulation = response.content.strip()
        if len(simulation) > 500:
            simulation = simulation[:500] + "..."

        # Cache the result
        if redis_url:
            try:
                import redis.asyncio as aioredis
                r = aioredis.from_url(redis_url, decode_responses=True)
                try:
                    await r.setex(cache_key, SIMULATION_CACHE_TTL, simulation)
                finally:
                    await r.aclose()
            except Exception:
                pass

        logger.info("anticipatory.simulated", skill=skill_name, preview=simulation[:60])
        return simulation
    except Exception as e:
        logger.debug("anticipatory.failed", skill=skill_name, error=str(e))
        return None
