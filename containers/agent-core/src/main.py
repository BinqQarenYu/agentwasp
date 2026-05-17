import asyncio
import logging
import signal

import structlog

from .config import settings
from .db.session import init_db, close_db, ensure_indexes, async_session
from .events.bus import EventBus
from .events.handlers import EventHandler
from .memory.manager import MemoryManager
from .memory.seed import seed_initial_memory
from .models.manager import ModelManager
from .skills.registry import SkillRegistry
from .skills.executor import SkillExecutor
from .skills.builtin import register_builtin_skills
from .scheduler.scheduler import Scheduler
from .scheduler.jobs import HealthCheckJob, ReflectionJob, MemoryCleanupJob, SnapshotJob, ReminderCheckerJob, MonitorCheckerJob, ProactiveJob, PromotionJob, CustomTaskRunnerJob, CheckInJob, ExecutionKnowledgeSyncJob
from .scheduler.audit_retention import AuditRetentionJob
from .scheduler.db_maintenance import DbMaintenanceJob
from .scheduler.intelligence_monitor import ExecutionIntelligenceMonitorJob
from .scheduler.opportunities_processor import OpportunitiesProcessorJob
from .scheduler.subscriptions import SubscriptionCheckerJob
from .scheduler.dream import DreamJob
from .scheduler.autonomous import AutonomousGoalGeneratorJob
from .scheduler.perception import BackgroundPerceptionJob
from .scheduler.behavioral_learner import BehavioralLearnerJob
from .scheduler.opportunity import OpportunityEngineJob
from .scheduler.integrity import SelfIntegrityMonitorJob
from .scheduler.cpi_monitor import CognitiveLoadMonitorJob
from .scheduler.digest import DigestJob
from .goal_orchestrator import GoalOrchestrator, GoalTickJob, PlanGenerator
from .goal_orchestrator.reflection_job import GoalMetaReflectionJob
from .goal_orchestrator.types import AutonomyMode
from .agent_manager import AgentOrchestrator
from .agent_manager.tick_job import AgentTickJob
from .health.monitor import HealthMonitor
from .integrations import IntegrationRegistry, SecretVault, PolicyEngine, IntegrationSkillBridge
from .integrations.connectors.slack import SlackConnector
from .integrations.connectors.discord import DiscordConnector
from .integrations.connectors.zapier import ZapierConnector
from .integrations.connectors.github import GitHubConnector
from .integrations.connectors.notion import NotionConnector
from .integrations.connectors.webhook import WebhookConnector
from .integrations.connectors.home_assistant import HomeAssistantConnector
from .integrations.connectors.mcp import MCPConnector
# Phase 1 connectors
from .integrations.connectors.telegram import TelegramConnector
from .integrations.connectors.whatsapp import WhatsAppConnector
from .integrations.connectors.signal import SignalConnector
from .integrations.connectors.matrix import MatrixConnector
from .integrations.connectors.weather import WeatherConnector
from .integrations.connectors.spotify import SpotifyConnector
from .integrations.connectors.trello import TrelloConnector
from .integrations.connectors.twitter import TwitterConnector
from .integrations.connectors.image_gen import ImageGenConnector
from .integrations.connectors.gif_search import GifSearchConnector
from .integrations.connectors.browser_controlled import BrowserControlledConnector
from .integrations.connectors.gmail_connector import GmailConnector as GmailIntegrationConnector
from .integrations.connectors.google_calendar import GoogleCalendarConnector
from .integrations.connectors.cron import CronConnector
# Phase 2 connectors
from .integrations.connectors.sonos import SonosConnector
from .integrations.connectors.shazam import ShazamConnector
from .integrations.connectors.philips_hue import PhilipsHueConnector
from .integrations.connectors.eight_sleep import EightSleepConnector
from .integrations.connectors.obsidian import ObsidianConnector
from .integrations.connectors.email_generic import EmailGenericConnector
from .integrations.connectors.nextcloud_talk import NextcloudTalkConnector
# Phase 3 connectors — core parity
from .integrations.connectors.webchat import WebChatConnector
from .integrations.connectors.onepassword import OnePasswordConnector
from .integrations.connectors.whatsapp_baileys import WhatsAppBaileysConnector
from .integrations.connectors.teams import TeamsConnector
from .integrations.connectors.bluebubbles import BlueBubblesConnector
from .integrations.connectors.nostr import NostrConnector
from .integrations.connectors.zalo import ZaloConnector
# Phase 3 connectors — platform bridges
from .integrations.connectors.platform_macos import MacOSBridgeConnector
from .integrations.connectors.platform_ios import IOSBridgeConnector
from .integrations.connectors.platform_android import AndroidBridgeConnector
from .integrations.connectors.platform_windows import WindowsBridgeConnector
from .integrations.connectors.platform_linux import LinuxBridgeConnector
from .integrations.connectors.google_notebooklm import GoogleNotebookLMConnector

from .health.repair import SelfHealer
from .health.introspection import Introspector
from .health.broker_client import BrokerClient
from .identity import IdentityManager
from .observability.metrics import metrics as metrics_collector
from .observability.economics import economics as economics_tracker
from .runtime.registry import registry, SERVICE_MEMORY, SERVICE_MODELS, SERVICE_SKILLS, SERVICE_EXECUTOR, SERVICE_SCHEDULER, SERVICE_BUS, SERVICE_HEALTH, SERVICE_INTROSPECTOR, SERVICE_BROKER, SERVICE_METRICS, SERVICE_ECONOMICS

LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        LOG_LEVELS.get(settings.log_level.upper(), logging.INFO)
    ),
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
)

logger = structlog.get_logger()


async def main():
    logger.info("agent_core.starting", phase=15)

    # Ensure required data directories exist (best-effort — volume may be root-owned)
    import os
    for _d in ["/data/shared", "/data/screenshots", "/data/memory", "/data/logs", "/data/src_patches", "/data/self_improve_backups", "/data/config"]:
        try:
            os.makedirs(_d, exist_ok=True)
        except PermissionError:
            pass  # Directory created by docker-compose or entrypoint as root

    # Seed /data/config/prime.md (+ prime.default.md) from baked-in defaults on
    # first install. Without this the agent boots with an empty persona on a
    # fresh /data/config volume — the operator override block in the system
    # prompt would be blank.
    try:
        import shutil
        for _name in ("prime.md", "prime.default.md"):
            _dst = f"/data/config/{_name}"
            _src = f"/app/config/{_name}"
            if not os.path.exists(_dst) and os.path.exists(_src):
                shutil.copy2(_src, _dst)
                logger.info("agent_core.prime_seeded", file=_name)
    except Exception as _seed_err:
        logger.warning("agent_core.prime_seed_failed", error=str(_seed_err))

    # Apply persisted self-modifications (survive container rebuilds)
    try:
        from .skills.builtin.self_improve import apply_persisted_patches
        _patches_applied = apply_persisted_patches()
        if _patches_applied > 0:
            logger.info("agent_core.persisted_patches_applied", count=_patches_applied)
    except Exception as _e:
        logger.warning("agent_core.persisted_patches_failed", error=str(_e))

    # Initialize observability singletons with Redis URL
    metrics_collector._redis_url = settings.redis_url
    economics_tracker._redis_url = settings.redis_url
    try:
        await economics_tracker.load_from_redis()
    except Exception as _e:
        logger.warning("agent_core.economics_restore_failed", error=str(_e))

    # Initialize database
    await init_db()
    logger.info("agent_core.db_initialized")
    try:
        await ensure_indexes()
        logger.info("agent_core.db_indexes_ensured")
    except Exception as _e:
        logger.warning("agent_core.db_indexes_failed", error=str(_e))

    # Bootstrap admin user from env if no admin exists yet. Lets the
    # installer's onboarding wizard pre-seed credentials so the user does
    # not have to register manually after the dashboard comes up.
    try:
        import os as _os
        from uuid import uuid4 as _uuid4
        from sqlalchemy import func as _func, select as _select
        from .db.models import AdminUser as _AdminUser
        from .db.session import async_session as _admin_session
        from .dashboard.auth import hash_password as _hash_password

        _admin_user = (_os.environ.get("DASHBOARD_USER") or "").strip()
        _admin_pass = _os.environ.get("DASHBOARD_PASSWORD") or ""
        async with _admin_session() as _session:
            _existing_count = (await _session.execute(_select(_func.count(_AdminUser.id)))).scalar_one()
        if _existing_count > 0:
            logger.info("agent_core.admin_bootstrap_skipped", reason="admin_already_exists", count=_existing_count)
        elif _admin_user and len(_admin_user) >= 3 and len(_admin_pass) >= 8:
            async with _admin_session() as _session:
                _session.add(_AdminUser(
                    id=str(_uuid4()),
                    username=_admin_user,
                    password_hash=_hash_password(_admin_pass),
                ))
                await _session.commit()
                logger.info("agent_core.admin_bootstrapped", username=_admin_user)
        else:
            # Fail-closed: no admin exists AND no usable env creds.
            # Generate a strong temporary credential and print it once to the
            # logs. This prevents the open `/register` race where the first
            # visitor on a public deployment becomes admin.
            import secrets as _secrets
            _gen_user = "admin"
            _gen_pass = _secrets.token_urlsafe(18)  # ~24 char URL-safe
            async with _admin_session() as _session:
                _session.add(_AdminUser(
                    id=str(_uuid4()),
                    username=_gen_user,
                    password_hash=_hash_password(_gen_pass),
                ))
                await _session.commit()
            _banner = (
                "\n" + "=" * 70 +
                "\n  WASP — Dashboard temporary credentials (generated at first boot)" +
                "\n" + "-" * 70 +
                f"\n  Username:  {_gen_user}" +
                f"\n  Password:  {_gen_pass}" +
                "\n" + "-" * 70 +
                "\n  Log in at the dashboard URL printed by 'wasp status', then" +
                "\n  change this password from the dashboard. To pre-seed your own" +
                "\n  credentials instead, set DASHBOARD_USER and DASHBOARD_PASSWORD" +
                "\n  in .env BEFORE first start." +
                "\n" + "=" * 70 + "\n"
            )
            # Print to stderr so it's not swallowed by JSON log filters
            import sys as _sys
            print(_banner, file=_sys.stderr, flush=True)
            logger.warning(
                "agent_core.admin_bootstrap_generated",
                msg="generated temporary admin credentials — see stderr banner above",
                username=_gen_user,
            )
    except Exception as _ae:
        logger.warning("agent_core.admin_bootstrap_failed", error=str(_ae)[:200])

    # Ensure agent identity row exists (creates on first run, sets born_at)
    try:
        from .agent.identity import get_or_create as _identity_init
        await _identity_init()
        logger.info("agent_core.identity_initialized")
    except Exception as _ie:
        logger.warning("agent_core.identity_init_failed", error=str(_ie))

    # Restore execution knowledge (strategy scores, learned selectors, global stats)
    try:
        from .intent.execution_persistence import configure_persistence, load_execution_knowledge, apply_loaded_knowledge
        configure_persistence(settings.redis_url)
        _exec_knowledge = await load_execution_knowledge(settings.redis_url)
        apply_loaded_knowledge(_exec_knowledge)
        logger.info("agent_core.execution_knowledge_restored")
    except Exception as _ek_err:
        logger.warning("agent_core.execution_knowledge_restore_failed", error=str(_ek_err)[:120])

    # Initialize memory
    memory = MemoryManager()
    async with async_session() as session:
        await seed_initial_memory(memory, session)

    # Initialize model manager (local-first with Ollama)
    model_manager = ModelManager(
        ollama_base_url=settings.ollama_base_url,
    )
    if settings.ollama_model:
        await model_manager.set_default_model(settings.ollama_model)

    # ── Option A: factory-reset symmetry — check NO_REHYDRATE sentinel ────
    # When set by the dashboard's factory reset, this sentinel suppresses
    # ALL .env-based auto-imports for this and every future boot. Forces
    # the operator to re-configure secrets via the Integrations panel.
    # Cleared only by manual `redis-cli DEL system:no_rehydrate` or by a
    # future "fresh setup" flow on the dashboard.
    import redis.asyncio as aioredis
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    _NO_REHYDRATE = False
    try:
        _NO_REHYDRATE = bool(await r.exists("system:no_rehydrate"))
    except Exception:
        _NO_REHYDRATE = False
    if _NO_REHYDRATE:
        logger.info(
            "agent_core.no_rehydrate_active",
            note=".env auto-import suppressed (factory reset performed). "
                 "Configure all secrets via dashboard.",
        )
    else:
        # Fresh install or normal boot — register providers from .env
        model_manager.auto_detect_providers(settings)

    # Always load runtime API keys from Redis on top (Redis wins over .env).
    # When NO_REHYDRATE is set, this is the ONLY source of provider keys.
    try:
        stored_keys = await r.hgetall("apikeys")
        
        async def _register(p_name: str, p_key: str):
            await model_manager.register_provider(p_name, p_key)
            logger.info("agent_core.api_key_loaded_from_redis", provider=p_name)
            
        tasks = []
        for provider_name, api_key in stored_keys.items():
            if api_key:
                tasks.append(_register(provider_name, api_key))
                
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            
    except Exception:
        logger.warning("agent_core.redis_apikeys_load_failed")
    finally:
        await r.aclose()

    model_manager.redis_url = settings.redis_url
    await model_manager.initialize()
    # First-run: pick a starter model for the first configured remote provider
    # so the agent is usable immediately after install. No-op if a default is
    # already persisted in Redis.
    await model_manager.ensure_default_model()
    logger.info("agent_core.models_initialized")

    # Seed Gmail credentials from config into Redis (only if not already configured).
    # ── Option A: skipped when NO_REHYDRATE sentinel is set (post-factory-reset).
    if not _NO_REHYDRATE and settings.gmail_address and settings.gmail_app_password:
        try:
            r_gmail = aioredis.from_url(settings.redis_url, decode_responses=True)
            try:
                existing = await r_gmail.hgetall("gmail:credentials")
                if not existing.get("address"):
                    await r_gmail.hset("gmail:credentials", mapping={
                        "address": settings.gmail_address,
                        "password": settings.gmail_app_password,
                    })
                    logger.info("agent_core.gmail_credentials_seeded", address=settings.gmail_address)
            finally:
                await r_gmail.aclose()
        except Exception:
            logger.warning("agent_core.gmail_credentials_seed_failed")

    # Initialize skills
    skill_registry = None
    skill_executor = None
    if settings.skills_enabled:
        skill_registry = SkillRegistry()
        register_builtin_skills(skill_registry, memory, settings=settings)
        overrides_loaded = skill_registry.load_overrides_from_disk()
        if overrides_loaded:
            logger.info("agent_core.skill_overrides_loaded", count=overrides_loaded)
        skill_executor = SkillExecutor(
            skill_registry,
            model_manager=model_manager,
            redis_url=settings.redis_url,
        )
        logger.info(
            "agent_core.skills_initialized",
            count=len(skill_registry.list_all()),
            enabled=len(skill_registry.list_enabled()),
        )

    # Initialize Identity Engine
    identity_manager = IdentityManager(redis_url=settings.redis_url)
    await identity_manager.initialize()
    logger.info("agent_core.identity_initialized")

    # Initialize Integration Platform
    _vault = None   # safe default — used in late-wire blocks below regardless of integrations_enabled
    _policy = None  # safe default
    integration_registry = None
    if settings.integrations_enabled:
        _vault  = SecretVault(redis_url=settings.redis_url, master_secret=settings.dashboard_secret or "wasp-default-vault-key")
        _policy = PolicyEngine(redis_url=settings.redis_url, autonomy_mode=settings.integrations_policy_mode)
        await _policy.initialize()
        integration_registry = IntegrationRegistry(
            vault=_vault,
            policy=_policy,
            cb_failure_threshold=settings.integrations_cb_failure_threshold,
            cb_recovery_timeout=settings.integrations_cb_recovery_timeout,
            redis_url=settings.redis_url,
        )
        # Register all connectors
        for _connector in [
            # Phase 0 (original)
            SlackConnector(), DiscordConnector(), ZapierConnector(),
            GitHubConnector(), NotionConnector(), WebhookConnector(),
            HomeAssistantConnector(), MCPConnector(),
            # Phase 1
            TelegramConnector(), WhatsAppConnector(), SignalConnector(), MatrixConnector(),
            WeatherConnector(), SpotifyConnector(), TrelloConnector(), TwitterConnector(),
            ImageGenConnector(), GifSearchConnector(), BrowserControlledConnector(),
            GmailIntegrationConnector(),
            GoogleCalendarConnector(),
            # Phase 2
            SonosConnector(), ShazamConnector(), PhilipsHueConnector(), EightSleepConnector(),
            ObsidianConnector(), EmailGenericConnector(), NextcloudTalkConnector(),
            # Phase 3 — core parity
            WebChatConnector(), OnePasswordConnector(), WhatsAppBaileysConnector(),
            TeamsConnector(), BlueBubblesConnector(), NostrConnector(), ZaloConnector(),
            # Phase 3 — platform bridges
            MacOSBridgeConnector(), IOSBridgeConnector(), AndroidBridgeConnector(),
            WindowsBridgeConnector(), LinuxBridgeConnector(),
            # New Integrations
            GoogleNotebookLMConnector(),
        ]:

            integration_registry.register(_connector)
        logger.info("agent_core.integrations_initialized", connectors=len(integration_registry.list_integrations()))

        # Auto-migrate gmail:credentials → vault + enable policy (runs once, idempotent)
        try:
            _gmail_vault_keys = await _vault.list_keys("gmail-connector")
            if "address" not in _gmail_vault_keys:
                # Vault is empty — check legacy Redis key
                _r_gm = aioredis.from_url(settings.redis_url, decode_responses=True)
                try:
                    _legacy_creds = await _r_gm.hgetall("gmail:credentials")
                finally:
                    await _r_gm.aclose()
                if _legacy_creds.get("address") and _legacy_creds.get("password"):
                    await _vault.set("gmail-connector", "address", _legacy_creds["address"])
                    await _vault.set("gmail-connector", "app_password", _legacy_creds["password"])
                    await _policy.enable("gmail-connector")
                    logger.info(
                        "agent_core.gmail_migrated_to_vault",
                        address=_legacy_creds["address"],
                    )
            else:
                # Vault already has credentials — ensure policy is enabled
                if not _policy.is_enabled("gmail-connector"):
                    await _policy.enable("gmail-connector")
                    logger.info("agent_core.gmail_policy_enabled")
        except Exception as _gm_err:
            logger.warning("agent_core.gmail_migration_failed", error=str(_gm_err))

        # Auto-configure Telegram integration from env vars (idempotent).
        # ── Option A: skipped when NO_REHYDRATE sentinel is set ─────────────
        # After a dashboard factory reset, the operator must re-add the bot
        # token via the Integrations panel — the .env mirror does NOT run.
        # This makes "factory reset" symmetric: every integration must be
        # explicitly re-configured, no automatic resurrection from .env.
        if _NO_REHYDRATE:
            logger.info(
                "agent_core.telegram_rehydrate_suppressed",
                note="NO_REHYDRATE active — Telegram vault mirror skipped. "
                     "Add bot_token via dashboard Integrations panel.",
            )
        else:
            try:
                import os as _os
                _tg_token = _os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
                _tg_allowed = _os.environ.get("TELEGRAM_ALLOWED_USERS", "").strip()
                if _tg_token:
                    _tg_vault_keys = await _vault.list_keys("telegram")
                    if "bot_token" not in _tg_vault_keys:
                        # First run — push token from env into vault
                        await _vault.set("telegram", "bot_token", _tg_token)
                        if _tg_allowed:
                            await _vault.set("telegram", "allowed_users", _tg_allowed)
                        logger.info("agent_core.telegram_migrated_to_vault")
                    # Always ensure policy is enabled when token exists in env
                    if not _policy.is_enabled("telegram"):
                        await _policy.enable("telegram")
                        logger.info("agent_core.telegram_policy_enabled")
                else:
                    logger.info("agent_core.telegram_no_token_in_env",
                                note="Set TELEGRAM_BOT_TOKEN to auto-configure Telegram integration")
            except Exception as _tg_err:
                logger.warning("agent_core.telegram_migration_failed", error=str(_tg_err))

        # Register integration skill bridge
        if skill_registry:
            _bridge = IntegrationSkillBridge(integration_registry)
            from .skills.builtin.integration_skill import IntegrationSkill
            from .skills.capability import CapabilityLevel, capability_registry as _cap_reg
            _cap_reg.register("integration", CapabilityLevel.CONTROLLED)
            skill_registry.register(IntegrationSkill(bridge=_bridge))
            logger.info("agent_core.integration_skill_registered")

            # Wire the registry into the integration_manager skill that was
            # registered earlier without it (in register_builtin_skills).
            try:
                _im = skill_registry.get("integration_manager")
                if _im and hasattr(_im, "set_registry"):
                    _im.set_registry(integration_registry)
                    logger.info("agent_core.integration_manager_wired")
            except Exception as _imerr:
                logger.warning("agent_core.integration_manager_wire_failed", error=str(_imerr))

    # Initialize Goal Engine (needs skills + model_manager + bus — bus initialized below)
    goal_orchestrator = None

    # Initialize event bus
    bus = EventBus(settings.redis_url)
    await bus.connect()
    await bus.ensure_group(settings.stream_incoming, settings.consumer_group)

    # Fix 6 — PEL recovery: claim and ACK zombie messages idle > 5 minutes
    try:
        _pel_result = await bus.client.xautoclaim(
            name=settings.stream_incoming,
            groupname=settings.consumer_group,
            consumername="startup-recovery",
            min_idle_time=300_000,  # 5 minutes in ms
            start_id="0-0",
            count=200,
        )
        # _pel_result = (next_id, [(msg_id, fields), ...], deleted_ids)
        _claimed = _pel_result[1] if isinstance(_pel_result, (list, tuple)) and len(_pel_result) > 1 else []
        if _claimed:
            _claimed_ids = [m[0] for m in _claimed if m]
            await bus.client.xack(settings.stream_incoming, settings.consumer_group, *_claimed_ids)
            logger.info("agent_core.pel_recovery_complete", claimed=len(_claimed_ids))
        else:
            logger.info("agent_core.pel_recovery_clean")
    except Exception as _e:
        logger.warning("agent_core.pel_recovery_failed", error=str(_e))

    # Fix 4 — Apply config:overrides from Redis to runtime settings
    _BOOL_FLAGS = {
        "sovereign_mode", "plan_critic_enabled", "skill_evolution_enabled",
        "temporal_reasoning_enabled", "world_model_enabled", "vector_memory_enabled",
        "meta_agent_enabled", "goal_engine_enabled", "agents_enabled",
        "integrations_enabled", "governor_enabled", "scheduler_enabled",
        "memory_ranking_enabled",
    }
    try:
        import json as _json
        import redis.asyncio as _aioredis
        _r = _aioredis.from_url(settings.redis_url, decode_responses=True)
        _raw = await _r.get("config:overrides")
        await _r.aclose()
        if _raw:
            _overrides: dict = _json.loads(_raw)
            for _k, _v in _overrides.items():
                if _k in _BOOL_FLAGS and isinstance(_v, bool) and hasattr(settings, _k):
                    setattr(settings, _k, _v)
            logger.info("agent_core.config_overrides_applied", count=len(_overrides))
    except Exception as _e:
        logger.warning("agent_core.config_overrides_failed", error=str(_e))

    # Fix 3 — Start SaccadicVision daemon
    _saccadic_vision = None
    try:
        from .runtime.saccadic_vision import SaccadicVision as _SV
        _saccadic_vision = _SV(redis_url=settings.redis_url)
        _saccadic_vision.start()
        logger.info("agent_core.saccadic_vision_started")
    except Exception as _e:
        logger.warning("agent_core.saccadic_vision_failed", error=str(_e))

    # Initialize broker client
    broker_client = BrokerClient(settings.redis_url)
    await broker_client.connect()

    # Late-wire broker_client into SelfImproveSkill for auto-restart on rebuild
    if skill_registry:
        try:
            from .skills.builtin.self_improve import SelfImproveSkill
            for _skill in skill_registry._skills.values():
                if isinstance(_skill, SelfImproveSkill):
                    _skill.set_broker(broker_client)
                    logger.info("agent_core.self_improve_broker_wired")
                    break
        except Exception:
            pass

    # Initialize Goal Engine (now bus is available)
    if settings.goal_engine_enabled and skill_registry and skill_executor:
        plan_generator = PlanGenerator(
            model_manager=model_manager,
            skill_registry=skill_registry,
        )
        _autonomy_mode = AutonomyMode(settings.goal_default_autonomy_mode)
        goal_orchestrator = GoalOrchestrator(
            redis_url=settings.redis_url,
            plan_generator=plan_generator,
            skill_executor=skill_executor,
            memory_manager=memory,
            bus=bus,
            max_concurrent=settings.goal_max_concurrent,
            default_chat_id=settings.scheduler_notify_chat_id,
            budget_max_tokens_planning=settings.goal_budget_max_tokens_planning,
            budget_max_tokens_execution=settings.goal_budget_max_tokens_execution,
            budget_max_replans=settings.goal_budget_max_replans,
            budget_max_memory_bytes=settings.goal_budget_max_memory_bytes,
            default_autonomy_mode=_autonomy_mode,
        )
        logger.info("agent_core.goal_engine_initialized")

        # ── System 2: Plan Critic (late-wire after goal_orchestrator) ──────
        if settings.plan_critic_enabled:
            try:
                from .goal_orchestrator.plan_validator import PlanCritic
                goal_orchestrator.plan_critic = PlanCritic(
                    model_manager=model_manager,
                    skill_registry=skill_registry,
                    max_tokens=settings.plan_critic_max_tokens,
                    enabled=True,
                )
                logger.info("agent_core.plan_critic_initialized")
            except Exception as _e:
                logger.warning("agent_core.plan_critic_init_failed", error=str(_e))

    # Initialize Resource Governor
    governor = None
    try:
        from .governance import ResourceGovernor
        governor = ResourceGovernor(redis_url=settings.redis_url, settings=settings)
        if goal_orchestrator:
            goal_orchestrator.governor = governor
        logger.info("agent_core.resource_governor_initialized", enabled=settings.governor_enabled)
    except Exception as _e:
        logger.warning("agent_core.resource_governor_init_failed", error=str(_e))

    # Initialize Self-Reflection Engine
    reflection_engine = None
    try:
        from .reflection_engine import ReflectionEngine
        reflection_engine = ReflectionEngine(
            model_manager=model_manager,
            redis_url=settings.redis_url,
        )
        if goal_orchestrator:
            goal_orchestrator.reflection_engine = reflection_engine
        logger.info("agent_core.reflection_engine_initialized")
    except Exception as _e:
        logger.warning("agent_core.reflection_engine_init_failed", error=str(_e))

    # Initialize Capability Evolution Engine
    capability_evolution_engine = None
    if skill_registry:
        try:
            from .capability_evolution_engine import CapabilityEvolutionEngine
            capability_evolution_engine = CapabilityEvolutionEngine(
                model_manager=model_manager,
                skill_registry=skill_registry,
                redis_url=settings.redis_url,
                memory_manager=memory,
            )
            capability_evolution_engine.reflection_engine = reflection_engine
            capability_evolution_engine.governor = governor
            if goal_orchestrator:
                goal_orchestrator.capability_evolution_engine = capability_evolution_engine
            logger.info("agent_core.capability_evolution_engine_initialized")
        except Exception as _e:
            logger.warning("agent_core.cee_init_failed", error=str(_e))

    # Log active execution backend (Objective 3 — Worker Separation Foundations)
    if goal_orchestrator:
        _backend = goal_orchestrator.execution_backend
        logger.info(
            "agent_core.execution_backend_active",
            backend=_backend.backend_name,
            note="swap to QueueExecutionBackend for distributed workers",
        )

    # Initialize Multi-Agent Orchestration Layer
    agent_orchestrator = None
    if settings.agents_enabled and goal_orchestrator and skill_executor:
        agent_orchestrator = AgentOrchestrator(
            redis_url=settings.redis_url,
            goal_orchestrator=goal_orchestrator,
            skill_executor=skill_executor,
            memory_manager=memory,
            model_manager=model_manager,
            bus=bus,
            max_active_agents=settings.agents_max_active,
            max_concurrent_agent_steps=settings.agents_max_concurrent_steps,
            cpu_usage_threshold=settings.agents_cpu_threshold,
            global_token_budget_per_minute=settings.agents_global_token_budget_per_minute,
        )
        logger.info("agent_core.agent_orchestrator_initialized")
        # Late-wire vault into GoogleCalendarConnector so it can persist refreshed tokens
        if _vault:
            try:
                from .integrations.connectors.google_calendar import GoogleCalendarConnector as _GCConn
                for _conn in integration_registry._connectors.values():
                    if isinstance(_conn, _GCConn):
                        _conn._vault = _vault
                        break
            except Exception:
                pass

        # Late-wire vault into GoogleCalendarSkill registered earlier
        if skill_registry and _vault:
            try:
                from .skills.builtin.google_calendar import GoogleCalendarSkill as _GCSkill
                for _sk in skill_registry._skills.values():
                    if isinstance(_sk, _GCSkill):
                        _sk._vault = _vault
                        logger.info("agent_core.google_calendar_skill_vault_wired")
                        break
            except Exception:
                pass

        # Late-wire vault + policy into GmailSkill (integrations single source of truth)
        if skill_registry and _vault:
            try:
                from .skills.builtin.gmail import GmailSkill as _GmailSkill
                for _sk in skill_registry._skills.values():
                    if isinstance(_sk, _GmailSkill):
                        _sk._vault = _vault
                        _sk._policy = _policy
                        logger.info("agent_core.gmail_skill_vault_wired")
                        break
            except Exception:
                pass

        # Late-wire agent_orchestrator into the AgentManagerSkill that was registered earlier
        try:
            from .skills.builtin.agent_manager_skill import AgentManagerSkill
            for skill in skill_registry._skills.values():
                if isinstance(skill, AgentManagerSkill):
                    skill._orch = agent_orchestrator
                    logger.info("agent_core.agent_manager_skill_wired")
                    break
        except Exception:
            pass

        # ── System 3: Meta-Agent Supervisor ───────────────────────────────
        if settings.meta_agent_enabled and skill_registry:
            try:
                from .agent_manager.meta_agent import MetaSupervisor
                from .skills.builtin.meta_orchestrate_skill import MetaOrchestrateSkill
                meta_supervisor = MetaSupervisor(
                    agent_orchestrator=agent_orchestrator,
                    model_manager=model_manager,
                    max_team_size=settings.meta_agent_max_team_size,
                )
                meta_skill = MetaOrchestrateSkill(meta_supervisor=meta_supervisor)
                skill_registry.register(meta_skill)
                logger.info("agent_core.meta_agent_initialized")
            except Exception as _e:
                logger.warning("agent_core.meta_agent_init_failed", error=str(_e))

    # Initialize health subsystem
    health_monitor = HealthMonitor(
        redis_url=settings.redis_url,
        ollama_base_url=settings.ollama_base_url,
    )
    self_healer = SelfHealer(
        bus=bus,
        memory=memory,
        model_manager=model_manager,
        notify_chat_id=settings.scheduler_notify_chat_id,
        broker_client=broker_client,
    )
    introspector = Introspector(
        memory=memory,
        model_manager=model_manager,
        skill_registry=skill_registry,
        health_monitor=health_monitor,
    )
    logger.info("agent_core.health_initialized")

    # Initialize scheduler
    scheduler = None
    if settings.scheduler_enabled:
        scheduler = Scheduler(
            redis_url=settings.redis_url,
            bus=bus,
            notify_chat_id=settings.scheduler_notify_chat_id,
        )
        scheduler.register(
            "health_check",
            settings.scheduler_health_check_interval,
            HealthCheckJob(
                bus=bus,
                chat_id=settings.scheduler_notify_chat_id,
                health_monitor=health_monitor,
                self_healer=self_healer,
            ),
        )
        scheduler.register(
            "reflection",
            settings.scheduler_reflection_interval,
            ReflectionJob(memory, model_manager),
        )
        scheduler.register(
            "memory_cleanup",
            settings.scheduler_memory_cleanup_interval,
            MemoryCleanupJob(memory),
        )
        scheduler.register(
            "audit_retention",
            21600,  # Every 6 hours (was daily; raised batch size 5k→50k)
            AuditRetentionJob(retention_days=30),
        )
        scheduler.register(
            "db_maintenance",
            604800,  # Weekly VACUUM ANALYZE — safe online, no table locks
            DbMaintenanceJob(),
        )
        scheduler.register(
            "snapshot",
            settings.scheduler_snapshot_interval,
            SnapshotJob(memory),
        )
        scheduler.register(
            "reminder_checker",
            30,  # Check every 30 seconds
            ReminderCheckerJob(bus=bus, chat_id=settings.scheduler_notify_chat_id, memory=memory, agent_orchestrator=agent_orchestrator),
        )
        scheduler.register(
            "monitor_checker",
            300,  # Check every 5 minutes
            MonitorCheckerJob(bus=bus, chat_id=settings.scheduler_notify_chat_id, memory=memory),
        )
        scheduler.register(
            "proactive",
            settings.scheduler_proactive_interval,
            ProactiveJob(
                bus=bus,
                chat_id=settings.scheduler_notify_chat_id,
                memory=memory,
                model_manager=model_manager,
                redis_url=settings.redis_url,
                quiet_start=settings.proactive_quiet_start,
                quiet_end=settings.proactive_quiet_end,
                max_daily=settings.proactive_max_daily,
            ),
        )
        scheduler.register(
            "promotion",
            43200,  # Every 12 hours
            PromotionJob(memory),
        )
        scheduler.register(
            "checkin",
            3600,  # Check every hour
            CheckInJob(
                bus=bus,
                chat_id=settings.scheduler_notify_chat_id,
                memory=memory,
                redis_url=settings.redis_url,
                quiet_start=settings.proactive_quiet_start,
                quiet_end=settings.proactive_quiet_end,
            ),
        )
        scheduler.register(
            "custom_task_runner",
            60,  # Check every 60 seconds
            CustomTaskRunnerJob(bus=bus, default_chat_id=settings.scheduler_notify_chat_id, redis_url=settings.redis_url),
        )
        scheduler.register(
            "subscription_checker",
            300,  # Check every 5 minutes
            SubscriptionCheckerJob(bus=bus, redis_url=settings.redis_url, chat_id=settings.scheduler_notify_chat_id),
        )
        if goal_orchestrator is not None:
            scheduler.register(
                "goal_tick",
                settings.goal_tick_interval,
                GoalTickJob(orchestrator=goal_orchestrator),
            )
            scheduler.register(
                "goal_meta_reflection",
                settings.goal_meta_reflection_interval,
                GoalMetaReflectionJob(
                    model_manager=model_manager,
                    redis_url=settings.redis_url,
                    bus=bus,
                    notify_chat_id=settings.scheduler_notify_chat_id,
                ),
            )
        if agent_orchestrator is not None:
            scheduler.register(
                "agent_tick",
                settings.agents_tick_interval,
                AgentTickJob(orchestrator=agent_orchestrator),
            )
        scheduler.register(
            "dream",
            3600,  # Check every hour (activates only when conditions met)
            DreamJob(
                memory=memory,
                model_manager=model_manager,
                redis_url=settings.redis_url,
                bus=bus,
                notify_chat_id=settings.scheduler_notify_chat_id,
            ),
        )
        scheduler.register(
            "autonomous",
            1800,  # Check every 30 minutes
            AutonomousGoalGeneratorJob(
                model_manager=model_manager,
                redis_url=settings.redis_url,
                bus=bus,
                notify_chat_id=settings.scheduler_notify_chat_id,
                goal_orchestrator=goal_orchestrator,
            ),
        )
        scheduler.register(
            "digest",
            86400,  # Daily check (regenerates max once per 20h)
            DigestJob(
                model_manager=model_manager,
                redis_url=settings.redis_url,
                memory=memory,
            ),
        )
        scheduler.register(
            "cpi_monitor",
            300,  # Every 5 minutes
            CognitiveLoadMonitorJob(
                redis_url=settings.redis_url,
                max_concurrent_goals=settings.goal_max_concurrent,
            ),
        )
        scheduler.register(
            "self_integrity",
            21600,  # Run every 6 hours
            SelfIntegrityMonitorJob(
                redis_url=settings.redis_url,
                bus=bus,
                notify_chat_id=settings.scheduler_notify_chat_id,
            ),
        )
        scheduler.register(
            "perception",
            900,  # Check every 15 minutes
            BackgroundPerceptionJob(
                model_manager=model_manager,
                redis_url=settings.redis_url,
                bus=bus,
                notify_chat_id=settings.scheduler_notify_chat_id,
                quiet_start=settings.proactive_quiet_start,
                quiet_end=settings.proactive_quiet_end,
            ),
        )
        scheduler.register(
            "behavioral_learner",
            120,  # Check every 2 minutes for pending corrections
            BehavioralLearnerJob(
                model_manager=model_manager,
                redis_url=settings.redis_url,
                bus=bus,
                notify_chat_id=settings.scheduler_notify_chat_id,
            ),
        )
        # ── Next-Gen System Scheduler Jobs ────────────────────────────────

        # System 1: Vector Index (requires VECTOR_MEMORY_ENABLED)
        if settings.vector_memory_enabled:
            try:
                from .scheduler.vector_index import VectorIndexJob
                from .memory.embeddings import create_provider as _make_embed_provider
                _embed_provider = _make_embed_provider(settings)
                scheduler.register(
                    "vector_index",
                    1800,  # Every 30 minutes
                    VectorIndexJob(provider=_embed_provider),
                )
                logger.info(
                    "agent_core.vector_index_job_registered",
                    embed_provider=_embed_provider.model_name,
                    semantic=_embed_provider.is_semantic,
                )
            except Exception as _e:
                logger.warning("agent_core.vector_index_job_failed", error=str(_e))

        # System 4: World Model Update (enabled by default)
        if settings.world_model_enabled:
            try:
                from .scheduler.world_model_job import WorldModelUpdateJob
                scheduler.register(
                    "world_model",
                    900,  # Every 15 minutes
                    WorldModelUpdateJob(ollama_url=settings.ollama_base_url),
                )
                logger.info("agent_core.world_model_job_registered")
            except Exception as _e:
                logger.warning("agent_core.world_model_job_failed", error=str(_e))

        # System 5: Skill Evolution (requires SKILL_EVOLUTION_ENABLED)
        if settings.skill_evolution_enabled and model_manager:
            try:
                from .scheduler.skill_evolution_job import SkillEvolutionJob
                scheduler.register(
                    "skill_evolution",
                    21600,  # Every 6 hours
                    SkillEvolutionJob(
                        model_manager=model_manager,
                        min_pattern_count=settings.skill_pattern_threshold,
                    ),
                )
                logger.info("agent_core.skill_evolution_job_registered")
            except Exception as _e:
                logger.warning("agent_core.skill_evolution_job_failed", error=str(_e))

        # Capability Evolution Engine — periodic gap scan
        if capability_evolution_engine:
            try:
                from .scheduler.capability_evolution import CapabilityEvolutionJob
                scheduler.register(
                    "capability_evolution",
                    3600,  # Every hour — complements fire-and-forget on goal failure
                    CapabilityEvolutionJob(engine=capability_evolution_engine),
                )
                logger.info("agent_core.capability_evolution_job_registered")
            except Exception as _e:
                logger.warning("agent_core.cee_job_init_failed", error=str(_e))

        # CapabilityLearner — mines execution traces for recurring patterns
        if settings.redis_url:
            try:
                from .scheduler.capability_learner import CapabilityLearnerJob
                scheduler.register(
                    "capability_learner",
                    3600,
                    CapabilityLearnerJob(redis_url=settings.redis_url),
                )
                logger.info("agent_core.capability_learner_registered")
            except Exception as _cle:
                logger.warning("agent_core.capability_learner_init_failed", error=str(_cle))

        # Execution Knowledge Sync — Redis → PostgreSQL durability flush every 5 min
        try:
            scheduler.register(
                "execution_knowledge_sync",
                300,
                ExecutionKnowledgeSyncJob(redis_url=settings.redis_url),
            )
            logger.info("agent_core.execution_knowledge_sync_registered")
        except Exception as _eks_err:
            logger.warning("agent_core.execution_knowledge_sync_init_failed", error=str(_eks_err))

        # Opportunities Processor — consumes opportunities:pending queue (every 5 min)
        try:
            scheduler.register(
                "opportunities_processor",
                300,
                OpportunitiesProcessorJob(redis_url=settings.redis_url),
            )
            logger.info("agent_core.opportunities_processor_registered")
        except Exception as _op_err:
            logger.warning("agent_core.opportunities_processor_init_failed", error=str(_op_err))

        # Execution Intelligence Monitor — evidence-based pattern detection (every 10 min)
        try:
            scheduler.register(
                "execution_intelligence_monitor",
                600,
                ExecutionIntelligenceMonitorJob(
                    redis_url=settings.redis_url,
                    bus=bus,
                    notify_chat_id=settings.scheduler_notify_chat_id,
                ),
            )
            logger.info("agent_core.execution_intelligence_monitor_registered")
        except Exception as _eim_err:
            logger.warning("agent_core.execution_intelligence_monitor_init_failed", error=str(_eim_err))

        # Opportunity Engine — proactive automation detection
        try:
            scheduler.register(
                "opportunity_engine",
                7200,  # Every 2 hours
                OpportunityEngineJob(
                    memory=memory,
                    model_manager=model_manager,
                    redis_url=settings.redis_url,
                    bus=bus,
                    notify_chat_id=settings.scheduler_notify_chat_id,
                    governor=governor,
                    goal_orchestrator=goal_orchestrator,
                ),
            )
            logger.info("agent_core.opportunity_engine_registered")
        except Exception as _e:
            logger.warning("agent_core.opportunity_engine_init_failed", error=str(_e))

        # Procedural memory pruner — daily cleanup of stale procedures
        try:
            from .scheduler.procedural_pruner import ProceduralPrunerJob
            scheduler.register(
                "procedural_pruner",
                86400,  # Every 24 hours
                ProceduralPrunerJob(),
            )
            logger.info("agent_core.procedural_pruner_registered")
        except Exception as _e:
            logger.warning("agent_core.procedural_pruner_init_failed", error=str(_e))

        # Growth-control pruners — keep unbounded tables within safe size limits
        try:
            from .scheduler.memory_pruner import (
                KnowledgeGraphPrunerJob,
                LearningExamplesPrunerJob,
                BehavioralRulesPrunerJob,
            )
            scheduler.register(
                "kg_pruner",
                86400,    # Daily — KG grows ~500 nodes/day at moderate usage
                KnowledgeGraphPrunerJob(),
            )
            scheduler.register(
                "learning_pruner",
                604800,   # Weekly — learning examples grow slowly
                LearningExamplesPrunerJob(),
            )
            scheduler.register(
                "behavioral_pruner",
                2592000,  # Monthly (30d) — archive/delete stale behavioral rules
                BehavioralRulesPrunerJob(),
            )
            logger.info("agent_core.memory_pruners_registered")
        except Exception as _e:
            logger.warning("agent_core.memory_pruners_init_failed", error=str(_e))

        # Execution reflection pruner — keeps execution_reflections table ≤ 1000 rows
        try:
            from .scheduler.execution_reflection_pruner import ExecutionReflectionPrunerJob
            scheduler.register(
                "execution_reflection_pruner",
                21600,  # Every 6 hours
                ExecutionReflectionPrunerJob(),
            )
            logger.info("agent_core.execution_reflection_pruner_registered")
        except Exception as _ep:
            logger.warning("agent_core.execution_reflection_pruner_init_failed", error=str(_ep))

        # KG Insights Updater — computes salience + tool patterns, caches to Redis
        try:
            from .scheduler.kg_insights_updater import KgInsightsUpdaterJob
            scheduler.register(
                "kg_insights_updater",
                1800,  # Every 30 minutes
                KgInsightsUpdaterJob(redis_url=settings.redis_url),
            )
            logger.info("agent_core.kg_insights_updater_registered")
        except Exception as _kg_e:
            logger.warning("agent_core.kg_insights_updater_init_failed", error=str(_kg_e))

        # Disk cleanup — browser sessions (weekly) + screenshots (daily).
        # Register each job in its own try block so a single failure (e.g. an
        # import-time error in one job class) cannot silently take down the
        # others — that's how browser_session_cleanup vanished from
        # scheduler:job_state in earlier builds.
        try:
            from .scheduler.disk_cleanup import (
                BrowserSessionCleanupJob,
                ScreenshotCleanupJob,
                DiskMonitorJob,
            )
        except Exception as _dc_imp:
            logger.exception("agent_core.disk_cleanup_import_failed", error=str(_dc_imp))
            BrowserSessionCleanupJob = ScreenshotCleanupJob = DiskMonitorJob = None  # type: ignore

        if BrowserSessionCleanupJob is not None:
            try:
                scheduler.register(
                    "browser_session_cleanup",
                    604800,  # Weekly (7 days)
                    BrowserSessionCleanupJob(),
                )
            except Exception as _bsc_e:
                logger.exception("agent_core.browser_session_cleanup_register_failed", error=str(_bsc_e))

        if ScreenshotCleanupJob is not None:
            try:
                scheduler.register(
                    "screenshot_cleanup",
                    86400,  # Daily
                    ScreenshotCleanupJob(redis_url=settings.redis_url),
                )
            except Exception as _scc_e:
                logger.exception("agent_core.screenshot_cleanup_register_failed", error=str(_scc_e))

        if DiskMonitorJob is not None:
            try:
                scheduler.register(
                    "disk_monitor",
                    1800,  # Every 30 minutes
                    DiskMonitorJob(
                        redis_url=settings.redis_url,
                        bus=bus,
                        notify_chat_id=str(settings.scheduler_notify_chat_id) if settings.scheduler_notify_chat_id else "",
                    ),
                )
            except Exception as _dm_e:
                logger.exception("agent_core.disk_monitor_register_failed", error=str(_dm_e))

        logger.info("agent_core.disk_cleanup_jobs_registered")

        await scheduler.start()
        logger.info("agent_core.scheduler_started")
        # Late-register CronConnector now that scheduler is fully initialized
        if integration_registry:
            integration_registry.register(CronConnector(scheduler=scheduler))
            logger.info("agent_core.cron_connector_registered")

    # Register all services in the service registry (clean interface boundaries)
    registry.register(SERVICE_MEMORY, memory)
    registry.register(SERVICE_MODELS, model_manager)
    registry.register(SERVICE_SKILLS, skill_registry)
    registry.register(SERVICE_EXECUTOR, skill_executor)
    registry.register(SERVICE_SCHEDULER, scheduler)
    registry.register(SERVICE_BUS, bus)
    registry.register(SERVICE_HEALTH, health_monitor)
    registry.register(SERVICE_INTROSPECTOR, introspector)
    registry.register(SERVICE_BROKER, broker_client)
    registry.register(SERVICE_METRICS, metrics_collector)
    registry.register(SERVICE_ECONOMICS, economics_tracker)
    logger.info("agent_core.service_registry_ready", services=len(registry.list_services()))

    # Create handler with memory, model manager, skills, scheduler, and identity
    handler = EventHandler(
        bus, settings.stream_outgoing, memory, model_manager,
        skill_registry=skill_registry, skill_executor=skill_executor,
        scheduler=scheduler, introspector=introspector,
        broker_client=broker_client,
        identity_manager=identity_manager,
        redis_url=settings.redis_url,
        goal_orchestrator=goal_orchestrator,
        governor=governor,
        agent_orchestrator=agent_orchestrator,
    )

    shutdown = asyncio.Event()

    def signal_handler():
        logger.info("agent_core.shutdown_signal")
        shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    logger.info("agent_core.ready", consumer_group=settings.consumer_group)

    # Consumer loop coroutine
    async def consumer_loop():
        while not shutdown.is_set():
            try:
                messages = await bus.consume(
                    stream=settings.stream_incoming,
                    group=settings.consumer_group,
                    consumer=settings.consumer_name,
                    count=1,
                    block=5000,
                )
                for msg_id, data in messages:
                    try:
                        await handler.handle(msg_id, data)
                    except Exception:
                        logger.exception("agent_core.handler_error", msg_id=msg_id)
                    finally:
                        await bus.ack(settings.stream_incoming, settings.consumer_group, msg_id)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("agent_core.consume_error")
                await asyncio.sleep(2)

    # Start consumer + dashboard concurrently
    tasks = [consumer_loop()]

    if settings.dashboard_enabled and settings.dashboard_secret:
        from .dashboard.app import run_dashboard
        tasks.append(run_dashboard(
            memory=memory,
            model_manager=model_manager,
            skill_registry=skill_registry,
            skill_executor=skill_executor,
            scheduler=scheduler,
            bus=bus,
            shutdown_event=shutdown,
            health_monitor=health_monitor,
            introspector=introspector,
            identity_manager=identity_manager,
            handler=handler,
            goal_orchestrator=goal_orchestrator,
            integration_registry=integration_registry,
            agent_orchestrator=agent_orchestrator,
        ))
        logger.info("agent_core.dashboard_enabled", port=settings.dashboard_port)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException) and not isinstance(r, (asyncio.CancelledError, KeyboardInterrupt)):
            logger.error("agent_core.fatal_task_exception", error=str(r))
            raise SystemExit(1)

    if scheduler:
        await scheduler.stop()

    if _saccadic_vision is not None:
        try:
            _saccadic_vision.stop()
            logger.info("agent_core.saccadic_vision_stopped")
        except Exception:
            pass

    await bus.disconnect()
    await close_db()
    logger.info("agent_core.stopped")


if __name__ == "__main__":
    asyncio.run(main())
