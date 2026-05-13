"""Browser connector — STRICT MODE.

Provides controlled browser automation using Selenium + headless Chromium.
ONLY allowlisted actions are exposed. No arbitrary browsing.

All actions are capability-gated:
    open_url       — Navigate to URL (optional domain allowlist check)  (MEDIUM)
    click          — Click element by CSS selector                       (HIGH)
    type_text      — Type text into an input field                       (HIGH)
    screenshot     — Take screenshot, return base64 PNG                  (MEDIUM)
    extract_text   — Extract text by CSS selector                        (MEDIUM)
    scroll         — Scroll page up/down/to element                      (LOW)
    get_title      — Get page title and URL                              (LOW)

Secrets:
    allowed_domains — Comma-separated list of allowed domains (optional).
                      If empty, all domains are permitted (still MEDIUM risk).
                      Example: "example.com,docs.python.org"
                      Stored as a secret so operators can configure per-env.
"""
from __future__ import annotations

import base64
import io
from typing import Any

import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_TIMEOUT_S = 20


class BrowserControlledConnector(BaseConnector):
    """STRICT MODE browser connector — allowlist-gated Selenium automation."""

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="browser-controlled", version="1.0.0", name="Browser (Strict)", category="tools",
            description=(
                "Headless browser automation in STRICT MODE. "
                "Only allowlisted actions: open_url, click, type_text, screenshot, extract_text, scroll, get_title. "
                "Domain allowlist configurable per environment."
            ),
            capabilities=["navigate_urls", "click_elements", "type_text", "screenshot", "extract_text", "scroll"],
            risk_level=RiskLevel.HIGH,
            required_secrets=[],
            config_schema={
                "type": "object",
                "properties": {
                    "allowed_domains": {
                        "type": "string",
                        "description": "Comma-separated allowlisted domains. Empty = all allowed.",
                    }
                },
            },
            rate_limits={
                "open_url":     RateLimit(requests_per_minute=20),
                "click":        RateLimit(requests_per_minute=30),
                "type_text":    RateLimit(requests_per_minute=30),
                "screenshot":   RateLimit(requests_per_minute=20),
                "extract_text": RateLimit(requests_per_minute=30),
                "scroll":       RateLimit(requests_per_minute=60),
                "get_title":    RateLimit(requests_per_minute=30),
            },
            actions=[
                ActionSpec(id="open_url", description="Navigate browser to a URL",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("url", "string", "Target URL (must be in allowed_domains if configured)", required=True),
                        ParamSpec("wait_seconds", "integer", "Seconds to wait for page load (default 3)", required=False),
                    ]),
                ActionSpec(id="click", description="Click an element by CSS selector",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[
                        ParamSpec("selector", "string", "CSS selector of element to click", required=True),
                        ParamSpec("wait_seconds", "integer", "Seconds to wait after click (default 1)", required=False),
                    ]),
                ActionSpec(id="type_text", description="Type text into an input field by CSS selector",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[
                        ParamSpec("selector", "string", "CSS selector of input field", required=True),
                        ParamSpec("text", "string", "Text to type", required=True),
                        ParamSpec("clear_first", "boolean", "Clear field before typing (default true)", required=False),
                    ]),
                ActionSpec(id="screenshot", description="Take a screenshot of the current page",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("selector", "string", "CSS selector to capture specific element (optional)", required=False),
                    ]),
                ActionSpec(id="extract_text", description="Extract visible text from the page or a specific element",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("selector", "string", "CSS selector (optional, default: body)", required=False),
                        ParamSpec("max_chars", "integer", "Max chars to return (default 5000)", required=False),
                    ]),
                ActionSpec(id="scroll", description="Scroll the page or to a specific element",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("direction", "string", "up|down|top|bottom (default down)", required=False),
                        ParamSpec("pixels", "integer", "Pixels to scroll (default 500)", required=False),
                        ParamSpec("selector", "string", "Scroll to element by CSS selector (optional)", required=False),
                    ]),
                ActionSpec(id="get_title", description="Get the current page title and URL",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
            ],
            homepage="https://www.selenium.dev",
            docs_url="https://www.selenium.dev/documentation/",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        # Domain allowlist from vault (optional)
        allowed_raw = secrets.get("allowed_domains", "")
        allowed_domains = [d.strip() for d in allowed_raw.split(",") if d.strip()] if allowed_raw else []

        if action == "open_url":
            url = params.get("url", "")
            if not url:
                return self.err("url is required")
            if not url.startswith(("http://", "https://")):
                url = f"https://{url}"
            if allowed_domains and not self._domain_allowed(url, allowed_domains):
                return self.err(f"Domain not in allowlist. Allowed: {allowed_domains}")
            return await self._open_url(url, int(params.get("wait_seconds") or 3))

        if action == "click":
            return await self._click(params.get("selector", ""), int(params.get("wait_seconds") or 1))

        if action == "type_text":
            return await self._type_text(params.get("selector", ""), params.get("text", ""),
                                          params.get("clear_first", True))

        if action == "screenshot":
            return await self._screenshot(params.get("selector"))

        if action == "extract_text":
            max_chars = min(int(params.get("max_chars") or 5000), 20000)
            return await self._extract_text(params.get("selector") or "body", max_chars)

        if action == "scroll":
            return await self._scroll(params.get("direction") or "down",
                                       int(params.get("pixels") or 500),
                                       params.get("selector"))

        if action == "get_title":
            return await self._get_title()

        return self.err(f"Unknown action: {action}")

    def _domain_allowed(self, url: str, allowed: list[str]) -> bool:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        return any(host == d or host.endswith(f".{d}") for d in allowed)

    async def _get_driver(self):
        import asyncio
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1280,800")
        loop = asyncio.get_event_loop()
        driver = await loop.run_in_executor(None, lambda: webdriver.Chrome(options=opts))
        return driver

    async def _open_url(self, url: str, wait: int) -> dict:
        import asyncio
        try:
            driver = await self._get_driver()
            try:
                await asyncio.get_event_loop().run_in_executor(None, driver.get, url)
                await asyncio.sleep(wait)
                title = driver.title
                current_url = driver.current_url
                # Store driver reference in a simple way for chained calls
                # (Note: for simplicity, each action creates a new driver session)
                return self.ok({"url": current_url, "title": title, "status": "loaded"})
            finally:
                await asyncio.get_event_loop().run_in_executor(None, driver.quit)
        except Exception as exc:
            return self.err(f"Browser error: {exc}")

    async def _click(self, selector: str, wait: int) -> dict:
        import asyncio
        if not selector:
            return self.err("selector is required")
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            driver = await self._get_driver()
            try:
                loop = asyncio.get_event_loop()
                def _do():
                    el = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                    el.click()
                await loop.run_in_executor(None, _do)
                await asyncio.sleep(wait)
                return self.ok({"clicked": selector})
            finally:
                await loop.run_in_executor(None, driver.quit)
        except Exception as exc:
            return self.err(f"Click failed: {exc}")

    async def _type_text(self, selector: str, text: str, clear_first: bool = True) -> dict:
        import asyncio
        if not selector or not text:
            return self.err("selector and text are required")
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            driver = await self._get_driver()
            try:
                loop = asyncio.get_event_loop()
                def _do():
                    el = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
                    if clear_first:
                        el.clear()
                    el.send_keys(text)
                await loop.run_in_executor(None, _do)
                return self.ok({"typed": len(text), "selector": selector})
            finally:
                await loop.run_in_executor(None, driver.quit)
        except Exception as exc:
            return self.err(f"Type failed: {exc}")

    async def _screenshot(self, selector: str | None) -> dict:
        import asyncio
        try:
            from selenium.webdriver.common.by import By
            driver = await self._get_driver()
            try:
                loop = asyncio.get_event_loop()
                if selector:
                    def _do():
                        el = driver.find_element(By.CSS_SELECTOR, selector)
                        return el.screenshot_as_base64
                    png_b64 = await loop.run_in_executor(None, _do)
                else:
                    png_b64 = await loop.run_in_executor(None, lambda: driver.get_screenshot_as_base64())
                return self.ok({"screenshot_base64": png_b64, "format": "png"})
            finally:
                await loop.run_in_executor(None, driver.quit)
        except Exception as exc:
            return self.err(f"Screenshot failed: {exc}")

    async def _extract_text(self, selector: str, max_chars: int) -> dict:
        import asyncio
        try:
            from selenium.webdriver.common.by import By
            driver = await self._get_driver()
            try:
                loop = asyncio.get_event_loop()
                def _do():
                    els = driver.find_elements(By.CSS_SELECTOR, selector)
                    return " ".join(el.text for el in els if el.text)
                text = await loop.run_in_executor(None, _do)
                return self.ok({"text": text[:max_chars], "total_chars": len(text)})
            finally:
                await loop.run_in_executor(None, driver.quit)
        except Exception as exc:
            return self.err(f"Extract failed: {exc}")

    async def _scroll(self, direction: str, pixels: int, selector: str | None) -> dict:
        import asyncio
        try:
            driver = await self._get_driver()
            try:
                loop = asyncio.get_event_loop()
                def _do():
                    if selector:
                        from selenium.webdriver.common.by import By
                        el = driver.find_element(By.CSS_SELECTOR, selector)
                        driver.execute_script("arguments[0].scrollIntoView(true);", el)
                    elif direction == "top":
                        driver.execute_script("window.scrollTo(0, 0);")
                    elif direction == "bottom":
                        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    elif direction == "up":
                        driver.execute_script(f"window.scrollBy(0, -{pixels});")
                    else:
                        driver.execute_script(f"window.scrollBy(0, {pixels});")
                await loop.run_in_executor(None, _do)
                return self.ok({"scrolled": direction})
            finally:
                await loop.run_in_executor(None, driver.quit)
        except Exception as exc:
            return self.err(f"Scroll failed: {exc}")

    async def _get_title(self) -> dict:
        import asyncio
        try:
            driver = await self._get_driver()
            try:
                loop = asyncio.get_event_loop()
                title = await loop.run_in_executor(None, lambda: driver.title)
                url   = await loop.run_in_executor(None, lambda: driver.current_url)
                return self.ok({"title": title, "url": url})
            finally:
                await loop.run_in_executor(None, driver.quit)
        except Exception as exc:
            return self.err(f"Browser error: {exc}")
