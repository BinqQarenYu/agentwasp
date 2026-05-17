"""FastAPI dashboard app factory and runner."""

import asyncio
import secrets
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import settings
from .auth import AuthMiddleware

logger = structlog.get_logger()

# ---- Security headers ----
# Tailwind Play CDN (unsafe-eval) replaced with pre-compiled static CSS build.
# Inline scripts (unsafe-inline) replaced with per-request nonces + strict-dynamic.
# All CDN scripts/styles are now served from /static (htmx, d3, marked, tailwind+daisyui).
# All inline style="" attributes converted to data-style="" + JS applier (applyDataStyles).
# All <style> blocks in templates carry per-request nonces.
# style-src is now fully hardened: no unsafe-inline.
_CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}' 'strict-dynamic'; "
    "style-src 'self' 'nonce-{nonce}'; "
    "img-src 'self' data: blob: https://raw.githubusercontent.com https://cdn.simpleicons.org; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    "font-src 'self' data:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self';"
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    memory,
    model_manager,
    skill_registry,
    skill_executor,
    scheduler,
    bus,
    redis_url: str,
    health_monitor=None,
    introspector=None,
    identity_manager=None,
    handler=None,
    goal_orchestrator=None,
    integration_registry=None,
    agent_orchestrator=None,
) -> FastAPI:
    app = FastAPI(title="Agent Dashboard", docs_url=None, redoc_url=None)

    # Shared objects on app.state
    app.state.memory = memory
    app.state.model_manager = model_manager
    app.state.skill_registry = skill_registry
    app.state.skill_executor = skill_executor
    app.state.scheduler = scheduler
    app.state.bus = bus
    app.state.redis_url = redis_url
    app.state._redis = None
    app.state.health_monitor = health_monitor
    app.state.introspector = introspector
    app.state.identity_manager = identity_manager
    app.state.handler = handler
    app.state.goal_orchestrator = goal_orchestrator
    app.state.integration_registry = integration_registry
    app.state.agent_orchestrator = agent_orchestrator
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Auth middleware
    app.add_middleware(AuthMiddleware)

    # Security headers middleware
    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        # Generate a fresh nonce for every request; templates read it via request.state.csp_nonce
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = _CSP_TEMPLATE.format(nonce=nonce)
        return response

    # Static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Routes
    from .routes.auth_routes import router as auth_router
    from .routes.overview import router as overview_router
    from .routes.memory import router as memory_router
    from .routes.models import router as models_router
    from .routes.skills import router as skills_router
    from .routes.scheduler import router as scheduler_router
    from .routes.audit import router as audit_router
    from .routes.health import router as health_router
    from .routes.metrics import router as metrics_router
    from .routes.cmd import router as cmd_router
    from .routes.identity import router as identity_router
    from .routes.chat import router as chat_router
    from .routes.live import router as live_router
    from .routes.tasks import router as tasks_router
    from .routes.goals import router as goals_router
    from .routes.integrations import router as integrations_router
    from .routes.agents import router as agents_router
    from .routes.cognitive import router as cognitive_router
    from .routes.brain import router as brain_router
    # Next-gen cognitive systems routes
    from .routes.vector_memory import router as vector_memory_router
    from .routes.world_model import router as world_model_router
    from .routes.skill_evolution import router as skill_evolution_router
    # New dedicated pages
    from .routes.self_improve import router as self_improve_router
    from .routes.behavioral_rules import router as behavioral_rules_router
    from .routes.knowledge_graph import router as knowledge_graph_router
    from .routes.subscriptions import router as subscriptions_router
    from .routes.config_center import router as config_center_router
    from .routes.traces import router as traces_router

    app.include_router(auth_router)
    app.include_router(overview_router)
    app.include_router(memory_router, prefix="/memory")
    app.include_router(models_router, prefix="/models")
    app.include_router(skills_router, prefix="/skills")
    app.include_router(scheduler_router, prefix="/scheduler")
    app.include_router(tasks_router, prefix="/tasks")
    app.include_router(audit_router, prefix="/audit")
    app.include_router(health_router, prefix="/health")
    app.include_router(metrics_router, prefix="/metrics")
    app.include_router(cmd_router, prefix="/api")
    app.include_router(identity_router, prefix="/identity")
    app.include_router(chat_router, prefix="/chat")
    app.include_router(live_router, prefix="/live")
    app.include_router(goals_router, prefix="/goals")
    app.include_router(integrations_router, prefix="/integrations")
    app.include_router(agents_router, prefix="/agents")
    app.include_router(cognitive_router, prefix="/cognitive")
    app.include_router(brain_router, prefix="/brain")
    # Next-gen systems
    app.include_router(vector_memory_router, prefix="/vector-memory")
    app.include_router(world_model_router, prefix="/world-model")
    app.include_router(skill_evolution_router, prefix="/skill-evolution")
    # New dedicated governance/config pages
    app.include_router(self_improve_router, prefix="/self-improve")
    app.include_router(behavioral_rules_router, prefix="/behavioral-rules")
    app.include_router(knowledge_graph_router, prefix="/knowledge-graph")
    app.include_router(subscriptions_router, prefix="/subscriptions")
    app.include_router(config_center_router, prefix="/config")
    app.include_router(traces_router, prefix="/traces")
    from .routes.workspaces import router as workspaces_router
    app.include_router(workspaces_router, prefix="/workspaces")
    
    # Panic Reset
    from .routes.reset import router as reset_router
    app.include_router(reset_router, prefix="/reset")

    # Phase C: Opportunity Engine feed
    from .routes.opportunities import router as opportunities_router
    app.include_router(opportunities_router, prefix="/opportunities")

    # Agent state badge (htmx fragment endpoint)
    from .routes.state import router as state_router
    app.include_router(state_router, prefix="/state")

    return app


async def run_dashboard(
    memory,
    model_manager,
    skill_registry,
    skill_executor,
    scheduler,
    bus,
    shutdown_event: asyncio.Event,
    health_monitor=None,
    introspector=None,
    identity_manager=None,
    handler=None,
    goal_orchestrator=None,
    integration_registry=None,
    agent_orchestrator=None,
):
    app = create_app(
        memory=memory,
        model_manager=model_manager,
        skill_registry=skill_registry,
        skill_executor=skill_executor,
        scheduler=scheduler,
        bus=bus,
        redis_url=settings.redis_url,
        health_monitor=health_monitor,
        introspector=introspector,
        identity_manager=identity_manager,
        handler=handler,
        goal_orchestrator=goal_orchestrator,
        integration_registry=integration_registry,
        agent_orchestrator=agent_orchestrator,
    )

    config = uvicorn.Config(
        app=app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # We handle signals ourselves

    logger.info(
        "dashboard.starting",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
    )

    async def watch_shutdown():
        await shutdown_event.wait()
        server.should_exit = True

    await asyncio.gather(server.serve(), watch_shutdown())
