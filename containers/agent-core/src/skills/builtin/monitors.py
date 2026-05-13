"""Web monitoring skills — create, list, remove website monitors."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

import httpx
import structlog
from bs4 import BeautifulSoup

from ...db.session import async_session
from ...memory.manager import MemoryManager
from ...memory.types import MemoryQuery, MemoryType
from ...utils.network_safety import validate_url_for_request
from ..base import SkillBase
from ..types import SkillDefinition, SkillParam, SkillResult, ParamType

logger = structlog.get_logger()

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _content_hash(text: str) -> str:
    """SHA-256 hash of normalized text."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


async def fetch_page_content(
    url: str, selector: str = "", use_browser: bool = False,
) -> tuple[str, str | None]:
    """Fetch a page and return (clean_text, error_or_none).

    For use_browser=True, uses the Selenium browser skill.
    Otherwise uses httpx + BeautifulSoup (same as fetch_url).
    """
    if use_browser:
        return await _fetch_with_browser(url, selector)
    return await _fetch_with_httpx(url, selector)


async def _fetch_with_httpx(url: str, selector: str = "") -> tuple[str, str | None]:
    """Light fetch with httpx + BeautifulSoup. SSRF-guarded on every redirect hop."""
    reason = await validate_url_for_request(url)
    if reason is not None:
        return "", f"Blocked: {reason}"
    try:
        # follow_redirects=False — we re-validate each Location manually below.
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            current = url
            for _ in range(6):  # max 5 redirects
                resp = await client.get(current, headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Accept-Language": "es,en;q=0.9",
                })
                if resp.is_redirect and resp.headers.get("location"):
                    current = str(httpx.URL(current).join(resp.headers["location"]))
                    reason = await validate_url_for_request(current)
                    if reason is not None:
                        return "", f"Blocked redirect: {reason}"
                    continue
                break
            resp.raise_for_status()
    except httpx.TimeoutException:
        return "", f"Timeout fetching {url}"
    except httpx.HTTPStatusError as e:
        return "", f"HTTP {e.response.status_code} for {url}"
    except Exception as e:
        return "", f"Fetch error: {e}"

    try:
        soup = BeautifulSoup(resp.text, "html.parser")

        if selector:
            target = soup.select_one(selector)
            if target:
                soup = BeautifulSoup(str(target), "html.parser")

        # Remove noise
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)

        # Normalize whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)

        if len(text) < 30:
            return "", f"Page returned very little content ({len(text)} chars). May need use_browser=true."

        return text, None
    except Exception as e:
        return "", f"Parse error: {e}"


async def _fetch_with_browser(url: str, selector: str = "") -> tuple[str, str | None]:
    """Fetch with headless Chromium via the browser skill."""
    try:
        from .browser import BrowserSkill
        import asyncio

        skill = BrowserSkill()
        nav_result = await asyncio.wait_for(
            skill.execute(action="navigate", url=url), timeout=90,
        )
        if not nav_result.success:
            return "", f"Browser navigate error: {nav_result.error}"

        if selector:
            text_result = await asyncio.wait_for(
                skill.execute(action="get_text", selector=selector), timeout=30,
            )
        else:
            text_result = await asyncio.wait_for(
                skill.execute(action="get_text"), timeout=30,
            )

        if not text_result.success:
            return "", f"Browser get_text error: {text_result.error}"

        text = text_result.output
        if len(text) < 30:
            return "", "Browser returned very little text."

        return text, None
    except Exception as e:
        return "", f"Browser error: {e}"


def _get_tz():
    """Get configured timezone."""
    import os
    try:
        from zoneinfo import ZoneInfo
        tz_name = os.environ.get("TIMEZONE", "America/Santiago")
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def _now_local():
    return datetime.now(_get_tz())


class CreateMonitorSkill(SkillBase):
    """Create a website monitor."""

    def __init__(self, memory: MemoryManager):
        self._memory = memory

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="create_monitor",
            description="Monitor a website for changes, keywords, or new content",
            params=[
                SkillParam(name="url", description="URL to monitor"),
                SkillParam(
                    name="monitor_type", description="Type: change, keyword, new_content",
                    required=False, default="change",
                ),
                SkillParam(name="keyword", description="Keyword to watch for (type=keyword)", required=False, default=""),
                SkillParam(name="selector", description="CSS selector to narrow scope", required=False, default=""),
                SkillParam(name="interval_minutes", description="Check interval in minutes (min 5)", required=False, default="60"),
                SkillParam(name="use_browser", description="Use Chromium for JS-heavy sites (true/false)", required=False, default="false"),
                SkillParam(name="label", description="Human-friendly label", required=False, default=""),
            ],
            category="monitoring",
            timeout_seconds=60.0,
        )

    async def execute(self, **kwargs) -> SkillResult:
        url = kwargs.get("url", "").strip()
        if not url:
            return SkillResult(skill_name="create_monitor", success=False, output="", error="URL required.")

        # Normalize URL
        if not url.startswith("http"):
            url = f"https://{url}"

        monitor_type = kwargs.get("monitor_type", "change").strip().lower()
        if monitor_type not in ("change", "keyword", "new_content"):
            monitor_type = "change"

        keyword = kwargs.get("keyword", "").strip()
        if monitor_type == "keyword" and not keyword:
            return SkillResult(
                skill_name="create_monitor", success=False, output="",
                error="Keyword required for monitor_type=keyword.",
            )

        selector = kwargs.get("selector", "").strip()

        try:
            interval = max(5, int(kwargs.get("interval_minutes", "60")))
        except (ValueError, TypeError):
            interval = 60

        use_browser = str(kwargs.get("use_browser", "false")).lower() in ("true", "1", "yes")
        label = kwargs.get("label", "").strip()
        if not label:
            # Auto-label from URL
            from urllib.parse import urlparse
            parsed = urlparse(url)
            label = parsed.netloc + (parsed.path if parsed.path != "/" else "")

        chat_id = kwargs.get("chat_id", "")
        local_now = _now_local()

        # Initial fetch to capture baseline
        text, error = await fetch_page_content(url, selector, use_browser)
        if error:
            # Still create the monitor but warn
            content_hash = ""
            snippet = ""
            warning = f"\nWarning: could not access the site ({error}). Monitor was created but the first check may fail."
        else:
            content_hash = _content_hash(text)
            snippet = text[:500]
            warning = ""

        content = {
            "url": url,
            "monitor_type": monitor_type,
            "keyword": keyword,
            "selector": selector,
            "interval_minutes": interval,
            "chat_id": chat_id,
            "created_at": local_now.isoformat(),
            "last_checked_at": local_now.isoformat(),
            "last_content_hash": content_hash,
            "last_content_snippet": snippet,
            "check_count": 1 if content_hash else 0,
            "change_count": 0,
            "consecutive_errors": 0,
            "last_error": error or "",
            "label": label,
            "use_browser": use_browser,
        }

        async with async_session() as session:
            await self._memory.store_memory(
                session,
                memory_type=MemoryType.WORKING,
                content=content,
                summary=f"Monitor: {label} ({monitor_type})",
                tags=["monitor", "active"],
            )

        # Build response
        type_desc = {
            "change": "content changes",
            "keyword": f"appearance of \"{keyword}\"",
            "new_content": "new content",
        }
        desc = type_desc.get(monitor_type, monitor_type)

        return SkillResult(
            skill_name="create_monitor",
            success=True,
            output=(
                f"Monitor creado: {label}\n"
                f"URL: {url}\n"
                f"Tipo: {desc}\n"
                f"Intervalo: cada {interval} minutos"
                f"{warning}"
            ),
        )


class ListMonitorsSkill(SkillBase):
    """List active web monitors."""

    def __init__(self, memory: MemoryManager):
        self._memory = memory

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="list_monitors",
            description="List all active website monitors",
            params=[],
            category="monitoring",
            timeout_seconds=10.0,
        )

    async def execute(self, **kwargs) -> SkillResult:
        async with async_session() as session:
            entries = await self._memory.retrieve(
                session,
                MemoryQuery(memory_type=MemoryType.WORKING, tags=["monitor", "active"], limit=20),
            )

        if not entries:
            return SkillResult(
                skill_name="list_monitors", success=True,
                output="No hay monitores activos.",
            )

        lines = [f"Monitores activos ({len(entries)}):\n"]
        for i, entry in enumerate(entries, 1):
            url = entry.content.get("url", "?")
            mtype = entry.content.get("monitor_type", "change")
            interval = entry.content.get("interval_minutes", 60)
            checks = entry.content.get("check_count", 0)
            changes = entry.content.get("change_count", 0)
            last = entry.content.get("last_checked_at", "nunca")
            if last and last != "nunca":
                last = last[:19]
            label = entry.content.get("label", url)
            keyword = entry.content.get("keyword", "")

            line = f"{i}. {label}"
            if keyword:
                line += f" (keyword: \"{keyword}\")"
            line += f"\n   {url}"
            line += f"\n   Tipo: {mtype} | Cada {interval}min | Checks: {checks} | Cambios: {changes}"
            line += f"\n   Último check: {last}"
            line += f"\n   ID: {entry.id[:8]}"
            lines.append(line)

        return SkillResult(
            skill_name="list_monitors", success=True,
            output="\n".join(lines),
        )


class RemoveMonitorSkill(SkillBase):
    """Remove a web monitor."""

    def __init__(self, memory: MemoryManager):
        self._memory = memory

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="remove_monitor",
            description="Remove a website monitor by URL or ID",
            params=[
                SkillParam(name="target", description="URL or ID prefix of the monitor to remove"),
            ],
            category="monitoring",
            timeout_seconds=10.0,
        )

    async def execute(self, **kwargs) -> SkillResult:
        target = kwargs.get("target", "").strip()
        if not target:
            return SkillResult(
                skill_name="remove_monitor", success=False, output="",
                error="Specify a URL or ID prefix.",
            )

        async with async_session() as session:
            entries = await self._memory.retrieve(
                session,
                MemoryQuery(memory_type=MemoryType.WORKING, tags=["monitor", "active"], limit=50),
            )
            # Also check error/paused monitors
            error_entries = await self._memory.retrieve(
                session,
                MemoryQuery(memory_type=MemoryType.WORKING, tags=["monitor", "error"], limit=50),
            )
            entries.extend(error_entries)

        found = None
        for entry in entries:
            if entry.id.startswith(target) or target in entry.content.get("url", ""):
                found = entry
                break

        if not found:
            return SkillResult(
                skill_name="remove_monitor", success=False, output="",
                error=f"Monitor no encontrado: {target}",
            )

        label = found.content.get("label", found.content.get("url", "?"))
        async with async_session() as session:
            await self._memory.delete(session, MemoryType.WORKING, found.id)

        return SkillResult(
            skill_name="remove_monitor", success=True,
            output=f"Monitor eliminado: {label}",
        )
