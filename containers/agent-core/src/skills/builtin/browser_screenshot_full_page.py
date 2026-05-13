"""Browser Screenshot Full Page skill.

Captures a complete webpage by scrolling step-by-step and taking screenshots at
each position. Handles lazy loading, detects duplicate frames, and stops at
page bottom or when MAX_SCREENSHOTS is reached.

Separation of responsibilities:
  - This skill handles ONLY capture. It never extracts structured text.
  - It reuses low-level browser.py infrastructure (sessions, driver management).
  - It does NOT call any other skill internally.

Output format (plaintext, one line per entry):
  screenshots: /data/screenshots/screenshot_X.png, /data/screenshots/screenshot_Y.png
  total: N
  url: <url>
  title: <page title>
  [note: <any warning>]
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time

import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

# Import shared browser infrastructure (low-level helpers only, NOT BrowserSkill)
from .browser import (
    SCREENSHOT_DIR,
    SCREENSHOT_SHARED,
    _dismiss_overlays,
    _get_driver,
    _normalize_url,
    _wait_for_page,
)

logger = structlog.get_logger()

MAX_SCREENSHOTS = 30
_DEDUP_CONSECUTIVE_LIMIT = 2  # Stop after N consecutive identical frames


def _hash_png(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _capture_frame(driver, session_name: str, chat_id: str = "") -> tuple[str, bytes]:
    """Capture a single frame via CDP. Returns (saved_path, raw_bytes)."""
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    os.makedirs(SCREENSHOT_SHARED, exist_ok=True)

    ts = int(time.time() * 1000)
    fname = f"screenshot_{ts}.png"
    saved_path = os.path.join(SCREENSHOT_DIR, fname)
    shared_path = os.path.join(SCREENSHOT_SHARED, fname)

    raw_bytes = b""
    try:
        cdp_result = driver.execute_cdp_cmd("Page.captureScreenshot", {"format": "png"})
        raw_bytes = base64.b64decode(cdp_result["data"])
        with open(saved_path, "wb") as f:
            f.write(raw_bytes)
        with open(shared_path, "wb") as f:
            f.write(raw_bytes)
    except Exception:
        try:
            driver.save_screenshot(saved_path)
            with open(saved_path, "rb") as f:
                raw_bytes = f.read()
            import shutil
            shutil.copy2(saved_path, shared_path)
        except Exception as e:
            return "", b""

    # Visual memory: enqueue metadata for the async consumer to process.
    # get_event_loop().create_task() from a worker thread is unsafe — it either
    # picks up the wrong loop or raises. The async wrapper drains the queue
    # after asyncio.to_thread returns and schedules stores on the main loop.
    try:
        from .browser import _visual_memory_queue as _vmq
        _vmq.put_nowait({
            "file_path": saved_path,
            "url": driver.current_url,
            "page_title": driver.title or "",
            "description": "full-page scroll capture",
            "tags": ["screenshot", "full-page", "scroll"],
            "chat_id": chat_id,
        })
    except Exception:
        pass

    return saved_path, raw_bytes


def _do_screenshot_full_page(
    url: str,
    session: str,
    wait_ms: int,
    scroll_step: int,
    chat_id: str,
    user_id: str,
) -> str:
    """Blocking implementation (runs in thread via asyncio.to_thread)."""
    url = _normalize_url(url)
    if not url:
        return "error: url is required"

    session = session or "fullpage1"
    wait_s = max(0.3, min(0.8, wait_ms / 1000.0))  # clamp 300-800ms → 0.3-0.8s

    # ── Navigation ────────────────────────────────────────────────────────────
    try:
        driver = _get_driver(session)
        driver.get(url)
    except Exception as e:
        return f"error: navigation failed — {e}"

    _wait_for_page(driver)
    time.sleep(0.8)

    # ── Overlay dismissal ─────────────────────────────────────────────────────
    accepted = _dismiss_overlays(session)
    if accepted:
        time.sleep(0.8)
    # Second pass — some overlays appear after initial JS render
    _dismiss_overlays(session)
    time.sleep(0.3)

    # ── Get page title + reset to top ─────────────────────────────────────────
    try:
        page_title = driver.title or ""
        page_url = driver.current_url
    except Exception:
        page_title = ""
        page_url = url

    if page_url in ("data:,", "about:blank", ""):
        return f"error: browser blocked by anti-bot at {url}"

    try:
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.3)
    except Exception:
        pass

    # ── Determine scroll step ─────────────────────────────────────────────────
    try:
        vh = driver.execute_script("return window.innerHeight") or 768
    except Exception:
        vh = 768

    step = scroll_step if scroll_step > 0 else max(vh - 80, 200)

    # ── Scrolling capture loop ────────────────────────────────────────────────
    screenshots: list[str] = []
    notes: list[str] = []
    prev_hashes: list[str] = []
    consecutive_dupes = 0

    for i in range(MAX_SCREENSHOTS):
        target_y = i * step

        try:
            driver.execute_script(f"window.scrollTo(0, {target_y});")
        except Exception:
            notes.append(f"scroll failed at position {i}")
            break

        time.sleep(wait_s)  # Wait for lazy-load content (300–800ms)

        # Capture frame
        saved_path, raw_bytes = _capture_frame(driver, session, chat_id)
        if not saved_path or not raw_bytes:
            notes.append(f"frame capture failed at position {i}")
            if screenshots:
                break  # Return partial results
            return "error: no screenshots captured"

        # Duplicate frame detection via MD5 hash
        frame_hash = _hash_png(raw_bytes)
        if prev_hashes and frame_hash == prev_hashes[-1]:
            consecutive_dupes += 1
            if consecutive_dupes >= _DEDUP_CONSECUTIVE_LIMIT:
                notes.append(f"stopped early: {_DEDUP_CONSECUTIVE_LIMIT} consecutive identical frames")
                break
        else:
            consecutive_dupes = 0

        prev_hashes.append(frame_hash)
        screenshots.append(saved_path)

        # Check if we've reached the real page bottom
        try:
            scroll_top_after = driver.execute_script(
                "return window.pageYOffset + window.innerHeight"
            ) or 0
            current_scroll_h = driver.execute_script(
                "return document.documentElement.scrollHeight"
            ) or vh
            if scroll_top_after >= current_scroll_h - 10:
                break  # Reached bottom — done
        except Exception:
            pass

    # Scroll back to top when done
    try:
        driver.execute_script("window.scrollTo(0, 0);")
    except Exception:
        pass

    # ── Format output ─────────────────────────────────────────────────────────
    if not screenshots:
        return "error: no screenshots captured"

    paths_str = ", ".join(screenshots)
    lines = [
        f"screenshots: {paths_str}",
        f"total: {len(screenshots)}",
        f"url: {page_url}",
        f"title: {page_title}",
    ]
    for note in notes:
        lines.append(f"note: {note}")

    logger.info(
        "browser_screenshot_full_page.complete",
        url=page_url,
        count=len(screenshots),
        session=session,
    )
    return "\n".join(lines)


class BrowserScreenshotFullPageSkill(SkillBase):
    """Capture a full webpage via step-by-step scrolling with duplicate detection."""

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="browser_screenshot_full_page",
            description=(
                "Capture a COMPLETE webpage by scrolling and screenshotting each section. "
                "Use when the user says: 'captura completa', 'captura toda la página', "
                "'haz scroll y captura', 'full page screenshot', 'todo el sitio'. "
                "Automatically detects duplicate frames (stops when bottom is reached). "
                "Max 30 screenshots. Returns paths to all captured images. "
                "IMPORTANT: After capture, images are sent automatically. "
                "Write ONE short sentence about what was captured — no lists, no paths."
            ),
            params=[
                SkillParam(
                    name="url",
                    param_type=ParamType.STRING,
                    description="URL of the page to capture",
                ),
                SkillParam(
                    name="session",
                    param_type=ParamType.STRING,
                    required=False,
                    default="fullpage1",
                    description="Browser session name (persists cookies). Default: 'fullpage1'",
                ),
                SkillParam(
                    name="wait_ms",
                    param_type=ParamType.INTEGER,
                    required=False,
                    default="500",
                    description="Milliseconds to wait after each scroll for lazy loading (300–800). Default: 500",
                ),
                SkillParam(
                    name="scroll_step",
                    param_type=ParamType.INTEGER,
                    required=False,
                    default="0",
                    description="Scroll step in pixels. 0 = auto (viewport height minus 80px overlap). Default: 0",
                ),
            ],
            category="web",
            timeout_seconds=120.0,
            capability_level="monitored",
        )

    async def execute(
        self,
        url: str = "",
        session: str = "fullpage1",
        wait_ms: int = 500,
        scroll_step: int = 0,
        **kwargs,
    ) -> SkillResult:
        chat_id = kwargs.get("chat_id", "")
        user_id = kwargs.get("user_id", "")

        if not url:
            return SkillResult(
                skill_name="browser_screenshot_full_page",
                success=False,
                output="",
                error="url is required",
            )

        try:
            wait_ms = int(wait_ms)
        except (TypeError, ValueError):
            wait_ms = 500
        try:
            scroll_step = int(scroll_step)
        except (TypeError, ValueError):
            scroll_step = 0

        try:
            result = await asyncio.to_thread(
                _do_screenshot_full_page,
                url, session, wait_ms, scroll_step, chat_id, user_id,
            )
            # Drain visual-memory queue (filled by sync producer in worker thread)
            try:
                import queue as _q
                from .browser import _visual_memory_queue as _vmq
                from ...memory.visual import store_screenshot as _vm_store
                while True:
                    try:
                        _meta = _vmq.get_nowait()
                    except _q.Empty:
                        break
                    asyncio.ensure_future(_vm_store(**_meta))
            except Exception:
                pass
            success = not result.startswith("error:")
            return SkillResult(
                skill_name="browser_screenshot_full_page",
                success=success,
                output=result if success else "",
                error=result if not success else "",
            )
        except Exception as e:
            logger.exception("browser_screenshot_full_page.error", url=url)
            return SkillResult(
                skill_name="browser_screenshot_full_page",
                success=False,
                output="",
                error=str(e),
            )
