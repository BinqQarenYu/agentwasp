"""Browser Smart Navigate skill.

Human-like page navigation WITHOUT any vision model. Pure DOM/JavaScript-based.

Capabilities:
  - Open URL and dismiss overlays
  - Scroll step-by-step (viewport height per step)
  - Detect page bottom (scrollTop + innerHeight >= scrollHeight)
  - Detect lazy-loaded content (height increase after scroll + wait)
  - Detect height stagnation (stop if no growth for N consecutive steps)
  - Optionally click "load more" / "ver más" / pagination next buttons
  - Optional screenshot at each step (no vision model: screenshots are output only)
  - Hard 30-second wall-clock timeout on the entire loop
  - Max 30 scroll steps hard ceiling

Separation of responsibilities:
  - This skill handles ONLY navigation + interaction.
  - It does NOT extract structured text (use browser_deep_scrape for that).
  - It does NOT call any other skill internally.

Output: JSON string with keys:
  total_scrolls    — number of scroll steps completed
  screenshots      — list of absolute paths (empty if capture=false)
  final_url        — URL at end of navigation
  final_title      — page title at end
  status           — "complete" (bottom reached) | "partial" (limit/timeout)
  stop_reason      — why the loop stopped
  load_more_clicks — number of successful "load more" / pagination clicks
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time

import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

# Shared browser infrastructure — low-level helpers only, NOT BrowserSkill
from .browser import (
    SCREENSHOT_DIR,
    SCREENSHOT_SHARED,
    _dismiss_overlays,
    _get_driver,
    _normalize_url,
    _wait_for_page,
)

logger = structlog.get_logger()

# Hard limits
_MAX_STEPS = 30
_WALL_TIMEOUT_S = 30.0          # seconds for the entire scroll loop
_STALE_HEIGHT_LIMIT = 2         # consecutive no-growth checks before stop
_WAIT_AFTER_LOAD_MORE_S = 1.5  # wait after clicking load more for content

# JavaScript: find and click a "load more" / pagination "next" button.
# Returns true if clicked, false otherwise. No vision — pure DOM text/attribute matching.
_LOAD_MORE_JS = """
(function() {
    var ACCEPT = [
        'load more', 'ver más', 'ver mas', 'cargar más', 'cargar mas',
        'mostrar más', 'mostrar mas', 'show more', 'more results',
        'más resultados', 'mas resultados', 'load more results',
        'ver más resultados', 'siguiente', 'next page', 'next'
    ];

    // Check if element is visually interactive (not hidden, not disabled)
    function isClickable(el) {
        if (!el || el.offsetParent === null) return false;
        var style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
        var rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }

    // 1. Buttons, links, divs/spans with matching text
    var candidates = document.querySelectorAll(
        'button, a, [role="button"], input[type="button"], input[type="submit"]'
    );
    for (var i = 0; i < candidates.length; i++) {
        var el = candidates[i];
        var raw = (el.innerText || el.textContent || el.value || '').trim().toLowerCase();
        for (var j = 0; j < ACCEPT.length; j++) {
            if (raw === ACCEPT[j] || raw.startsWith(ACCEPT[j])) {
                if (isClickable(el)) {
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    return 'clicked:text:' + raw.slice(0, 40);
                }
            }
        }
    }

    // 2. Pagination: rel="next" link, .pagination .next, aria-label="Next page"
    var pagination = document.querySelectorAll(
        'a[rel="next"], ' +
        '.pagination .next > a, .pagination .next, ' +
        '.pagination-next a, .pagination-next, ' +
        '[aria-label="Next page"], [aria-label="Siguiente página"], ' +
        '.pager-next a, .pager-next, ' +
        'li.next > a, li.next-page > a'
    );
    for (var k = 0; k < pagination.length; k++) {
        var pg = pagination[k];
        if (isClickable(pg)) {
            pg.scrollIntoView({block: 'center'});
            pg.click();
            return 'clicked:pagination';
        }
    }

    // 3. data-* or aria-label hints
    var hints = document.querySelectorAll(
        '[data-action*="load-more"], [data-action*="loadMore"], ' +
        '[aria-label*="load more"], [aria-label*="ver más"]'
    );
    for (var m = 0; m < hints.length; m++) {
        var hint = hints[m];
        if (isClickable(hint)) {
            hint.scrollIntoView({block: 'center'});
            hint.click();
            return 'clicked:aria-hint';
        }
    }

    return null;
})();
"""

# JavaScript: get scroll metrics in one call (minimise round-trips)
_SCROLL_METRICS_JS = """
(function() {
    return {
        scrollTop: window.pageYOffset || document.documentElement.scrollTop || 0,
        innerHeight: window.innerHeight || document.documentElement.clientHeight || 768,
        scrollHeight: Math.max(
            document.body.scrollHeight || 0,
            document.documentElement.scrollHeight || 0
        )
    };
})();
"""


def _take_screenshot(driver, session_name: str, chat_id: str = "") -> str:
    """Capture current viewport via CDP. Returns saved path or '' on failure.

    Self-contained — does not call any other skill or import from sibling skills.
    No vision model involved: screenshots are raw pixel output only.
    """
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    os.makedirs(SCREENSHOT_SHARED, exist_ok=True)

    ts = int(time.time() * 1000)
    fname = f"screenshot_{ts}.png"
    saved_path = os.path.join(SCREENSHOT_DIR, fname)
    shared_path = os.path.join(SCREENSHOT_SHARED, fname)

    try:
        cdp = driver.execute_cdp_cmd("Page.captureScreenshot", {"format": "png"})
        raw = base64.b64decode(cdp["data"])
        with open(saved_path, "wb") as f:
            f.write(raw)
        with open(shared_path, "wb") as f:
            f.write(raw)
    except Exception:
        try:
            driver.save_screenshot(saved_path)
            import shutil
            shutil.copy2(saved_path, shared_path)
        except Exception:
            return ""

    # Visual memory: enqueue metadata for the async consumer to process.
    # See rationale in browser.py — direct DB writes from worker threads break
    # SQLAlchemy's async pool.
    try:
        from .browser import _visual_memory_queue as _vmq
        _vmq.put_nowait({
            "file_path": saved_path,
            "url": getattr(driver, 'current_url', ''),
            "page_title": getattr(driver, 'title', '') or "",
            "description": "smart_navigate scroll frame",
            "tags": ["browser", "smart_navigate", "scroll"],
            "chat_id": chat_id,
        })
    except Exception:
        pass

    return saved_path


async def _do_smart_navigate(
    url: str,
    session: str,
    click_load_more: bool,
    max_steps: int,
    wait_ms: int,
    capture: bool,
    chat_id: str,
    user_id: str,
) -> str:
    """Blocking implementation. Returns JSON string."""
    url = _normalize_url(url)
    if not url:
        return json.dumps({"status": "error", "error": "url is required"})

    session = session or "nav1"
    max_steps = min(int(max_steps), _MAX_STEPS)
    wait_s = max(0.3, min(0.8, wait_ms / 1000.0))  # clamp 300–800 ms

    screenshots: list[str] = []
    stop_reason = "max_steps"
    load_more_clicks = 0

    # ── Navigation ─────────────────────────────────────────────────────────────
    try:
        driver = await asyncio.to_thread(_get_driver, session)
        await asyncio.to_thread(driver.get, url)
    except Exception as e:
        return json.dumps({"status": "error", "error": f"navigation failed: {e}"})

    await asyncio.to_thread(_wait_for_page, driver)
    await asyncio.sleep(0.8)

    # ── Overlay dismissal ───────────────────────────────────────────────────────
    await asyncio.to_thread(_dismiss_overlays, session)
    await asyncio.sleep(0.5)
    await asyncio.to_thread(_dismiss_overlays, session)  # second pass — some banners load after JS renders
    await asyncio.sleep(0.3)

    try:
        final_url = await asyncio.to_thread(getattr, driver, 'current_url')
        final_title = await asyncio.to_thread(getattr, driver, 'title') or ""
    except Exception:
        final_url = url
        final_title = ""

    if final_url in ("data:,", "about:blank", ""):
        return json.dumps({
            "status": "error",
            "error": f"anti-bot block at {url}",
            "final_url": url,
        })

    # ── Scroll to top ───────────────────────────────────────────────────────────
    try:
        await asyncio.to_thread(driver.execute_script, "window.scrollTo(0, 0);")
        await asyncio.sleep(0.3)
    except Exception:
        pass

    # ── Initial metrics ─────────────────────────────────────────────────────────
    try:
        m = await asyncio.to_thread(driver.execute_script, _SCROLL_METRICS_JS)
        vh = m.get("innerHeight") or 768
        scroll_height = m.get("scrollHeight") or vh
    except Exception:
        vh = 768
        scroll_height = vh

    step_px = max(vh - 80, 200)  # 80px overlap between frames for continuity

    # ── Scroll loop ─────────────────────────────────────────────────────────────
    loop_start = time.monotonic()
    stale_count = 0
    prev_scroll_height = scroll_height

    for step in range(max_steps):
        # ── Wall-clock timeout guard ──────────────────────────────────────────
        elapsed = time.monotonic() - loop_start
        if elapsed >= _WALL_TIMEOUT_S:
            stop_reason = f"timeout:{elapsed:.1f}s"
            break

        # ── Capture BEFORE scroll (shows what was visible at this position) ───
        if capture:
            path = await asyncio.to_thread(_take_screenshot, driver, session, chat_id)
            if path:
                screenshots.append(path)

        # ── Scroll by viewport height ─────────────────────────────────────────
        target_y = step * step_px
        try:
            await asyncio.to_thread(driver.execute_script, f"window.scrollTo(0, {target_y});")
        except Exception:
            stop_reason = "scroll_error"
            break

        await asyncio.sleep(wait_s)  # wait for lazy-loaded content (300–800 ms)

        # ── Re-read metrics after wait ────────────────────────────────────────
        try:
            m = await asyncio.to_thread(driver.execute_script, _SCROLL_METRICS_JS)
            scroll_top = m.get("scrollTop", 0)
            scroll_height = m.get("scrollHeight", vh)
        except Exception:
            stop_reason = "metrics_error"
            break

        # ── Bottom detection ──────────────────────────────────────────────────
        at_bottom = (scroll_top + vh) >= (scroll_height - 10)

        if at_bottom:
            # Capture the final bottom frame
            if capture:
                path = await asyncio.to_thread(_take_screenshot, driver, session, chat_id)
                if path:
                    screenshots.append(path)

            # Try "load more" / pagination if enabled
            if click_load_more:
                try:
                    click_result = await asyncio.to_thread(driver.execute_script, _LOAD_MORE_JS)
                except Exception:
                    click_result = None

                if click_result:
                    load_more_clicks += 1
                    await asyncio.sleep(_WAIT_AFTER_LOAD_MORE_S)
                    # Re-check height — did new content load?
                    try:
                        m2 = await asyncio.to_thread(driver.execute_script, _SCROLL_METRICS_JS)
                        new_h = m2.get("scrollHeight", scroll_height)
                    except Exception:
                        new_h = scroll_height

                    if new_h > scroll_height + 50:
                        # New content loaded — continue scrolling
                        scroll_height = new_h
                        stale_count = 0
                        logger.info(
                            "browser_smart_navigate.load_more_success",
                            click=click_result,
                            new_height=new_h,
                            step=step,
                        )
                        continue  # Resume scroll loop

            # Truly at bottom with nothing more to load
            stop_reason = "bottom_reached"
            break

        # ── Height stagnation detection (infinite scroll guard) ───────────────
        if scroll_height <= prev_scroll_height:
            stale_count += 1
            if stale_count >= _STALE_HEIGHT_LIMIT:
                stop_reason = f"height_stagnant:{scroll_height}px"
                break
        else:
            stale_count = 0

        prev_scroll_height = scroll_height

    else:
        stop_reason = "max_steps"

    # ── Scroll back to top when done ────────────────────────────────────────────
    try:
        await asyncio.to_thread(driver.execute_script, "window.scrollTo(0, 0);")
    except Exception:
        pass

    # ── Capture final state of page ─────────────────────────────────────────────
    try:
        final_url = await asyncio.to_thread(getattr, driver, 'current_url')
        final_title = (await asyncio.to_thread(getattr, driver, 'title')) or final_title
    except Exception:
        pass

    status = "complete" if stop_reason == "bottom_reached" else "partial"
    total_scrolls = len(screenshots) if capture else (
        min(step + 1, max_steps) if 'step' in dir() else 0
    )

    result = {
        "status": status,
        "total_scrolls": total_scrolls,
        "screenshots": screenshots,
        "final_url": final_url,
        "final_title": final_title,
        "stop_reason": stop_reason,
        "load_more_clicks": load_more_clicks,
        "elapsed_s": round(time.monotonic() - loop_start, 2),
    }

    logger.info(
        "browser_smart_navigate.complete",
        url=final_url,
        status=status,
        total_scrolls=total_scrolls,
        stop_reason=stop_reason,
        load_more_clicks=load_more_clicks,
        screenshots=len(screenshots),
    )

    return json.dumps(result, ensure_ascii=False)


class BrowserSmartNavigateSkill(SkillBase):
    """Human-like page navigation: scroll, detect content, click load-more. No vision model."""

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="browser_smart_navigate",
            description=(
                "Navigate a webpage step-by-step like a human: scroll viewport by viewport, "
                "wait for lazy-loaded content, detect page bottom, and optionally click "
                "'load more' / 'ver más' / pagination buttons to reveal more content. "
                "NO vision model used — pure DOM/JavaScript detection. "
                "Use when the user says: 'navega', 'scroll inteligente', 'recorre la página', "
                "'baja hasta el final', 'carga todo el contenido', 'navega paso a paso'. "
                "Returns JSON: {total_scrolls, screenshots, final_url, status, stop_reason, load_more_clicks}. "
                "status='complete' means bottom reached; 'partial' means limit/timeout hit."
            ),
            params=[
                SkillParam(
                    name="url",
                    param_type=ParamType.STRING,
                    description="URL to navigate",
                ),
                SkillParam(
                    name="session",
                    param_type=ParamType.STRING,
                    required=False,
                    default="nav1",
                    description="Browser session name (persists cookies/login). Default: 'nav1'",
                ),
                SkillParam(
                    name="click_load_more",
                    param_type=ParamType.STRING,
                    required=False,
                    default="true",
                    description=(
                        "Whether to click 'load more' / 'ver más' / pagination buttons "
                        "when the bottom is reached. true/false. Default: true"
                    ),
                ),
                SkillParam(
                    name="max_steps",
                    param_type=ParamType.INTEGER,
                    required=False,
                    default="30",
                    description="Maximum scroll steps (hard ceiling: 30). Default: 30",
                ),
                SkillParam(
                    name="wait_ms",
                    param_type=ParamType.INTEGER,
                    required=False,
                    default="500",
                    description=(
                        "Milliseconds to wait after each scroll for lazy-loaded content "
                        "(300–800, clamped). Default: 500"
                    ),
                ),
                SkillParam(
                    name="capture",
                    param_type=ParamType.STRING,
                    required=False,
                    default="true",
                    description=(
                        "Whether to take a screenshot at each scroll step. "
                        "true/false. Default: true. "
                        "Set false to navigate only without capturing images."
                    ),
                ),
            ],
            category="web",
            timeout_seconds=90.0,   # outer timeout > inner 30s loop to allow cleanup
            capability_level="monitored",
        )

    async def execute(
        self,
        url: str = "",
        session: str = "nav1",
        click_load_more: str = "true",
        max_steps: int = 30,
        wait_ms: int = 500,
        capture: str = "true",
        **kwargs,
    ) -> SkillResult:
        chat_id = kwargs.get("chat_id", "")
        user_id = kwargs.get("user_id", "")

        if not url:
            return SkillResult(
                skill_name="browser_smart_navigate",
                success=False,
                output="",
                error="url is required",
            )

        # Normalise bool-as-string params (SkillCall.arguments is dict[str, str])
        _click_lm = str(click_load_more).lower() not in ("false", "0", "no")
        _capture = str(capture).lower() not in ("false", "0", "no")

        try:
            max_steps = int(max_steps)
        except (TypeError, ValueError):
            max_steps = 30
        try:
            wait_ms = int(wait_ms)
        except (TypeError, ValueError):
            wait_ms = 500

        try:
            output = await _do_smart_navigate(
                url, session, _click_lm, max_steps, wait_ms, _capture,
                chat_id, user_id,
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
            # Treat as success unless output is an error JSON
            try:
                parsed = json.loads(output)
                success = parsed.get("status") != "error"
                err = parsed.get("error", "") if not success else ""
            except Exception:
                success = True
                err = ""

            return SkillResult(
                skill_name="browser_smart_navigate",
                success=success,
                output=output if success else "",
                error=err,
            )
        except Exception as e:
            logger.exception("browser_smart_navigate.error", url=url)
            return SkillResult(
                skill_name="browser_smart_navigate",
                success=False,
                output="",
                error=str(e),
            )
