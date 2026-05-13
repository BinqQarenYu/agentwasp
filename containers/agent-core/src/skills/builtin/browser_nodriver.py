"""Nodriver-based engine for the browser skill.

Two responsibilities:

1. Stateless one-shot ``navigate_capture()`` used by intent-aware routing
   when a fresh navigate/capture is requested.

2. Stateful session manager (``_SESSIONS`` dict) so that a session that
   begins on nodriver can stay on nodriver for subsequent click / type /
   capture calls — cookies and login state survive in-process.

Why stateful matters
--------------------
Without the session cache, each call would spin up a fresh nodriver
browser and lose all cookies set by the previous call. Form workflows
(navigate → type → click → capture) would break exactly the way they did
in the legacy two-engine setup.

Public API
----------
- ``navigate_capture(url, session_name, ...)`` — one-shot navigate + screenshot + validate
- ``navigate(url, session_name, ...)`` — navigate, leave the browser+tab alive
- ``click(selector, session_name)``
- ``type_text(selector, text, submit, session_name)``
- ``screenshot(selector, session_name, chat_id, user_id, task_hint)``
- ``get_text(selector, session_name)``
- ``scroll(direction, session_name)``
- ``back(session_name)``
- ``close(session_name)``
- ``session_exists(session_name)``
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

_SESSIONS_ROOT = "/data/browser_sessions"
_SCREENSHOT_DIR = "/data/screenshots"
_NAV_TIMEOUT_S = 25.0
_VALIDATE_DELAY_S = 1.5
_DEFAULT_VIEWPORT = (1366, 768)

# In-process session cache: session_name -> {"browser", "tab", "last_used"}
# Tabs survive between calls so cookies/login state persist within a session.
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSIONS_LOCK = asyncio.Lock()
_SESSION_IDLE_TTL_S = 600  # 10 minutes idle → evict


def _profile_dir(session_name: str) -> str:
    """Return the profile dir Selenium uses, prefixed with `nd_` so the two
    libraries don't fight over the same SQLite cookie locks. Cookies persist
    across calls within the same session_name (important for login flows)."""
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", session_name or "s1")
    return os.path.join(_SESSIONS_ROOT, f"nd_{safe}")


async def _start_browser(session_name: str):
    import nodriver as nd
    profile = _profile_dir(session_name)
    Path(profile).mkdir(parents=True, exist_ok=True)
    # Chromium leaves SingletonLock / SingletonCookie / SingletonSocket
    # behind when it dies ungracefully (container restart, OOM, agent
    # crash). The next launch then fails with "Failed to connect to
    # browser" because Chrome thinks another instance owns the profile.
    # Safe to remove because we hold the session manager lock for this
    # session_name when we get here, and a previous in-process owner
    # would have been popped from _SESSIONS already.
    for stale in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.remove(os.path.join(profile, stale))
        except FileNotFoundError:
            pass
        except Exception:
            pass
    browser = await nd.start(
        user_data_dir=profile,
        headless=True,
        sandbox=False,
        browser_args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            f"--window-size={_DEFAULT_VIEWPORT[0]},{_DEFAULT_VIEWPORT[1]}",
        ],
    )
    return browser


async def _evict_stale_sessions_locked() -> None:
    """Caller must hold _SESSIONS_LOCK."""
    now = time.time()
    stale = [k for k, v in _SESSIONS.items() if (now - v.get("last_used", 0)) > _SESSION_IDLE_TTL_S]
    for k in stale:
        ent = _SESSIONS.pop(k, None)
        if ent and ent.get("browser") is not None:
            try:
                ent["browser"].stop()
            except Exception:
                pass
            logger.info("nodriver.session_evicted", session=k, reason="idle_ttl")


async def _get_or_create_session(session_name: str, *, create_if_missing: bool = True):
    """Return (browser, tab) for the named session. Tab may be None if no
    navigate has happened yet on this session.

    With create_if_missing=False, returns (None, None) when no session
    exists — used to check "does this session live in nodriver?".
    """
    safe = session_name or "s1"
    async with _SESSIONS_LOCK:
        await _evict_stale_sessions_locked()
        ent = _SESSIONS.get(safe)
        if ent is not None:
            ent["last_used"] = time.time()
            return ent["browser"], ent.get("tab")
    if not create_if_missing:
        return None, None

    browser = await asyncio.wait_for(_start_browser(safe), timeout=_NAV_TIMEOUT_S)
    async with _SESSIONS_LOCK:
        # Re-check after lock — concurrent caller may have created it.
        ent = _SESSIONS.get(safe)
        if ent is not None:
            try:
                browser.stop()
            except Exception:
                pass
            ent["last_used"] = time.time()
            return ent["browser"], ent.get("tab")
        _SESSIONS[safe] = {"browser": browser, "tab": None, "last_used": time.time()}
        logger.info("nodriver.session_started", session=safe)
        return browser, None


async def _set_tab(session_name: str, tab) -> None:
    safe = session_name or "s1"
    async with _SESSIONS_LOCK:
        ent = _SESSIONS.get(safe)
        if ent is not None:
            ent["tab"] = tab
            ent["last_used"] = time.time()


async def session_exists(session_name: str) -> bool:
    safe = session_name or "s1"
    async with _SESSIONS_LOCK:
        ent = _SESSIONS.get(safe)
        if ent is None:
            return False
        if (time.time() - ent.get("last_used", 0)) > _SESSION_IDLE_TTL_S:
            return False
        return True


async def _dismiss_overlays(tab) -> None:
    for sel in (
        "button[id*='accept' i]",
        "button[class*='accept' i]",
        "button[aria-label*='accept' i]",
        "button[id*='consent' i]",
        "[id*='cookie' i] button",
    ):
        try:
            el = await tab.select(sel, timeout=1)
            if el:
                await el.click()
                await asyncio.sleep(0.4)
                break
        except Exception:
            continue


async def _enqueue_visual_memory(
    *,
    file_path: str,
    final_url: str,
    valid_tag: str,
    vr,
    task_hint: str,
    chat_id: str,
    is_valid: bool,
) -> None:
    try:
        from . import browser as _br
        _vm_q = getattr(_br, "_visual_memory_queue", None)
        if _vm_q is None:
            return
        desc_parts = [valid_tag, "[engine=nodriver]"]
        if vr is not None and getattr(vr, "reason", ""):
            desc_parts.append(vr.reason[:200])
        if task_hint:
            desc_parts.append(f"hint={task_hint[:100]}")
        tags = ["capture", "browser", "nodriver"]
        if not is_valid:
            tags.append("invalid")
        _vm_q.put_nowait({
            "file_path": file_path,
            "url": final_url,
            "page_title": "",
            "description": " | ".join(desc_parts),
            "tags": tags,
            "chat_id": chat_id,
        })
    except Exception:
        pass


# ───────────────────────────────────────────────────────────────────────
# Public API: stateless one-shot
# ───────────────────────────────────────────────────────────────────────
async def navigate_capture(
    url: str,
    session_name: str = "s1",
    chat_id: str = "",
    user_id: str = "",
    task_hint: str = "",
) -> str:
    """Navigate to ``url`` and capture a screenshot. Reuses or creates the
    named session. The session stays alive after this call so a following
    click/type can act on the same page.

    Returns the same shape as the Selenium ``_do_capture``:
        [CAPTURE_VALID: true|false]
        [CAPTURE_STATUS: SUCCESS] | <validation failure message>
        Screenshot saved: /data/screenshots/screenshot_<ts>.png
    """
    if not url:
        return "Error: url is required for nodriver capture."
    Path(_SCREENSHOT_DIR).mkdir(parents=True, exist_ok=True)

    try:
        browser, _ = await _get_or_create_session(session_name)
        tab = await asyncio.wait_for(browser.get(url), timeout=_NAV_TIMEOUT_S)
        await _set_tab(session_name, tab)
        await asyncio.sleep(_VALIDATE_DELAY_S)
        await _dismiss_overlays(tab)
        try:
            await tab.evaluate("window.scrollBy(0, 200);")
            await asyncio.sleep(0.4)
            await tab.evaluate("window.scrollBy(0, -200);")
            await asyncio.sleep(0.3)
        except Exception:
            pass

        try:
            cf_title = await tab.evaluate("document.title || ''")
            cf_body = await tab.evaluate("document.body ? document.body.innerText : ''")
            cf_url = await tab.evaluate("window.location.href")
        except Exception:
            cf_title = cf_body = ""
            cf_url = url
        from . import browser_cloudflare as _cf
        if _cf.is_cloudflare_challenge(title=cf_title or "", body_text=cf_body or "", url=cf_url or url):
            logger.warning("nodriver.cloudflare_blocked", url=(cf_url or url)[:120])
            return _cf.blocked_response(cf_url or url, engine="nodriver")

        ts = int(time.time() * 1000)
        path = f"{_SCREENSHOT_DIR}/screenshot_{ts}.png"
        await tab.save_screenshot(filename=path, full_page=False)

        page_text = (cf_body or "")[:8000]
        final_url = cf_url or url

        from . import browser_validator as _bv
        try:
            vr = _bv.validate_page_from_text(
                page_text=page_text or "", url=final_url or url, task_hint=task_hint,
            )
        except Exception:
            vr = None

        is_valid = vr is None or vr.valid
        valid_tag = "[CAPTURE_VALID: true]" if is_valid else "[CAPTURE_VALID: false]"
        await _enqueue_visual_memory(
            file_path=path, final_url=final_url or url, valid_tag=valid_tag,
            vr=vr, task_hint=task_hint, chat_id=chat_id, is_valid=is_valid,
        )

        if not is_valid:
            failure_msg = _bv.format_validation_failure(final_url or url, vr, path)
            return f"{valid_tag}\nScreenshot saved: {path}\n\n{failure_msg}"
        return (
            f"{valid_tag}\n"
            f"[CAPTURE_STATUS: SUCCESS] (engine=nodriver)\n"
            f"Screenshot saved: {path}"
        )

    except asyncio.TimeoutError:
        return f"[NODRIVER_FAIL: navigation timed out after {_NAV_TIMEOUT_S}s]"
    except Exception as e:
        logger.warning("nodriver.capture_failed", error=str(e)[:200], url=url[:80])
        return f"[NODRIVER_FAIL: {str(e)[:200]}]"


# ───────────────────────────────────────────────────────────────────────
# Public API: stateful per-session actions
# ───────────────────────────────────────────────────────────────────────
async def navigate(
    url: str,
    session_name: str = "s1",
    chat_id: str = "",
    user_id: str = "",
    task_hint: str = "",
) -> str:
    """Navigate without taking a screenshot. Tab kept alive for follow-ups."""
    if not url:
        return "Error: url is required."
    try:
        browser, _ = await _get_or_create_session(session_name)
        tab = await asyncio.wait_for(browser.get(url), timeout=_NAV_TIMEOUT_S)
        await _set_tab(session_name, tab)
        await asyncio.sleep(_VALIDATE_DELAY_S)
        await _dismiss_overlays(tab)
        try:
            final_url = await tab.evaluate("window.location.href")
        except Exception:
            final_url = url
        try:
            title = await tab.evaluate("document.title || ''")
        except Exception:
            title = ""
        try:
            body_text = await tab.evaluate("document.body ? document.body.innerText : ''")
        except Exception:
            body_text = ""
        from . import browser_cloudflare as _cf
        if _cf.is_cloudflare_challenge(title=title or "", body_text=body_text or "", url=final_url or url):
            logger.warning("nodriver.cloudflare_blocked", url=(final_url or url)[:120])
            return _cf.blocked_response(final_url or url, engine="nodriver")
        return f"Navigated to {final_url}\nTitle: {title}\n(engine=nodriver)"
    except asyncio.TimeoutError:
        return f"[NODRIVER_FAIL: navigation timed out after {_NAV_TIMEOUT_S}s]"
    except Exception as e:
        logger.warning("nodriver.navigate_failed", error=str(e)[:200], url=url[:80])
        return f"[NODRIVER_FAIL: {str(e)[:200]}]"


async def click(selector: str, session_name: str = "s1") -> str:
    if not selector:
        return "[NODRIVER_FAIL: selector is required]"
    browser, tab = await _get_or_create_session(session_name, create_if_missing=False)
    if tab is None:
        return "[NODRIVER_FAIL: no active tab — call navigate first]"
    try:
        el = await tab.select(selector, timeout=10)
        if not el:
            return f"[NODRIVER_FAIL: selector '{selector}' not found]"
        await el.click()
        await asyncio.sleep(1.0)
        try:
            current = await tab.evaluate("window.location.href")
        except Exception:
            current = ""
        try:
            title = await tab.evaluate("document.title || ''")
        except Exception:
            title = ""
        return f"Clicked {selector}\nNow at: {current} - {title}\n(engine=nodriver)"
    except Exception as e:
        logger.warning("nodriver.click_failed", selector=selector[:80], error=str(e)[:200])
        return f"[NODRIVER_FAIL: click error {str(e)[:200]}]"


async def type_text(
    selector: str,
    text: str,
    submit: bool = False,
    session_name: str = "s1",
) -> str:
    if not selector:
        return "[NODRIVER_FAIL: selector is required]"
    browser, tab = await _get_or_create_session(session_name, create_if_missing=False)
    if tab is None:
        return "[NODRIVER_FAIL: no active tab — call navigate first]"
    try:
        el = await tab.select(selector, timeout=10)
        if not el:
            return f"[NODRIVER_FAIL: selector '{selector}' not found]"
        # Best-effort clear before typing.
        try:
            await el.clear_input()
        except Exception:
            try:
                await tab.evaluate(
                    f"document.querySelector({selector!r}) && (document.querySelector({selector!r}).value = '')"
                )
            except Exception:
                pass
        await el.send_keys(text)
        if submit:
            # nodriver send_keys uses Input.insertText which doesn't fire
            # keydown events for Enter. Submit via JS form.submit() if the
            # element lives in a form, otherwise dispatch a keydown event.
            try:
                await tab.evaluate(
                    "(()=>{const e=document.querySelector(" + repr(selector) + ");"
                    "if(!e)return false;"
                    "if(e.form){e.form.submit();return true;}"
                    "const ev=new KeyboardEvent('keydown',{key:'Enter',code:'Enter',"
                    "keyCode:13,which:13,bubbles:true});"
                    "e.dispatchEvent(ev);return true;})()"
                )
            except Exception:
                pass
            await asyncio.sleep(2.0)
        else:
            await asyncio.sleep(0.5)
        suffix = " and submitted" if submit else ""
        return f"Typed '{text[:80]}' into {selector}{suffix}\n(engine=nodriver)"
    except Exception as e:
        logger.warning("nodriver.type_failed", selector=selector[:80], error=str(e)[:200])
        return f"[NODRIVER_FAIL: type error {str(e)[:200]}]"


async def screenshot(
    selector: str = "",
    session_name: str = "s1",
    chat_id: str = "",
    user_id: str = "",
    task_hint: str = "",
) -> str:
    """Take a screenshot of the current tab in the session.

    The selector argument is accepted for API parity with Selenium but is
    treated as full-page (cropping a single element is rarely the right
    shape for downstream consumers — they want the whole result page).
    """
    browser, tab = await _get_or_create_session(session_name, create_if_missing=False)
    if tab is None:
        return "[NODRIVER_FAIL: no active tab — call navigate first]"
    try:
        Path(_SCREENSHOT_DIR).mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        path = f"{_SCREENSHOT_DIR}/screenshot_{ts}.png"
        await tab.save_screenshot(filename=path, full_page=False)
        try:
            page_text = await tab.evaluate("document.body ? document.body.innerText : ''")
            page_text = (page_text or "")[:8000]
        except Exception:
            page_text = ""
        try:
            final_url = await tab.evaluate("window.location.href")
        except Exception:
            final_url = ""

        from . import browser_validator as _bv
        try:
            vr = _bv.validate_page_from_text(
                page_text=page_text or "", url=final_url or "", task_hint=task_hint,
            )
        except Exception:
            vr = None

        is_valid = vr is None or vr.valid
        valid_tag = "[CAPTURE_VALID: true]" if is_valid else "[CAPTURE_VALID: false]"
        await _enqueue_visual_memory(
            file_path=path, final_url=final_url, valid_tag=valid_tag,
            vr=vr, task_hint=task_hint, chat_id=chat_id, is_valid=is_valid,
        )
        if not is_valid:
            failure_msg = _bv.format_validation_failure(final_url, vr, path)
            return f"{valid_tag}\nScreenshot saved: {path}\n\n{failure_msg}"
        return (
            f"{valid_tag}\n"
            f"[CAPTURE_STATUS: SUCCESS] (engine=nodriver)\n"
            f"Screenshot saved: {path}"
        )
    except Exception as e:
        logger.warning("nodriver.screenshot_failed", error=str(e)[:200])
        return f"[NODRIVER_FAIL: screenshot error {str(e)[:200]}]"


async def get_text(selector: str = "", session_name: str = "s1") -> str:
    browser, tab = await _get_or_create_session(session_name, create_if_missing=False)
    if tab is None:
        return "[NODRIVER_FAIL: no active tab — call navigate first]"
    try:
        if selector:
            el = await tab.select(selector, timeout=10)
            if not el:
                return f"[NODRIVER_FAIL: selector '{selector}' not found]"
            try:
                text = await tab.evaluate(
                    f"(document.querySelector({selector!r}) || {{}}).innerText || ''"
                )
            except Exception:
                text = getattr(el, "text", "") or ""
        else:
            try:
                text = await tab.evaluate("document.body ? document.body.innerText : ''")
            except Exception:
                text = ""
        try:
            current = await tab.evaluate("window.location.href")
        except Exception:
            current = ""
        try:
            title = await tab.evaluate("document.title || ''")
        except Exception:
            title = ""
        text = (text or "")[:4000]
        return f"Page: {current} - {title}\n---\n{text}\n(engine=nodriver)"
    except Exception as e:
        logger.warning("nodriver.get_text_failed", error=str(e)[:200])
        return f"[NODRIVER_FAIL: get_text error {str(e)[:200]}]"


async def scroll(direction: str = "down", session_name: str = "s1") -> str:
    browser, tab = await _get_or_create_session(session_name, create_if_missing=False)
    if tab is None:
        return "[NODRIVER_FAIL: no active tab — call navigate first]"
    try:
        delta = 600 if direction.lower() == "down" else -600
        await tab.evaluate(f"window.scrollBy(0, {delta});")
        await asyncio.sleep(0.4)
        return f"Scrolled {direction} by {abs(delta)}px (engine=nodriver)"
    except Exception as e:
        return f"[NODRIVER_FAIL: scroll error {str(e)[:200]}]"


async def submit(selector: str = "", session_name: str = "s1") -> str:
    """Submit a form. With ``selector``: submit the form containing that
    element (or the element itself). Without: submit the form of the
    active element, falling back to the first form on the page."""
    browser, tab = await _get_or_create_session(session_name, create_if_missing=False)
    if tab is None:
        return "[NODRIVER_FAIL: no active tab — call navigate first]"
    try:
        if selector:
            js = (
                "(()=>{const e=document.querySelector(" + repr(selector) + ");"
                "if(!e)return false;"
                "if(e.form){e.form.submit();return true;}"
                "if(e.tagName==='FORM'){e.submit();return true;}"
                "return false;})()"
            )
        else:
            js = (
                "(()=>{const a=document.activeElement;"
                "if(a&&a.form){a.form.submit();return true;}"
                "const f=document.querySelector('form');"
                "if(f){f.submit();return true;}"
                "return false;})()"
            )
        await tab.evaluate(js)
        await asyncio.sleep(2.0)
        try:
            current = await tab.evaluate("window.location.href")
        except Exception:
            current = ""
        try:
            title = await tab.evaluate("document.title || ''")
        except Exception:
            title = ""
        return f"Submitted form. Now at: {current} - {title} (engine=nodriver)"
    except Exception as e:
        logger.warning("nodriver.submit_failed", error=str(e)[:200])
        return f"[NODRIVER_FAIL: submit error {str(e)[:200]}]"


async def back(session_name: str = "s1") -> str:
    browser, tab = await _get_or_create_session(session_name, create_if_missing=False)
    if tab is None:
        return "[NODRIVER_FAIL: no active tab]"
    try:
        await tab.evaluate("window.history.back();")
        await asyncio.sleep(1.0)
        try:
            current = await tab.evaluate("window.location.href")
        except Exception:
            current = ""
        return f"Went back\nNow at: {current}\n(engine=nodriver)"
    except Exception as e:
        return f"[NODRIVER_FAIL: back error {str(e)[:200]}]"


async def close(session_name: str = "s1") -> str:
    safe = session_name or "s1"
    async with _SESSIONS_LOCK:
        ent = _SESSIONS.pop(safe, None)
    if ent and ent.get("browser") is not None:
        try:
            ent["browser"].stop()
        except Exception:
            pass
        return f"Closed nodriver session '{safe}'"
    return f"No active nodriver session '{safe}'"
