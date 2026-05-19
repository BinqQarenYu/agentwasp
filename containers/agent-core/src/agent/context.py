import os

import structlog

from ..memory.manager import MemoryManager
from ..memory.types import MemoryQuery, MemoryType
from ..models.types import Message
from ..skills.openclaw.loader import load_installed_skills
from sqlalchemy.ext.asyncio import AsyncSession

from .constants import (
    IDENTITY_FEWSHOT,
    IDENTITY_POISON,
    MODEL_CREATORS,
    PROVIDER_LABELS,
    SKILL_FEWSHOT,
    SKILL_POISON,
    SYSTEM_PROMPT,
    _SOVEREIGN_BLOCK_TEMPLATE,
)

logger = structlog.get_logger()


def _get_creator(model_name: str) -> str:
    """Look up the creator of a model by its name prefix."""
    name = model_name.lower().split(":")[0]  # "qwen2.5:1.5b" -> "qwen2.5"
    # Try longest prefix match first
    for prefix, creator in sorted(MODEL_CREATORS.items(), key=lambda x: -len(x[0])):
        if name.startswith(prefix):
            return creator
    return "unknown"


def _load_prime_md() -> str:
    """Load /data/config/prime.md if it exists. Returns empty string if not found."""
    import os
    prime_path = "/data/config/prime.md"
    try:
        if os.path.isfile(prime_path):
            with open(prime_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            return content
    except Exception:
        pass
    return ""


def _wasp_host_dir() -> str:
    """Resolve the host install path for self-repair commands shown to the LLM.

    Defaults to /opt/wasp (public installer default). Operators with a custom
    install location set WASP_HOST_DIR in their .env (see installer / wasp CLI).
    """
    return os.environ.get("WASP_HOST_DIR", "/opt/wasp")


def _adaptive_history_limit(user_text: str) -> int:
    """Return how many episodic turns to inject based on query complexity."""
    n = len(user_text)
    if n < 40:
        return 3   # Very short greeting / quick question
    if n < 150:
        return 5   # Normal query
    return 8       # Long / multi-part query


def _detect_response_type(user_text: str) -> str:
    """Classify the expected response structure from the user's message.

    Returns one of: "list", "comparison", "multipart", "explanation", "action", "chat"
    """
    import re as _re
    t = user_text.lower()

    # Comparison: X vs Y, diferencia entre, compare
    if _re.search(
        r"\b(?:vs\.?|versus|compara(?:r)?|diferencia\s+entre|cual\s+es\s+mejor|"
        r"compare|difference\s+between|which\s+is\s+better)\b", t
    ):
        return "comparison"

    # Multi-part: multiple question marks, enumerated asks, "and also", "además"
    question_count = t.count("?")
    has_enum = bool(_re.search(
        r"(?:^|\W)(?:1[.\)]\s|\(1\)|primero[,\s]|first[,\s])",
        t, _re.MULTILINE
    ))
    has_also = bool(_re.search(
        r"\b(?:adem[aá]s|tambi[eé]n|y\s+(?:tambi[eé]n|adem[aá]s)|"
        r"also|and\s+also|furthermore|por\s+otro\s+lado)\b", t
    ))
    if question_count >= 2 or has_enum or (question_count >= 1 and has_also):
        return "multipart"

    # List: "list", "enumera", "cuáles son", "dame N"
    if _re.search(
        r"\b(?:lista(?:r)?|enumera(?:r)?|cu[aá]les\s+son|dame\s+(?:los|las|una\s+lista)|"
        r"list\s+(?:the|all)|what\s+are\s+(?:the|all)|show\s+me\s+all)\b", t
    ):
        return "list"

    # Action: skill-triggering requests
    if _re.search(
        r"\b(?:env[ií]a|send|programa(?:r)?|crea(?:r)?|ejecuta(?:r)?|captura(?:r)?|"
        r"busca(?:r)?|monitorea(?:r)?|schedule|create|execute|fetch|search)\b", t
    ):
        return "action"

    # Explanation: "explain", "how does", "what is", "por qué"
    if _re.search(
        r"\b(?:expl[ií]ca(?:me)?|c[oó]mo\s+funciona|qu[eé]\s+es|por\s+qu[eé]|"
        r"explain|how\s+does|what\s+is|why\s+(?:does|is|do))\b", t
    ):
        return "explanation"

    return "chat"


def _build_structure_rule(response_type: str) -> str:
    """Return a type-specific structure rule for the cognitive control block."""
    if response_type == "comparison":
        return (
            "6. STRUCTURE (COMPARISON): Present a clear side-by-side comparison. "
            "Use a table or aligned columns. Cover: features, differences, recommendation. "
            "Never collapse both items into one paragraph without clear separation."
        )
    if response_type == "multipart":
        return (
            "6. STRUCTURE (MULTI-PART): The user asked multiple distinct questions. "
            "Answer ALL of them — number each answer (1., 2., etc.) or use clear headers. "
            "Do NOT answer only the first question and ignore the rest. "
            "If you cannot answer one part, say so explicitly for that part only."
        )
    if response_type == "list":
        return (
            "6. STRUCTURE (LIST): The user wants an enumerated list. "
            "Use numbered or bulleted format. Do not collapse into prose. "
            "Each item must be on its own line with a clear label."
        )
    if response_type == "explanation":
        return (
            "6. STRUCTURE (EXPLANATION): Start with the core definition/answer (1-2 sentences). "
            "Then elaborate with context, examples, and implications. "
            "Use headers or numbered sections if the explanation has multiple components."
        )
    if response_type == "action":
        return (
            "6. STRUCTURE (ACTION): Confirm what was executed and what the result was. "
            "If multiple steps were required, confirm each one. "
            "Do not give a narrative description — state facts: what ran, what succeeded, what failed."
        )
    return ""  # "chat" — no extra structure rule needed


def _build_cognitive_control_block(user_text: str) -> str:
    """Return a per-request cognitive control block injected at the end of the system prompt.

    Locks the LLM to the current intent and prevents failure modes:
    - hallucination (inventing data not obtained from a skill)
    - drift (answering a different topic than requested)
    - incomplete execution (responding before all steps are done)
    - structural incompleteness (answering only part of a multi-part question)
    """
    if not user_text or len(user_text.strip()) < 10:
        return ""

    import re as _re
    intent = _re.split(r"[.\n;!]", user_text.strip())[0].strip()[:180]
    response_type = _detect_response_type(user_text)
    structure_rule = _build_structure_rule(response_type)

    base = (
        "[COGNITIVE CONTROL — THIS TURN]\n"
        f'Request: "{intent}"\n\n'
        "VERIFY before writing your response:\n"
        "1. DATA INTEGRITY: Every price, statistic, or specific fact in your response came "
        "from a skill result in this conversation. If you have no skill data → do NOT invent "
        "values → say instead (in the user's language): \"I could not obtain that information. Want me to try again?\"\n"
        "2. COMPLETION: You completed EVERY step the user asked for. If any step is pending "
        "(screenshot, email, fetch) → call the skill NOW — do not respond yet.\n"
        "3. TOPIC LOCK: Your response is about the active request above. If you notice you are "
        "about to answer something different → STOP → answer the actual request.\n"
        "4. MEMORY HONESTY: Do not invent previous conversations or results. If you are unsure "
        "what happened before → check with a skill or ask the user.\n"
        "5. PARTIAL IS FAILURE: 60% done = failed. If the user asked for data + screenshot + email, "
        "all three must complete before you send your final response."
    )
    if structure_rule:
        base += f"\n{structure_rule}"
    return base


async def build_context(
    session: AsyncSession,
    memory: MemoryManager,
    user_text: str,
    chat_id: str = "",
    model_name: str = "unknown",
    provider_name: str = "ollama",
    skill_catalog: str = "",
    identity_manager=None,
    redis_url: str = "",
    goal_id: str = "",
    is_light_mode: bool = False,
) -> list[Message]:
    """Build the LLM context from memory and the current message.

    ``is_light_mode`` (or provider_name=="ollama") activates lightweight mode:
    - skips heavy cognitive blocks (KG, epistemic, temporal, procedural, etc.)
    - reduces few-shots to 3 pairs
    - limits episodic history to the last 6 exchanges
    This prevents 7B-class local models from hitting context limits and keeps
    CPU load lower when the system is already under pressure.
    """
    # Lightweight mode: local models or high-load cloud inference
    _lightweight = is_light_mode or (provider_name == "ollama")

    creator = _get_creator(model_name)
    running_on = PROVIDER_LABELS.get(provider_name, f"via {provider_name}")
    host_dir = _wasp_host_dir()
    prompt = SYSTEM_PROMPT.format(
        model_name=model_name,
        creator=creator,
        running_on=running_on,
        wasp_host_dir=host_dir,
    )

    # Sovereign mode block — injected first when SOVEREIGN_MODE=true
    try:
        from ..config import settings as _cfg
        if _cfg.sovereign_mode:
            prompt = _SOVEREIGN_BLOCK_TEMPLATE.format(
                wasp_host_dir=host_dir) + "\n\n---\n\n" + prompt
    except Exception:
        pass

    # prime.md — operator-level direct injection (takes highest priority, after sovereign block)
    prime = _load_prime_md()
    if prime:
        prompt = prime + "\n\n---\n\n" + prompt

    messages = [Message(role="system", content=prompt)]

    # ── Parallel context injection ──────────────────────────────────────
    # All async lookups (policy, KG, self-model, epistemic, temporal, procedural)
    # run concurrently to minimise pre-LLM latency.

    async def _policies():
        try:
            from ..db.session import async_session as _async_session
            async with _async_session() as _pol_sess:
                rows = await memory.retrieve(_pol_sess, MemoryQuery(memory_type=MemoryType.POLICY, limit=1))
            return rows
        except Exception:
            return []

    async def _user_attrs():
        # Phase 5: stable user-declared facts. Authoritative — overrides any
        # episodic latest-mention. Empty string when none declared.
        if not chat_id:
            return ""
        try:
            from ..db.session import async_session as _async_session
            from ..memory.user_attributes import format_for_context as _ua_format
            async with _async_session() as _ua_sess:
                return await _ua_format(_ua_sess, chat_id) or ""
        except Exception as _e:
            logger.warning("context.user_attrs_failed", error=str(_e)[:120])
            return ""

    async def _kg():
        if not redis_url:
            return ""
        try:
            from ..memory.knowledge_graph import format_salient_for_context as kg_format
            # Lightweight mode: keep top-1 entity to preserve identity continuity.
            _max_e = 1 if _lightweight else 3
            return await kg_format(chat_id, intent=user_text, max_entities=_max_e) or ""
        except Exception as _e:
            logger.warning("context.kg_block_failed", error=str(_e)[:120])
            return ""

    async def _self_model():
        if not redis_url:
            return ""
        try:
            from ..agent.self_model import load as sm_load, format_for_context as sm_format
            model = await sm_load(redis_url)
            if _lightweight:
                # Minimum baseline: keep strengths + most-recent failure only.
                # This prevents amnesia under load while saving ~80% of tokens.
                _trim = {
                    "strengths": (model or {}).get("strengths", [])[:2],
                    "known_failures": (model or {}).get("known_failures", [])[-1:],
                    "user_preferences": {},
                    "weekly_stats": {},
                    "skill_success_rates": {},
                    "improvement_queue": [],
                    "total_messages_processed": (model or {}).get("total_messages_processed", 0),
                }
                return sm_format(_trim) or ""
            return sm_format(model) or ""
        except Exception as _e:
            logger.warning("context.self_model_block_failed",
                           error=str(_e)[:120])
            return ""

    async def _epistemic():
        if not redis_url or _lightweight:
            return ""
        try:
            from ..agent.epistemic import load as ep_load, format_for_context as ep_format
            return ep_format(await ep_load(redis_url)) or ""
        except Exception as _e:
            logger.warning("context.epistemic_block_failed",
                           error=str(_e)[:120])
            return ""

    async def _temporal():
        if not redis_url or _lightweight:
            return ""
        try:
            from ..memory.temporal import format_for_context as temporal_format
            return await temporal_format(chat_id, hours=48) or ""
        except Exception as _e:
            logger.warning("context.temporal_block_failed",
                           error=str(_e)[:120])
            return ""

    async def _procedural():
        if not redis_url or _lightweight:
            return ""
        try:
            from ..config import settings as _cfg
            from ..memory.procedural import find_procedures, format_procedures_for_context, record_use
            from ..memory.ranking import rank_and_cap
            _proc_limit = getattr(_cfg, "memory_procedural_max", 3)
            _hl = getattr(_cfg, "memory_recency_half_life_hours", 24.0)
            procs = await find_procedures(user_text, limit=_proc_limit * 2)
            procs = rank_and_cap(procs, limit=_proc_limit, half_life_hours=_hl,
                                 memory_type="procedural", goal_id=goal_id)
            # Record that these procedures were injected (optimistic success)
            import asyncio as _proc_asyncio
            for p in procs:
                try:
                    _proc_asyncio.get_running_loop().create_task(
                        record_use(p["id"], success=True))
                except RuntimeError:
                    pass
            return format_procedures_for_context(procs) or ""
        except Exception as _e:
            logger.warning("context.procedural_block_failed",
                           error=str(_e)[:120])
            return ""

    async def _behavioral_rules():
        try:
            from ..memory.behavioral import get_active_rules, format_for_context as br_format
            # Cap at 10 rules ordered by use_count DESC — prevents unbounded prompt growth.
            # Full rule set remains in DB; only the most-used rules are injected.
            rules = await get_active_rules(limit=10)
            return br_format(rules) or ""
        except Exception as _e:
            logger.warning("context.behavioral_rules_failed",
                           error=str(_e)[:120])
            return ""

    async def _gmail_status():
        if not redis_url:
            return ""
        try:
            import redis.asyncio as _aioredis
            _r = _aioredis.from_url(redis_url, decode_responses=True)
            try:
                creds = await _r.hgetall("gmail:credentials")
            finally:
                await _r.aclose()
            if creds.get("address"):
                return f"[Gmail: CONNECTED as {creds['address']} — inbox, send, search, delete available]"
            return ""
        except Exception as _e:
            logger.warning("context.gmail_status_block_failed",
                           error=str(_e)[:120])
            return ""

    _hist_limit = min(6, _adaptive_history_limit(user_text)
                      ) if _lightweight else _adaptive_history_limit(user_text)

    async def _episodic():
        try:
            from ..db.session import async_session as _async_session
            # Chat-scoped retrieval: filter by chat:{chat_id} tag so the
            # episodic block reflects THIS chat's history, not a global feed.
            # No backfill — a fresh chat must return EMPTY rather than load
            # other chats' content (the cross-chat leak that triggered LLM
            # hallucinations like "haz lo mismo" replying with another
            # chat's package status). Pre-fix entries without the tag stay
            # invisible to chat-scoped retrieval; that is correct.
            if not chat_id:
                # No chat_id (e.g. dashboard direct calls) — return empty.
                return []
            _ep_tags = [f"chat:{chat_id}"]
            async with _async_session() as _ep_sess:
                rows = await memory.retrieve(
                    _ep_sess,
                    MemoryQuery(
                        memory_type=MemoryType.EPISODIC,
                        tags=_ep_tags,
                        limit=_hist_limit,
                    ),
                )
            logger.info(
                "memory_retrieved",
                memory_type="episodic",
                retrieval_method="recent",
                count=len(rows),
                chat_scoped=True,
            )
            return rows
        except Exception as _e:
            logger.warning("context.episodic_block_failed",
                           error=str(_e)[:120])
            return []

    # ── System 6: Temporal Reasoning ──────────────────────────────────
    async def _temporal_insights():
        """Episodic Temporal Reasoning — [TEMPORAL INSIGHTS] block."""
        if _lightweight:
            return ""
        try:
            from ..config import settings as _cfg
            if not _cfg.temporal_reasoning_enabled:
                return ""
            from ..reasoning.temporal_reasoner import TemporalReasoner
            from ..db.session import async_session as _async_session
            reasoner = TemporalReasoner(
                max_insights=_cfg.temporal_reasoning_max_insights)
            async with _async_session() as _sess:
                return await reasoner.build_context_block(_sess, hours=24.0) or ""
        except Exception as _e:
            logger.warning(
                "context.temporal_insights_block_failed", error=str(_e)[:120])
            return ""

    # ── System 4: World Model ──────────────────────────────────────────
    async def _world_model():
        """World Model — [WORLD MODEL] entity state block."""
        if _lightweight:
            return ""
        try:
            from ..config import settings as _cfg
            if not _cfg.world_model_enabled:
                return ""
            from ..world.world_model import WorldModel
            from ..db.session import async_session as _async_session
            wm = WorldModel(ollama_url=_cfg.ollama_base_url)
            async with _async_session() as _sess:
                return await wm.format_for_context(_sess, max_entities=5) or ""
        except Exception as _e:
            logger.warning("context.world_model_block_failed",
                           error=str(_e)[:120])
            return ""

    # ── System 1: Vector Semantic Memory ───────────────────────────────
    async def _vector_memory():
        """Semantic memory retrieval for current query.

        Uses its own session to avoid concurrent access on the shared outer session.
        Detects degraded (hash-fallback) mode and injects a single awareness block.
        """
        if _lightweight:
            return ""
        try:
            from ..config import settings as _cfg
            if not _cfg.vector_memory_enabled:
                return ""
            from ..memory.vector_memory import semantic_search, format_for_context as vm_fmt
            from ..memory.ranking import rank_and_cap
            from ..memory.embeddings import create_provider as _make_provider
            from ..db.session import async_session as _async_session
            _sem_limit = getattr(_cfg, "memory_semantic_max", 5)
            _hl = getattr(_cfg, "memory_recency_half_life_hours", 24.0)
            _provider = _make_provider(_cfg)

            # Mode tracking: detect degraded↔semantic transitions via Redis flag
            _degraded_block = ""
            if redis_url:
                try:
                    import redis.asyncio as _aioredis
                    _vr = _aioredis.from_url(redis_url, decode_responses=True)
                    try:
                        _was_degraded = await _vr.get("vector_memory:degraded_warned")
                        if not _provider.is_semantic:
                            # Still degraded — warn once per 24h
                            if not _was_degraded:
                                await _vr.setex("vector_memory:degraded_warned", 86400, "1")
                                logger.warning(
                                    "vector_memory.degraded_no_embed_model",
                                    provider=_provider.model_name,
                                    fix="ollama pull nomic-embed-text",
                                )
                            _degraded_block = (
                                "[VECTOR MEMORY: FALLBACK MODE — embedding model unavailable. "
                                "Memory search uses keyword hashing only. You may miss relevant past context.]"
                            )
                        elif _was_degraded:
                            # Transition: fallback → semantic — clear flag, log recovery
                            await _vr.delete("vector_memory:degraded_warned")
                            logger.info(
                                "vector_memory.restored_to_semantic_mode",
                                provider=_provider.model_name,
                            )
                    finally:
                        await _vr.aclose()
                except Exception:
                    pass

            async with _async_session() as _vm_sess:
                results = await semantic_search(
                    session=_vm_sess,
                    query=user_text[:1000],
                    provider=_provider,
                    top_k=_sem_limit * 2,
                )
            if results:
                results = rank_and_cap(
                    results, limit=_sem_limit, half_life_hours=_hl,
                    memory_type="semantic", goal_id=goal_id,
                )
                logger.info(
                    "memory_retrieved",
                    memory_type="semantic",
                    retrieval_method="vector",
                    count=len(results),
                )
            content = vm_fmt(results) or ""
            return (_degraded_block + "\n\n" + content).strip() if _degraded_block else content
        except Exception as _e:
            logger.warning("context.vector_memory_block_failed",
                           error=str(_e)[:120])
            return ""

    async def _visual_memory():
        """Inject references to recent relevant screenshots into context."""
        if _lightweight or not chat_id:
            return ""
        try:
            from ..memory.visual import search_screenshots, format_visual_context
            _kw = " ".join(user_text.split()[:6]) if user_text else ""
            entries = await search_screenshots(keyword=_kw, chat_id=chat_id, limit=3)
            if not entries:
                entries = await search_screenshots(chat_id=chat_id, limit=2)
            return format_visual_context(entries) or ""
        except Exception as _e:
            logger.warning("context.visual_memory_block_failed",
                           error=str(_e)[:120])
            return ""

    async def _digest_block():
        """Inject the latest weekly digest summary so dream-derived narrative
        actually surfaces to the LLM (closes digest→context loop).  Bounded
        at 280 chars so it never dominates the prompt."""
        if _lightweight or not redis_url:
            return ""
        try:
            import redis.asyncio as _aio
            r = _aio.from_url(redis_url, decode_responses=True)
            try:
                raw = await r.get("agent:digest")
            finally:
                await r.aclose()
            if not raw:
                return ""
            import json as _j
            try:
                data = _j.loads(raw)
                _txt = (data.get("text") or "").strip()
            except Exception:
                _txt = raw if isinstance(raw, str) else ""
            if not _txt:
                return ""
            return f"[WEEKLY DIGEST]\n{_txt[:280]}"
        except Exception as _e:
            logger.debug("context.digest_block_failed", error=str(_e)[:80])
            return ""

    # ── Goal-Specific Memory ────────────────────────────────────────────
    async def _goal_memory():
        """Retrieve observations scoped to the current active goal."""
        if not goal_id or _lightweight:
            return ""
        try:
            from ..config import settings as _cfg
            from ..memory.goal_memory import get_observations, format_for_context as gm_fmt
            _gm_limit = getattr(_cfg, "memory_goal_max", 5)
            obs = await get_observations(goal_id=goal_id, limit=_gm_limit)
            return gm_fmt(obs, goal_id=goal_id) or ""
        except Exception as _e:
            logger.warning("context.goal_memory_block_failed",
                           error=str(_e)[:120])
            return ""

    # ── Self-Reflection Engine ──────────────────────────────────────────
    async def _reflections():
        """Retrieve recent reflections from both goal-level and execution-level sources."""
        if _lightweight:
            return ""
        parts: list[str] = []

        # Layer 1: goal-level reflections (LLM-generated, Redis, existing behaviour)
        try:
            from ..reflection_engine import ReflectionEngine, format_reflections_for_context
            re_engine = ReflectionEngine(
                model_manager=None, redis_url=redis_url)
            if goal_id:
                goal_entries = await re_engine.get_reflections_for_goal(goal_id)
                goal_entries = goal_entries[:2]
            else:
                goal_entries = await re_engine.get_recent_reflections(limit=2)
            blk = format_reflections_for_context(goal_entries)
            if blk:
                parts.append(blk)
        except Exception as _e:
            logger.warning(
                "context.goal_reflections_block_failed", error=str(_e)[:120])

        # Layer 2: execution-level reflections (heuristic, DB-backed)
        # current_intent passed for similarity ranking (Change 4)
        try:
            from ..reflection_engine import (
                get_execution_reflections,
                format_execution_reflections_for_context,
            )
            exec_entries = await get_execution_reflections(
                chat_id=chat_id,
                limit=3,
                current_intent=user_text or "",
            )
            blk2 = format_execution_reflections_for_context(exec_entries)
            if blk2:
                parts.append(blk2)
        except Exception as _e:
            logger.warning(
                "context.exec_reflections_block_failed", error=str(_e)[:120])

        return "\n\n".join(parts) if parts else ""

    import asyncio as _asyncio
    (
        policies_rows,
        kg_block,
        sm_block,
        ep_block,
        tw_block,
        proc_block,
        gmail_block,
        behavioral_block,
        recent,
        temporal_insights_block,
        world_model_block,
        vector_mem_block,
        goal_mem_block,
        reflections_block,
        visual_mem_block,
        digest_block,
        user_attrs_block,
    ) = await _asyncio.gather(
        _policies(), _kg(), _self_model(), _epistemic(), _temporal(), _procedural(),
        _gmail_status(), _behavioral_rules(), _episodic(),
        _temporal_insights(), _world_model(), _vector_memory(), _goal_memory(),
        _reflections(), _visual_memory(), _digest_block(),
        _user_attrs(),
    )

    # Apply policy rules
    if policies_rows:
        rules = policies_rows[0].content.get("rules", [])
        if rules:
            policy_text = "Active policy rules:\n" + \
                "\n".join(f"- {r}" for r in rules)
            messages[0].content += f"\n\n{policy_text}"

    # Inject skill catalog
    if skill_catalog:
        messages[0].content += f"\n\n{skill_catalog}"

    # Inject agent identity directive (only when non-default identity is set)
    if identity_manager is not None:
        try:
            identity_block = identity_manager.format_for_prompt()
            if identity_block:
                messages[0].content += f"\n\n{identity_block}"
        except Exception:
            pass

    # Token efficiency: skip raw temporal observations when temporal_insights is available
    # (temporal_insights is a superset: shows trends + change% + observation counts)
    effective_tw_block = tw_block if not temporal_insights_block else ""
    # Skip world_model_block when temporal_insights_block covers the same entities
    effective_wm_block = world_model_block if not temporal_insights_block else ""

    # Inject all cognitive system blocks (existing + next-gen)
    for blk in (
        # User-declared stable facts (Phase 5) — first so the LLM sees the
        # source of truth before any other cognitive layer can drift.
        user_attrs_block,
        kg_block, sm_block, ep_block, effective_tw_block, proc_block, gmail_block, behavioral_block,
        # Next-gen cognitive systems
        temporal_insights_block, effective_wm_block, vector_mem_block,
        # Visual memory — recent relevant screenshots (only when non-empty)
        visual_mem_block,
        # Goal-scoped memory (only non-empty when goal_id is provided)
        goal_mem_block,
        # Self-reflection insights
        reflections_block,
        # Weekly digest narrative (closes digest→context loop)
        digest_block,
    ):
        if blk:
            messages[0].content += f"\n\n{blk}"

    # Cognitive Control Block — per-request intent lock + hallucination barrier
    # Injected last so it's the freshest constraint when the LLM starts generating.
    _cc_block = _build_cognitive_control_block(user_text)
    if _cc_block:
        messages[0].content += f"\n\n{_cc_block}"

    # Impact tracking — record which cognitive systems were active this turn
    try:
        _active_sources = [
            name for name, blk in [
                ("kg", kg_block), ("self_model", sm_block), ("epistemic", ep_block),
                ("temporal", effective_tw_block or temporal_insights_block),
                ("procedural", proc_block), ("behavioral", behavioral_block),
                ("vector_memory", vector_mem_block), ("goal_memory", goal_mem_block),
                ("reflections", reflections_block),
            ] if blk
        ]
        if _active_sources and redis_url:
            from .impact_tracker import record_impact as _rec_impact
            import asyncio as _imp_asyncio
            _imp_asyncio.ensure_future(
                _rec_impact(
                    redis_url=redis_url,
                    decision_sources=_active_sources,
                    action_taken=user_text[:120] if user_text else "",
                    outcome="unknown",  # Updated to success/failure post-response in handlers
                    chat_id=chat_id or "",
                )
            )
    except Exception:
        pass

    # Inject installed OpenClaw skill instructions
    try:
        oc_skills = load_installed_skills()
        if oc_skills:
            oc_parts = ["\n\nINSTALLED OPENCLAW SKILLS:"]
            total_chars = 0
            for s in oc_skills:
                text = s.prompt_text
                if total_chars + len(text) > 4000:
                    break
                oc_parts.append(text)
                total_chars += len(text)
            messages[0].content += "\n".join(oc_parts)
    except Exception:
        pass  # Don't break context building if OpenClaw loading fails

    # Few-shot identity examples to override small model training bias
    # In lightweight mode skip entirely — saves ~300 tokens
    # All few-shots get meta={"fewshot": True} so policy.intent_gate skips them
    # (otherwise role="user" example text could look like a real user request).
    _FS_META = {"fewshot": True}
    if not _lightweight:
        fmt = {"model_name": model_name,
               "creator": creator, "running_on": running_on}
        for user_q, assistant_a in IDENTITY_FEWSHOT:
            messages.append(
                Message(role="user", content=user_q.format(**fmt), meta=_FS_META))
            messages.append(
                Message(role="assistant", content=assistant_a.format(**fmt), meta=_FS_META))

    # Few-shot skill usage examples to teach the model the pattern
    # Cap at 15 pairs normally; 3 pairs in lightweight mode (saves ~1200 tokens)
    if skill_catalog:
        _fewshot_cap = 3 if _lightweight else 15
        for user_q, assistant_a in SKILL_FEWSHOT[:_fewshot_cap]:
            messages.append(
                Message(role="user", content=user_q, meta=_FS_META))
            messages.append(
                Message(role="assistant", content=assistant_a, meta=_FS_META))

        # Inject learned few-shots from behavioral rules (reuse already-fetched rules)
        if behavioral_block:
            try:
                from ..memory.behavioral import get_active_rules, extract_fewshots
                # Reuse rules already fetched by _behavioral_rules() above (avoid 2nd DB query)
                _br_rules = await get_active_rules(limit=10)
                for uq, aa in extract_fewshots(_br_rules):
                    messages.append(
                        Message(role="user", content=uq, meta=_FS_META))
                    messages.append(
                        Message(role="assistant", content=aa, meta=_FS_META))
            except Exception:
                pass

    # Load recent episodic memories for conversation context
    # Filter out responses with wrong identity/hallucinated content that pollute context

    # Extend SKILL_POISON with dynamically learned patterns from behavioral rules
    dynamic_poison = []
    if behavioral_block:
        try:
            from ..memory.behavioral import get_active_rules, extract_poison_patterns
            _br_rules = await get_active_rules(limit=40)
            dynamic_poison = extract_poison_patterns(_br_rules)
        except Exception:
            pass
    all_poison = SKILL_POISON + dynamic_poison

    for entry in reversed(recent):
        user_input = entry.content.get("user_input", "")
        agent_response = entry.content.get("agent_response", "")
        if user_input and agent_response and agent_response != "(processing)":
            # Skip memories where the model claimed wrong identity or hallucinated
            response_lower = agent_response.lower()
            if any(poison in response_lower for poison in IDENTITY_POISON):
                continue
            if any(poison in response_lower for poison in all_poison):
                continue
            messages.append(Message(role="user", content=user_input))
            messages.append(Message(role="assistant", content=agent_response))

    # Current user message
    messages.append(Message(role="user", content=user_text))

    return messages
