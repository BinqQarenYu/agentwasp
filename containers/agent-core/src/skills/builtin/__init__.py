from ...memory.manager import MemoryManager
from ..capability import CapabilityLevel, capability_registry
from ..registry import SkillRegistry

from .calculate import CalculateSkill
from .datetime_skill import GetDatetimeSkill
from .fetch_url import FetchUrlSkill
from .file_ops import ReadFileSkill, WriteFileSkill
from .gmail import GmailSkill
from .browser import BrowserSkill
from .browser_screenshot_full_page import BrowserScreenshotFullPageSkill
from .browser_deep_scrape import BrowserDeepScrapeSkill
from .browser_smart_navigate import BrowserSmartNavigateSkill
from .http_request import HttpRequestSkill
from .notes import CreateNoteSkill, SearchNotesSkill
from .python_exec import PythonExecSkill
from .reminders import CreateReminderSkill, DeleteReminderSkill, ListRemindersSkill
from .shell import ShellSkill
from .system_info import SystemInfoSkill
from .translate import TranslateSkill
from .weather import GetWeatherSkill
from .web_search import WebSearchSkill
from .monitors import CreateMonitorSkill, ListMonitorsSkill, RemoveMonitorSkill
from .openclaw_skill import OpenClawSkill as OpenClawMgmtSkill
from .scrape import ScrapeSkill
from .skill_manager import SkillManagerSkill
from .task_manager import TaskManagerSkill
from .self_improve import SelfImproveSkill
from .subscribe import SubscribeSkill
from .agent_manager_skill import AgentManagerSkill
from .integration_manager_skill import IntegrationManagerSkill
from .google_calendar import GoogleCalendarSkill
from .render_report import RenderReportSkill
from .extract_fields import ExtractFieldsSkill
from .deep_scraper import DeepScraperSkill

# Capability level declarations for all builtin skills
_CAPABILITY_MAP: dict[str, CapabilityLevel] = {
    # SAFE — pure computation, no side effects
    "calculate":      CapabilityLevel.SAFE,
    "get_datetime":   CapabilityLevel.SAFE,
    "get_weather":    CapabilityLevel.SAFE,
    "translate":      CapabilityLevel.SAFE,
    "system_info":    CapabilityLevel.SAFE,
    # MONITORED — read-only external access
    "web_search":     CapabilityLevel.MONITORED,
    "fetch_url":      CapabilityLevel.MONITORED,
    "browser":        CapabilityLevel.MONITORED,
    "scrape":                       CapabilityLevel.MONITORED,
    "browser_screenshot_full_page": CapabilityLevel.MONITORED,
    "browser_deep_scrape":          CapabilityLevel.MONITORED,
    "browser_smart_navigate":       CapabilityLevel.MONITORED,
    # CONTROLLED — scoped writes with bounded impact
    "create_reminder":  CapabilityLevel.CONTROLLED,
    "list_reminders":   CapabilityLevel.CONTROLLED,
    "create_note":      CapabilityLevel.CONTROLLED,
    "search_notes":     CapabilityLevel.CONTROLLED,
    "create_monitor":   CapabilityLevel.CONTROLLED,
    "list_monitors":    CapabilityLevel.CONTROLLED,
    "remove_monitor":   CapabilityLevel.CONTROLLED,
    "gmail":            CapabilityLevel.CONTROLLED,
    "skill_manager":    CapabilityLevel.CONTROLLED,
    "task_manager":     CapabilityLevel.CONTROLLED,
    "openclaw":         CapabilityLevel.CONTROLLED,
    # RESTRICTED — arbitrary operations
    "shell":            CapabilityLevel.RESTRICTED,
    "python_exec":      CapabilityLevel.RESTRICTED,
    "http_request":     CapabilityLevel.RESTRICTED,
    "read_file":        CapabilityLevel.RESTRICTED,
    "write_file":       CapabilityLevel.RESTRICTED,
    "self_improve":     CapabilityLevel.PRIVILEGED,
    "subscribe":        CapabilityLevel.CONTROLLED,
    "agent_manager":    CapabilityLevel.CONTROLLED,
    "integration_manager": CapabilityLevel.CONTROLLED,
    "google_calendar":  CapabilityLevel.CONTROLLED,
    "render_report":    CapabilityLevel.SAFE,
    "extract_fields":   CapabilityLevel.SAFE,
    "delete_reminder":  CapabilityLevel.CONTROLLED,
    "meta_orchestrate": CapabilityLevel.CONTROLLED,
    "deep_scraper":     CapabilityLevel.MONITORED,
}


def register_builtin_skills(registry: SkillRegistry, memory: MemoryManager, settings=None, vault=None):
    """Register all built-in skills and declare their capability levels."""
    # Register capability levels first
    for skill_name, level in _CAPABILITY_MAP.items():
        capability_registry.register(skill_name, level)

    # Stateless skills
    registry.register(WebSearchSkill())
    registry.register(FetchUrlSkill())
    registry.register(GetDatetimeSkill())
    registry.register(GetWeatherSkill())
    registry.register(SystemInfoSkill())
    registry.register(CalculateSkill())
    registry.register(TranslateSkill())

    # Memory-backed skills
    registry.register(CreateReminderSkill(memory))
    registry.register(ListRemindersSkill(memory))
    registry.register(DeleteReminderSkill(memory))
    registry.register(CreateNoteSkill(memory))
    registry.register(SearchNotesSkill(memory))

    # Filesystem skills
    registry.register(ReadFileSkill())
    registry.register(WriteFileSkill())

    # Power skills
    registry.register(ShellSkill())
    registry.register(PythonExecSkill())
    registry.register(HttpRequestSkill())
    registry.register(BrowserSkill())
    registry.register(BrowserScreenshotFullPageSkill())
    registry.register(BrowserDeepScrapeSkill())
    registry.register(BrowserSmartNavigateSkill())

    # Monitoring skills
    registry.register(CreateMonitorSkill(memory))
    registry.register(ListMonitorsSkill(memory))
    registry.register(RemoveMonitorSkill(memory))

    # Scraping
    registry.register(ScrapeSkill())

    # Skill management (needs registry reference for enable/disable)
    registry.register(SkillManagerSkill(registry))

    # Gmail (always registered — credentials loaded from Redis or env at runtime)
    gmail_addr = settings.gmail_address if settings else ""
    gmail_pass = settings.gmail_app_password if settings else ""
    gmail_redis = settings.redis_url if settings else "redis://agent-redis:6379/0"
    registry.register(GmailSkill(redis_url=gmail_redis, address=gmail_addr, app_password=gmail_pass))

    # Task manager (custom scheduled tasks)
    task_redis = settings.redis_url if settings else "redis://agent-redis:6379/0"
    task_chat_id = settings.scheduler_notify_chat_id if settings else ""
    registry.register(TaskManagerSkill(redis_url=task_redis, default_chat_id=task_chat_id))

    # OpenClaw management
    registry.register(OpenClawMgmtSkill())

    # Self-improvement (agent modifies its own source code with user approval)
    self_improve_redis = settings.redis_url if settings else "redis://agent-redis:6379/0"
    registry.register(SelfImproveSkill(redis_url=self_improve_redis))

    # Subscriptions (RSS feeds and price alerts)
    sub_redis = settings.redis_url if settings else "redis://agent-redis:6379/0"
    sub_chat_id = settings.scheduler_notify_chat_id if settings else ""
    registry.register(SubscribeSkill(redis_url=sub_redis, default_chat_id=sub_chat_id))

    # Agent manager (registered without orchestrator — late-wired in main.py after agent_orchestrator is ready)
    registry.register(AgentManagerSkill(agent_orchestrator=None))

    # Integration manager — lets the LLM configure integrations from chat.
    # Late-wired in main.py after integration_registry is initialized.
    registry.register(IntegrationManagerSkill(registry=None))

    # Google Calendar (OAuth2 — credentials stored in integration vault)
    registry.register(GoogleCalendarSkill(vault=vault))

    # Render report (template-based output formatting)
    render_redis = settings.redis_url if settings else "redis://agent-redis:6379/0"
    registry.register(RenderReportSkill(redis_url=render_redis))

    # Extract fields (JSON field extraction with per-execution context)
    extract_redis = settings.redis_url if settings else "redis://agent-redis:6379/0"
    registry.register(ExtractFieldsSkill(redis_url=extract_redis))

    # Deep scraper (Playwright/Crawlee containerized — YouTube transcripts + JS-heavy pages)
    registry.register(DeepScraperSkill())

    # Load persisted Python skills from /data/skills/*/skill.py
    try:
        from ..openclaw.loader import load_all_python_skills
        for py_skill in load_all_python_skills():
            registry.register(py_skill)
    except Exception:
        import structlog as _sl
        _sl.get_logger().warning("skill_loader.python_skills_failed")
