"""Browser skill — Selenium-based headless Chromium for web interaction."""

import asyncio
import os
import queue
import re
import time
import threading

import structlog
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()

MAX_TEXT = 10000
SCREENSHOT_DIR = "/data/screenshots"
SCREENSHOT_SHARED = "/data/shared"
SESSIONS_DIR = "/data/browser_sessions"
# Auto-close browser after 5 minutes of inactivity
IDLE_TIMEOUT = 300

_lock = threading.Lock()
_driver: webdriver.Chrome | None = None
_last_used: float = 0

# Named persistent sessions — each has its own Chrome profile with persistent cookies
_sessions: dict[str, webdriver.Chrome] = {}

# Visual memory write queue — sync-safe handoff from worker threads to main async loop.
# Producers (sync screenshot/capture handlers) push dicts; consumer (BrowserSkill.__call__)
# drains after asyncio.to_thread returns and schedules store_screenshot on the right loop.
_visual_memory_queue: queue.Queue = queue.Queue()
_session_last_used: dict[str, float] = {}
_sessions_lock = threading.Lock()


def _idle_reaper() -> None:
    """Background daemon that closes named sessions idle longer than IDLE_TIMEOUT seconds.

    Runs every 60 s.  Uses the already-populated _session_last_used dict to
    detect stale sessions.  Skipped entirely if no sessions exist (zero cost
    when the browser skill is not in use).
    """
    while True:
        time.sleep(60)
        try:
            now = time.time()
            to_close: list[str] = []
            with _sessions_lock:
                for name, last in list(_session_last_used.items()):
                    if now - last > IDLE_TIMEOUT:
                        to_close.append(name)
            for name in to_close:
                try:
                    _close_driver(name)
                    logger.info("browser.idle_session_reaped", session=name)
                except Exception:
                    pass
        except Exception:
            pass


# Start the idle reaper as a daemon thread (dies automatically when the process exits)
_reaper_thread = threading.Thread(target=_idle_reaper, daemon=True, name="browser-idle-reaper")
_reaper_thread.start()

_STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : originalQuery(parameters);
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['es-CL', 'es', 'en-US', 'en']});
    Object.defineProperty(window, 'outerWidth', {get: () => window.innerWidth});
    Object.defineProperty(window, 'outerHeight', {get: () => window.innerHeight});
    window.close = () => {};
"""


def _create_driver(profile_dir: str | None = None) -> webdriver.Chrome:
    """Create a new Chrome WebDriver instance, optionally with a persistent profile."""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1366,768")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-features=IsolateOrigins,site-per-process")
    opts.add_argument("--js-flags=--max-old-space-size=256")
    opts.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    opts.add_argument("--lang=es")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    if profile_dir:
        os.makedirs(profile_dir, exist_ok=True)
        # Remove stale Chrome singleton lock files that cause "Chrome instance exited" crashes
        for _lock_file in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            _lock_path = os.path.join(profile_dir, _lock_file)
            try:
                os.remove(_lock_path)
                logger.info("browser.removed_stale_lock", file=_lock_file, profile=profile_dir)
            except FileNotFoundError:
                pass
        opts.add_argument(f"--user-data-dir={profile_dir}")

    chrome_bin = os.environ.get("CHROME_BIN", "/usr/bin/chromium")
    chromedriver = os.environ.get("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")
    opts.binary_location = chrome_bin

    service = Service(executable_path=chromedriver)
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(30)  # Increased from 15s — JS-heavy sites need more time
    driver.implicitly_wait(3)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _STEALTH_JS})
    return driver


def _get_driver(session_name: str = "") -> webdriver.Chrome:
    """Get or create a browser instance. Named sessions persist cookies/login state."""
    global _driver, _last_used

    if session_name:
        with _sessions_lock:
            driver = _sessions.get(session_name)
            if driver is not None:
                try:
                    _ = driver.title
                    _session_last_used[session_name] = time.time()
                    return driver
                except Exception:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    _sessions.pop(session_name, None)

            # Create new session with dedicated profile directory
            profile_dir = os.path.join(SESSIONS_DIR, session_name)
            driver = _create_driver(profile_dir)
            _sessions[session_name] = driver
            _session_last_used[session_name] = time.time()
            logger.info("browser.session_started", session=session_name, profile=profile_dir)
            return driver

    # Default shared (stateless) instance
    with _lock:
        if _driver is not None:
            try:
                _ = _driver.title
                _last_used = time.time()
                return _driver
            except Exception:
                try:
                    _driver.quit()
                except Exception:
                    pass
                _driver = None

        _driver = _create_driver()
        _last_used = time.time()
        logger.info("browser.started")
        return _driver


def _close_driver(session_name: str = ""):
    global _driver
    if session_name:
        with _sessions_lock:
            driver = _sessions.pop(session_name, None)
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
                _session_last_used.pop(session_name, None)
                logger.info("browser.session_closed", session=session_name)
        return
    with _lock:
        if _driver:
            try:
                _driver.quit()
            except Exception:
                pass
            _driver = None
            logger.info("browser.closed")


def _list_sessions() -> str:
    with _sessions_lock:
        if not _sessions:
            return "No active named sessions."
        lines = ["Active browser sessions:"]
        for name, driver in _sessions.items():
            try:
                url = driver.current_url
                title = driver.title or ""
                last = time.time() - _session_last_used.get(name, 0)
                lines.append(f"  [{name}] {title[:50]} — {url[:60]} (idle {last:.0f}s)")
            except Exception:
                lines.append(f"  [{name}] (crashed)")
        return "\n".join(lines)


def _truncate(text: str, limit: int = MAX_TEXT) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n... (truncated, {len(text)} total chars)"
    return text


def _wait_for_page(driver, timeout=8):
    """Wait for the page to finish loading, including JS rendering."""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except Exception:
        pass
    # Wait for dynamic JS content (SPAs, React, etc.)
    # Check if body has meaningful content, wait up to 2s max
    for _ in range(4):
        time.sleep(0.3)
        try:
            body_len = driver.execute_script("return document.body ? document.body.innerText.length : 0")
            img_count = driver.execute_script("return document.images ? document.images.length : 0")
            if body_len > 100 or img_count > 3:
                break
        except Exception:
            break


def _normalize_url(url: str) -> str:
    """Ensure URL has a scheme and strip common LLM-generated artifacts.

    Blocks dangerous schemes (file://, javascript:, data:) and internal SSRF targets.
    """
    url = url.strip()
    if not url:
        return ""
    # Strip trailing markdown/bracket artifacts the LLM sometimes appends
    url = re.sub(r"[)\]]+[/]*$", "", url)   # trailing ], )/,  ]/
    url = url.rstrip("/").rstrip()
    url = url.replace("%5D", "").replace("%5B", "")  # URL-encoded brackets

    # Block dangerous URL schemes before adding https:// prefix
    _lower = url.lower()
    if _lower.startswith(("file://", "javascript:", "data:", "vbscript:")):
        logger.warning("browser.blocked_dangerous_scheme", url=url[:80])
        return ""

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Block internal/metadata addresses (SSRF protection)
    import urllib.parse as _urlparse
    import ipaddress as _ipaddress
    try:
        _host = _urlparse.urlparse(url).hostname or ""
        _BLOCKED_HOSTS = frozenset({
            "169.254.169.254", "metadata.google.internal", "metadata.gcp.internal",
            "localhost", "ip6-localhost", "ip6-loopback",
        })
        if _host in _BLOCKED_HOSTS or _host.endswith(".local") or _host.endswith(".internal"):
            logger.warning("browser.blocked_ssrf_host", host=_host)
            return ""
        try:
            _addr = _ipaddress.ip_address(_host)
            if _addr.is_private or _addr.is_loopback or _addr.is_link_local:
                logger.warning("browser.blocked_internal_ip", host=_host)
                return ""
        except ValueError:
            pass  # Not an IP address — hostname is fine
    except Exception:
        pass

    return url


def _do_navigate(url: str, session_name: str = "", _extended_timeout: bool = False) -> str:
    raw_url = url
    url = _normalize_url(url)
    if not url:
        if raw_url.strip():
            return f"Error: URL blocked (dangerous scheme or internal address): {raw_url[:80]}"
        return "Error: No URL provided. Use browser(action=\"navigate\", url=\"https://example.com\")"
    driver = _get_driver(session_name)

    # Clear browser cache before each navigation (skip for named sessions to preserve login)
    if not session_name:
        try:
            driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        except Exception:
            pass

    # Extended timeout mode: temporarily raise to 50s for slow/interactive pages
    if _extended_timeout:
        try:
            driver.set_page_load_timeout(50)
        except Exception:
            pass

    _nav_timed_out = False
    current = url

    # Try navigation, with retry if anti-bot redirects to data:,
    for attempt in range(2):
        try:
            driver.get(url)
        except Exception as e:
            err_lower = str(e).lower()
            if "timeout" in err_lower or "timed out" in err_lower:
                _nav_timed_out = True
                logger.warning(
                    "browser.navigate_timeout",
                    url=url[:80],
                    attempt=attempt + 1,
                    extended=_extended_timeout,
                )
                # Do NOT re-raise — partial page content may still be usable
            else:
                raise
        _wait_for_page(driver)

        try:
            current = driver.current_url
        except Exception:
            pass

        # If anti-bot redirected to data:, or about:blank, retry once
        if current in ("data:,", "about:blank", "") and attempt == 0:
            logger.warning("browser.antibot_redirect", url=url, landed=current)
            # Close and reopen browser to get fresh stealth injection
            _close_driver(session_name)
            driver = _get_driver(session_name)
            continue
        break

    # Restore default timeout after extended navigation
    if _extended_timeout:
        try:
            driver.set_page_load_timeout(30)
        except Exception:
            pass

    title = "(no title)"
    text = ""
    try:
        title = driver.title or "(no title)"
        current = driver.current_url
    except Exception:
        pass

    # If still at data:, after retry, report failure with suggestion
    if current in ("data:,", "about:blank", ""):
        return (
            f"Browser could not load {url} (anti-bot protection detected).\n"
            f"Try using fetch_url(url=\"{url}\") or shell(command=\"curl -sL {url}\") instead."
        )

    # Navigation timed out but page partially loaded — include diagnostic note
    timeout_note = ""
    if _nav_timed_out:
        timeout_note = f"\n[Note: page load timed out after {'50' if _extended_timeout else '30'}s — content may be partial]"

    try:
        body = driver.find_element(By.TAG_NAME, "body")
        text = body.text[:MAX_TEXT] if body else ""
    except Exception:
        text = "(page content not yet available)"

    # Cloudflare challenge gate — bail before returning challenge HTML to caller
    try:
        from . import browser_cloudflare as _cf
        if _cf.is_cloudflare_challenge(title=title or "", body_text=text or "", url=current or url):
            logger.warning("browser.cloudflare_blocked", url=(current or url)[:120])
            return _cf.blocked_response(current or url, engine="selenium")
    except Exception:
        pass

    return f"Navigated to: {current}\nTitle: {title}{timeout_note}\n---\n{_truncate(text)}"


def _do_click(selector: str, session_name: str = "") -> str:
    driver = _get_driver(session_name)
    wait = WebDriverWait(driver, 10)
    el = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
    tag = el.tag_name
    text = el.text[:100] if el.text else el.get_attribute("value") or ""
    el.click()
    time.sleep(1)
    title = driver.title or ""
    current = driver.current_url
    return f"Clicked: <{tag}> '{text}'\nNow at: {current} - {title}"


def _do_type(selector: str, text: str, submit: bool = False, session_name: str = "") -> str:
    driver = _get_driver(session_name)
    wait = WebDriverWait(driver, 10)
    el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
    el.clear()
    el.send_keys(text)
    if submit:
        el.send_keys(Keys.RETURN)
        time.sleep(2)
    return f"Typed '{text}' into {selector}" + (" and submitted" if submit else "")


def _do_get_text(selector: str = "", session_name: str = "") -> str:
    driver = _get_driver(session_name)
    if selector:
        wait = WebDriverWait(driver, 10)
        el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
        text = el.text
    else:
        body = driver.find_element(By.TAG_NAME, "body")
        text = body.text
    current = driver.current_url
    title = driver.title or ""
    return f"Page: {current} - {title}\n---\n{_truncate(text)}"


def _do_screenshot(selector: str = "", session_name: str = "", chat_id: str = "", user_id: str = "") -> str:
    """Take a full-page screenshot, or crop to a CSS selector element if provided.

    Each call saves to a UNIQUE timestamped file so old screenshots are never returned.
    The session_name must match the session used for navigate() to capture the correct browser.
    """
    import base64
    driver = _get_driver(session_name)  # MUST match the session used for navigate

    # Verify the driver is alive and on a real page
    try:
        current = driver.current_url
        title = driver.title or ""
    except Exception:
        return "Screenshot failed: browser session is not available or crashed."

    if current in ("data:,", "about:blank", "", "chrome://newtab/"):
        return (
            f"Screenshot aborted: browser has no page loaded (current URL: {current!r}).\n"
            f"Use browser(action='navigate', url='...') first, then take the screenshot."
        )

    # Stop any pending renders
    try:
        driver.execute_script("window.stop();")
    except Exception:
        pass

    # Dismiss cookie banners, modals, and overlays that would block the content
    _dismiss_overlays(session_name)

    # Wait for JS-heavy pages (SPAs, charting widgets) to finish rendering
    time.sleep(2)

    # Unique filename per call — never overwrite old screenshots
    # Use millisecond precision so parallel calls within the same second don't collide
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    os.makedirs(SCREENSHOT_SHARED, exist_ok=True)
    ts = int(time.time() * 1000)
    fname = f"screenshot_{ts}.png"
    saved_path = os.path.join(SCREENSHOT_DIR, fname)
    # Also write a symlink-friendly copy to /data/shared for backward compat
    shared_path = os.path.join(SCREENSHOT_SHARED, fname)

    result_msg = ""

    # Element-level screenshot: scroll element into view, capture it
    if selector:
        try:
            wait = WebDriverWait(driver, 10)
            el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.5)
            el.screenshot(saved_path)
            size = el.size
            result_msg = (
                f"Element screenshot saved to {saved_path}\n"
                f"Selector: {selector} ({size['width']}x{size['height']}px)\n"
                f"Page: {current}\nTitle: {title}"
            )
        except Exception as e:
            logger.warning("browser.element_screenshot_failed", selector=selector, error=str(e))
            selector = ""  # Fall through to full-page

    if not selector:
        # Full-page screenshot via CDP (highest quality)
        try:
            cdp_result = driver.execute_cdp_cmd("Page.captureScreenshot", {"format": "png"})
            img_bytes = base64.b64decode(cdp_result["data"])
            with open(saved_path, "wb") as f:
                f.write(img_bytes)
            # Mirror to /data/shared so serve_media can find it by basename
            with open(shared_path, "wb") as f:
                f.write(img_bytes)
        except Exception:
            try:
                driver.save_screenshot(saved_path)
                import shutil
                shutil.copy2(saved_path, shared_path)
            except Exception as e:
                return f"Screenshot failed: {e}"
        result_msg = (
            f"Screenshot saved to {saved_path}\n"
            f"Page: {current}\nTitle: {title}\n"
            f"⚠️ Verify the title matches what you expected before including in response."
        )

    # Visual memory: enqueue metadata for the async consumer to write.
    # Direct asyncio.run() in a worker thread breaks SQLAlchemy's async pool
    # (Future attached to a different loop). The async __call__ drains this
    # queue after asyncio.to_thread returns, scheduling stores on the main loop.
    if saved_path:
        try:
            _visual_memory_queue.put_nowait({
                "file_path": saved_path,
                "url": current,
                "page_title": title,
                "description": ("selector=" + selector) if selector else "full-page",
                "tags": ["screenshot", "browser"],
                "chat_id": chat_id,
            })
        except Exception:
            pass  # Queue full or unexpected — visual memory is optional

    return result_msg


def _do_find_elements(selector: str) -> str:
    driver = _get_driver()
    elements = driver.find_elements(By.CSS_SELECTOR, selector)
    if not elements:
        return f"No elements found for: {selector}"
    lines = [f"Found {len(elements)} elements for '{selector}':\n"]
    for i, el in enumerate(elements[:25]):
        tag = el.tag_name
        text = (el.text or "")[:80].replace("\n", " ")
        href = el.get_attribute("href") or ""
        el_id = el.get_attribute("id") or ""
        classes = el.get_attribute("class") or ""
        desc = f"  [{i}] <{tag}"
        if el_id:
            desc += f' id="{el_id}"'
        if classes:
            desc += f' class="{classes[:50]}"'
        if href:
            desc += f' href="{href[:80]}"'
        desc += f"> {text}"
        lines.append(desc)
    return "\n".join(lines)


def _do_execute_js(script: str) -> str:
    driver = _get_driver()
    result = driver.execute_script(script)
    if result is None:
        return "(no return value)"
    return _truncate(str(result))


def _do_scroll(direction: str = "down", session_name: str = "") -> str:
    driver = _get_driver(session_name)
    if direction == "up":
        js_cmd = "window.scrollBy(0, -800);"
    elif direction == "top":
        js_cmd = "window.scrollTo(0, 0);"
    elif direction == "bottom":
        js_cmd = "window.scrollTo(0, document.body.scrollHeight);"
    else:
        js_cmd = "window.scrollBy(0, 800);"

    # Primary: window-level scroll
    driver.execute_script(js_cmd)
    time.sleep(0.5)
    scroll_y = driver.execute_script("return window.pageYOffset;")

    # Fallback for SPAs with custom scroll containers (Binance, etc.):
    # If window didn't scroll, find and scroll the largest scrollable div
    if scroll_y == 0 and direction not in ("top",):
        driver.execute_script("""
            var dy = arguments[0];
            var best = null, bestH = 0;
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {
                var el = all[i];
                var sh = el.scrollHeight, ch = el.clientHeight;
                if (sh > ch + 50 && ch > 200 && sh > bestH) {
                    var st = window.getComputedStyle(el);
                    var ov = st.overflowY || st.overflow;
                    if (ov === 'auto' || ov === 'scroll' || el.tagName === 'BODY') {
                        best = el; bestH = sh;
                    }
                }
            }
            if (best) { best.scrollTop += dy; }
        """, 800 if direction not in ("up",) else -800)
        time.sleep(0.3)
        scroll_y = driver.execute_script("return window.pageYOffset;")

    total = driver.execute_script("return document.body.scrollHeight;")
    return f"Scrolled {direction}. Position: {scroll_y}px / {total}px total"


def _do_back() -> str:
    driver = _get_driver()
    driver.back()
    time.sleep(1)
    return f"Navigated back to: {driver.current_url} - {driver.title}"


def _do_submit(selector: str = "", session_name: str = "") -> str:
    """Submit a form. With ``selector`` → submits the form containing that
    element (or the element itself if it is a form). Without ``selector``
    → submits the form of the active element, falling back to the first
    form on the page."""
    driver = _get_driver(session_name)
    if selector:
        wait = WebDriverWait(driver, 10)
        el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
        try:
            el.submit()
        except Exception:
            driver.execute_script(
                "arguments[0].form ? arguments[0].form.submit() : arguments[0].submit && arguments[0].submit()",
                el,
            )
    else:
        driver.execute_script(
            "(function(){"
            "var a=document.activeElement;"
            "if(a && a.form){a.form.submit();return;}"
            "var f=document.querySelector('form');"
            "if(f){f.submit();}"
            "})()"
        )
    time.sleep(2)
    return f"Submitted form. Now at: {driver.current_url} - {driver.title}"


def _do_close(session_name: str = "") -> str:
    _close_driver(session_name)
    if session_name:
        return f"Browser session '{session_name}' closed."
    return "Browser closed."


def _dismiss_overlays(session_name: str = "") -> bool:
    """Dismiss cookie banners, modals, newsletter popups, and any overlay blocking content.

    Strategy (in order):
    1. Click known cookie/consent accept buttons (CSS selectors)
    2. Click close/dismiss buttons (×, X, Cerrar, No gracias, Skip, Close)
    3. Accept-by-text (Aceptar, Allow, Got it, etc.)
    4. JavaScript nuclear: hide/remove fixed overlays with high z-index that are still present

    Uses zero implicit wait to avoid 3s × N_selectors = 60s hangtime when no dialog present.
    Returns True if something was dismissed.
    """
    try:
        driver = _get_driver(session_name)
    except Exception:
        return False

    dismissed = False
    driver.implicitly_wait(0)
    try:
        # ── 1. Cookie/consent accept buttons ──────────────────────────────────
        css_accept = [
            "#onetrust-accept-btn-handler",
            "#accept-all-cookies",
            "#acceptAllButton",
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
            "button[id*='accept-all']",
            "button[id*='acceptAll']",
            "button[id*='cookie-accept']",
            "button[id*='cookieAccept']",
            "button[class*='accept-all']",
            "button[class*='acceptAll']",
            "button[class*='cookie-accept']",
            "button[class*='js-accept']",
            "[data-testid*='accept']",
            "[data-testid='cookie-accept']",
            "[data-testid='gdpr-cookie-accept-all-button']",
            ".cookie-consent-accept",
            ".js-accept-cookies",
            ".cc-accept",
            ".cc-btn.cc-allow",
            # Binance
            "button.bn-button__primary",
            # Didomi
            "#didomi-notice-agree-button",
            # Quantcast
            ".qc-cmp2-summary-buttons button:first-child",
            # TrustArc
            "#truste-consent-button",
            # SourcePoint
            "button[title='Accept All']",
        ]
        for css in css_accept:
            try:
                el = driver.find_element(By.CSS_SELECTOR, css)
                if el.is_displayed() and el.is_enabled():
                    el.click()
                    time.sleep(0.6)
                    logger.info("browser.overlay_accepted", selector=css)
                    dismissed = True
                    break
            except Exception:
                pass

        # ── 2. Close/dismiss buttons (×, X, skip, no thanks) ─────────────────
        css_close = [
            # Generic close buttons by aria-label
            "button[aria-label='Close']",
            "button[aria-label='close']",
            "button[aria-label='Cerrar']",
            "button[aria-label='cerrar']",
            "button[aria-label='Dismiss']",
            "button[aria-label='dismiss']",
            # Common modal close patterns
            ".modal__close",
            ".modal-close",
            ".popup-close",
            ".popup__close",
            ".overlay-close",
            ".dialog-close",
            "[class*='modal'][class*='close']",
            "[class*='popup'][class*='close']",
            "[class*='banner'][class*='close']",
            # id patterns
            "#modal-close",
            "#popup-close",
            "#close-button",
            "#closeButton",
            "[id*='modal-close']",
            "[id*='popup-close']",
        ]
        for css in css_close:
            try:
                el = driver.find_element(By.CSS_SELECTOR, css)
                if el.is_displayed() and el.is_enabled():
                    el.click()
                    time.sleep(0.5)
                    logger.info("browser.overlay_closed", selector=css)
                    dismissed = True
                    break
            except Exception:
                pass

        # ── 3. Text-based accept/close matching ───────────────────────────────
        accept_texts = [
            "Accept All", "Accept all", "Accept Cookies", "I Accept", "I Agree",
            "Aceptar todo", "Aceptar todas", "Aceptar", "Acepto", "Permitir todo",
            "Permitir", "Allow All", "Allow all", "Allow cookies",
            "Got it", "Agree", "OK", "Confirm", "Continue",
        ]
        dismiss_texts = [
            "×", "✕", "✖", "Close", "Cerrar", "Dismiss",
            "No thanks", "No gracias", "Skip", "Omitir", "Not now", "Ahora no",
            "Maybe later", "Más tarde",
        ]
        all_texts = accept_texts + dismiss_texts
        for txt in all_texts:
            try:
                els = driver.find_elements(By.XPATH, f"//*[normalize-space(text())='{txt}']")
                for el in els:
                    if el.is_displayed() and el.is_enabled() and el.tag_name in ("button", "a", "span", "div", "i"):
                        el.click()
                        time.sleep(0.5)
                        logger.info("browser.overlay_dismissed_by_text", text=txt)
                        dismissed = True
                        break
                if dismissed:
                    break
            except Exception:
                pass

        # ── 4. JavaScript nuclear: remove fixed/sticky high-z-index overlays ──
        # If anything is still blocking (position:fixed or sticky, z-index > 100,
        # not a nav bar), forcibly remove it and restore body scroll.
        try:
            driver.execute_script("""
                (function() {
                    var removed = 0;
                    var els = document.querySelectorAll('*');
                    for (var i = 0; i < els.length; i++) {
                        var el = els[i];
                        var s = window.getComputedStyle(el);
                        var pos = s.position;
                        var z = parseInt(s.zIndex) || 0;
                        var display = s.display;
                        if (display === 'none') continue;
                        // Must be fixed/sticky with high z-index and cover meaningful area
                        if ((pos === 'fixed' || pos === 'sticky') && z > 100) {
                            var rect = el.getBoundingClientRect();
                            // Skip small elements (real close buttons, nav bars)
                            var area = rect.width * rect.height;
                            var viewArea = window.innerWidth * window.innerHeight;
                            // If it covers > 15% of viewport it's likely a blocker
                            if (area > viewArea * 0.15) {
                                el.remove();
                                removed++;
                            }
                        }
                    }
                    // Restore body scroll (overlays often set overflow:hidden)
                    document.body.style.overflow = '';
                    document.documentElement.style.overflow = '';
                    return removed;
                })();
            """)
            time.sleep(0.3)
        except Exception:
            pass

    finally:
        driver.implicitly_wait(3)

    return dismissed


# Keep old name as alias for any internal callers
def _accept_cookies(session_name: str = "") -> bool:
    return _dismiss_overlays(session_name)


def _do_capture(url: str, session_name: str = "s1", selector: str = "", chat_id: str = "", user_id: str = "", task_hint: str = "") -> str:
    """Navigate to URL, accept cookie dialogs, wait for content, then screenshot — all in ONE call.

    Includes generic page validation: detects login walls, captchas, and other blocking UIs.
    Retries once if the page is invalid.  Returns an honest failure message if blocked.
    """
    if not url:
        return "Error: url is required for capture action."

    # SSRF hard block — refuse before _do_navigate so we never spin up the
    # driver, validate, or take a screenshot of an internal address. The
    # _normalize_url helper already enforces this for navigate, but
    # _do_capture used to swallow the error string and continue to
    # validate_page on a blank driver, producing a screenshot of "" with
    # a "login_wall" verdict — i.e. SSRF was effectively allowed.
    _norm = _normalize_url(url)
    if not _norm:
        return f"Error: URL blocked (internal address or unsafe scheme): {url[:80]}"
    url = _norm

    session_name = session_name or "s1"

    from . import browser_validator as _bv

    def _load_and_prepare(is_retry: bool = False) -> tuple:
        """Navigate + dismiss overlays + scroll-settle.  Returns (driver, accepted_bool)."""
        _do_navigate(url, session_name)
        time.sleep(0.8)
        _accepted = _dismiss_overlays(session_name)
        if _accepted:
            time.sleep(0.8)
        time.sleep(0.5)
        _dismiss_overlays(session_name)
        try:
            _d = _get_driver(session_name)
            _d.execute_script("window.scrollBy(0, 150);")
            time.sleep(0.5)
            _d.execute_script("window.scrollBy(0, -150);")
            time.sleep(0.3)
        except Exception:
            pass
        return _accepted

    # Step 1: Navigate + prepare
    accepted = _load_and_prepare()

    # Step 1b: Cloudflare gate — if the page is a CF challenge, do NOT
    # screenshot/validate the challenge page. Return the blocked marker so
    # upstream routing skips fallback retries.
    try:
        from . import browser_cloudflare as _cf
        _d = _get_driver(session_name)
        _cf_title = ""
        _cf_text = ""
        try:
            _cf_title = _d.title or ""
        except Exception:
            pass
        try:
            _cf_text = _d.find_element(By.TAG_NAME, "body").text[:6000]
        except Exception:
            pass
        try:
            _cf_url = _d.current_url
        except Exception:
            _cf_url = url
        if _cf.is_cloudflare_challenge(title=_cf_title, body_text=_cf_text, url=_cf_url or url):
            logger.warning("browser.capture_cloudflare_blocked", url=(_cf_url or url)[:120])
            return _cf.blocked_response(_cf_url or url, engine="selenium")
    except Exception:
        pass

    # Step 2: Validate page content
    try:
        driver = _get_driver(session_name)
        vr = _bv.validate_page(driver, url, task_hint)
    except Exception:
        vr = None

    if vr is not None and not vr.valid:
        logger.warning("browser.capture_invalid_first_attempt", url=url[:80], reason=vr.reason)
        # Retry once with a fresh load
        time.sleep(1)
        accepted = _load_and_prepare(is_retry=True)
        try:
            driver = _get_driver(session_name)
            vr = _bv.validate_page(driver, url, task_hint)
        except Exception:
            vr = None

    # Step 3: Take screenshot (always — invalid pages get a diagnostic screenshot)
    shot_result = _do_screenshot(selector, session_name, chat_id, user_id)
    _shot_path_match = re.search(r"(/data/screenshots/screenshot_\d+\.png)", shot_result)
    _shot_path = _shot_path_match.group(1) if _shot_path_match else ""

    # Step 4: Build response with explicit validity tag
    # [CAPTURE_VALID: true/false] is parsed by handlers.py to gate report/email use.
    # The screenshot is ALWAYS included so the user can see what loaded.
    is_valid = vr is None or vr.valid
    valid_tag = "[CAPTURE_VALID: true]" if is_valid else "[CAPTURE_VALID: false]"

    # ── Visual Memory producer ────────────────────────────────────────────────
    # Enqueue metadata for the async consumer to write on the main event loop.
    # See queue rationale at module top — direct DB writes from worker threads
    # break SQLAlchemy's async connection pool.
    try:
        _vm_desc_parts = [valid_tag]
        if vr is not None and getattr(vr, "reason", ""):
            _vm_desc_parts.append(vr.reason[:200])
        if vr is not None and not vr.valid and _bv.is_blocked_source(vr):
            _vm_desc_parts.append("[BLOCKED_SOURCE]")
        if task_hint:
            _vm_desc_parts.append(f"hint={task_hint[:100]}")
        _vm_desc = " | ".join(_vm_desc_parts)
        _vm_tags = ["capture", "browser"]
        if not is_valid:
            _vm_tags.append("invalid")
            if _bv.is_blocked_source(vr):
                _vm_tags.append("blocked_source")
        if _shot_path:
            try:
                _visual_memory_queue.put_nowait({
                    "file_path": _shot_path,
                    "url": url,
                    "page_title": "",
                    "description": _vm_desc,
                    "tags": _vm_tags,
                    "chat_id": chat_id,
                })
            except Exception:
                pass
    except Exception:
        pass

    if not is_valid:
        failure_msg = _bv.format_validation_failure(url, vr, _shot_path)
        return (
            f"{valid_tag}\n"
            f"{shot_result}\n\n"
            f"{failure_msg}"
        )

    overlay_note = " (overlay/cookie dismissed)" if accepted else ""
    return (
        f"{valid_tag}\n"
        f"[CAPTURE_STATUS: SUCCESS]{overlay_note}\n"
        f"{shot_result}"
    )


def _do_scroll_capture(url: str, session_name: str = "s1", scroll_count: int = 0,
                        chat_id: str = "", user_id: str = "") -> str:
    """Navigate to URL then take screenshots while scrolling all the way to the bottom.

    scroll_count=0 (default) → auto mode: calculates shots from initial page height,
      then captures until page end detected or up to 30 shots. Stops early when
      scrollHeight stops growing (finite page) or warns if it keeps growing (infinite scroll).
    scroll_count>0 → capture exactly that many positions (up to 30 max).
    """
    session_name = session_name or "s1"
    MAX_SHOTS = 30  # Safety ceiling to avoid infinite scroll loops

    nav_result = _do_navigate(url, session_name)
    time.sleep(0.8)
    _dismiss_overlays(session_name)
    time.sleep(0.5)
    _dismiss_overlays(session_name)  # Second pass — some overlays appear after JS settles

    results = []

    try:
        driver = _get_driver(session_name)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.3)

        vh = driver.execute_script("return window.innerHeight") or 600
        step = max(vh - 80, 200)  # Scroll step with 80px overlap between shots

        if scroll_count > 0:
            # Explicit count requested — honour it up to MAX_SHOTS
            scroll_count = min(int(scroll_count), MAX_SHOTS)
            for i in range(scroll_count):
                target_y = i * step
                driver.execute_script(f"window.scrollTo(0, {target_y});")
                time.sleep(0.5)
                shot = _do_screenshot("", session_name, chat_id, user_id)
                results.append(f"Screenshot {i+1}/{scroll_count}: {shot}")
        else:
            # Auto mode — scroll until we hit the real bottom or MAX_SHOTS
            prev_scroll_h = 0
            stale_count = 0  # Consecutive shots with no height growth (infinite scroll guard)
            infinite_scroll_warned = False
            i = 0
            while i < MAX_SHOTS:
                target_y = i * step
                driver.execute_script(f"window.scrollTo(0, {target_y});")
                time.sleep(0.5)  # Give lazy-load content time to render

                current_scroll_h = driver.execute_script(
                    "return document.documentElement.scrollHeight"
                ) or vh

                shot = _do_screenshot("", session_name, chat_id, user_id)
                results.append(f"Screenshot {i+1}: {shot}")
                i += 1

                # Check if we've reached the real bottom of the page
                scroll_top_after = driver.execute_script(
                    "return window.pageYOffset + window.innerHeight"
                ) or 0
                if scroll_top_after >= current_scroll_h - 10:
                    # We're at (or past) the bottom — done
                    break

                # Detect infinite scroll: height keeps growing with every scroll
                if current_scroll_h > prev_scroll_h:
                    stale_count = 0
                else:
                    stale_count += 1

                if stale_count == 0 and i >= 5 and not infinite_scroll_warned:
                    # Height still growing after 5 shots — likely infinite scroll feed
                    infinite_scroll_warned = True
                    results.append(
                        "[Note: page appears to use infinite scroll — stopping at "
                        f"{MAX_SHOTS} screenshots to avoid an infinite loop]"
                    )

                prev_scroll_h = current_scroll_h

        # Scroll back to top when done
        driver.execute_script("window.scrollTo(0, 0);")
    except Exception as e:
        return f"scroll_capture error: {e}\nPartial results:\n" + "\n".join(results)

    note = f" ({len(results)} screenshots)" if results else ""
    summary = f"Page: {url}{note}\n" + "\n".join(results)
    return summary


def _dom_snapshot(driver) -> str:
    """Capture current body text for DOM change comparison."""
    try:
        return driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return ""


def _dom_changed(before: str, after: str, min_delta: int = 80) -> bool:
    """Return True if DOM content grew meaningfully after an action."""
    return len(after) > len(before) + min_delta or (
        len(before) > 0 and abs(len(after) - len(before)) > len(before) * 0.05
    )


def _find_submit_button_by_text(driver) -> object:
    """Search clickable elements by text content — track/search/submit/rastrear/buscar."""
    _SUBMIT_KEYWORDS = re.compile(
        r"\b(?:track|search|submit|rastrear|buscar|seguir|consultar|find|go|ver|check)\b",
        re.IGNORECASE,
    )
    try:
        candidates = driver.find_elements(
            By.CSS_SELECTOR,
            "button, input[type='submit'], input[type='button'], a[role='button'], [role='button'],"
            "[class*='search-area']:not(.hide), [title*='Track the'], [title*='Search']",
        )
        for el in candidates:
            if not el.is_displayed():
                continue
            label = (el.text or el.get_attribute("value") or el.get_attribute("aria-label") or "").strip()
            if label and _SUBMIT_KEYWORDS.search(label):
                return el
    except Exception:
        pass
    return None


def _validate_click_target(driver, el, trace: list, input_el=None) -> bool:
    """Pre-click validation: element must be visible, not covered, in same container as input.

    Returns True if safe to click, False if target looks wrong.
    """
    try:
        if not el.is_displayed() or not el.is_enabled():
            trace.append("  [target_invalid] element not visible or not enabled")
            return False

        # Same-container check: button must share an ancestor with the input field
        if input_el:
            same_container = driver.execute_script("""
                var btn=arguments[0], inp=arguments[1];
                var a=btn.parentElement;
                for(var i=0;i<6&&a;i++){
                    if(a.contains(inp)) return true;
                    a=a.parentElement;
                }
                var bf=btn.closest('form'), inf=inp.closest('form');
                return !!(bf&&inf&&bf===inf);
            """, el, input_el)
            if not same_container:
                lbl = (el.text or el.get_attribute("aria-label") or el.get_attribute("value") or "?")[:30]
                trace.append(f"  [target_invalid] '{lbl}' not in same container as input — skipping")
                return False

        # elementFromPoint check — element must not be covered by an overlay
        try:
            rect = driver.execute_script(
                "var r=arguments[0].getBoundingClientRect();"
                "return {x:r.left+r.width/2,y:r.top+r.height/2};", el)
            top_el = driver.execute_script(
                "return document.elementFromPoint(arguments[0],arguments[1]);",
                rect["x"], rect["y"])
            if top_el is not None:
                is_target = driver.execute_script(
                    "return arguments[0]===arguments[1]"
                    "||arguments[0].contains(arguments[1])"
                    "||arguments[1].contains(arguments[0]);",
                    top_el, el)
                if not is_target:
                    top_info = driver.execute_script(
                        "return arguments[0].tagName+' '+(arguments[0].className||'');",
                        top_el)[:50]
                    trace.append(f"  [target_covered] element covered by '{top_info}' — skipping")
                    return False
        except Exception:
            pass  # elementFromPoint failures are non-fatal

        lbl = (el.text or el.get_attribute("aria-label") or el.get_attribute("value") or el.tag_name or "?")[:30]
        trace.append(f"  [target_valid] '{lbl}' passed pre-click validation")
        return True
    except Exception as e:
        trace.append(f"  [target_check_error] {str(e)[:60]}")
        return True  # On error let click proceed — don't block unintentionally


def _detect_ui_interference(driver, trace: list) -> bool:
    """Post-click: detect if a dropdown/menu opened instead of a form submit.

    Returns True if interference detected (wrong element was clicked).

    Only CSS-based: checks for dropdown/menu/combobox elements in open state.
    The bare [aria-expanded='true'] selector is intentionally scoped to dropdown/menu/select
    roles to avoid false positives from tracking-result accordion elements that legitimately
    use aria-expanded when results load. The text-based language keyword check was removed
    because international tracking site footers always contain ≥3 language names, causing
    false rejections of valid results.
    """
    try:
        interference_css = (
            # aria-expanded scoped to interactive selection roles only
            "[aria-expanded='true'][role='listbox'],"
            "[aria-expanded='true'][role='combobox'],"
            "[aria-expanded='true'][role='menu'],"
            "[aria-expanded='true'][class*='dropdown'],"
            "[aria-expanded='true'][class*='autocomplete'],"
            # Explicit display:block dropdowns/menus
            "[class*='dropdown'][style*='display: block'],"
            "[class*='dropdown'][style*='display:block'],"
            "[class*='menu'][style*='display: block'],"
            "[class*='menu'][style*='display:block'],"
            "[class*='popover'][style*='display: block'],"
            "[class*='language'][class*='open'],"
            "[class*='lang'][class*='open'],"
            "[class*='select'][aria-expanded='true']"
        )
        expanded = [e for e in driver.find_elements(By.CSS_SELECTOR, interference_css) if e.is_displayed()]
        if expanded:
            info = []
            for e in expanded[:3]:
                cls = (e.get_attribute("class") or "")[:40]
                txt = (e.text or "")[:30]
                info.append(f"class='{cls}' text='{txt}'")
            trace.append(f"  [ui_interference] open dropdown/menu: {'; '.join(info)}")
            return True

        return False
    except Exception as e:
        trace.append(f"  [interference_check_error] {str(e)[:60]}")
        return False


def _close_ui_interference(driver, trace: list) -> None:
    """Dismiss an open dropdown/menu via Escape + body click."""
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        body.send_keys(Keys.ESCAPE)
        time.sleep(0.3)
        driver.execute_script("document.body.click();")
        trace.append("  [interference_closed] sent Escape + body.click()")
    except Exception as e:
        trace.append(f"  [interference_close_failed] {str(e)[:60]}")


def _detect_result_state(driver, dom_baseline: str, input_value: str, trace: list, min_growth: int = 300) -> bool:
    """Universal result-state detection: has the page transitioned to showing results?

    Checks (in order):
    1. JS container search for result-class elements with non-trivial text content
    2. input_value echoed ≥2× in body (results pages repeat the query)
    3. DOM grew beyond min_growth chars vs baseline

    Returns True if page appears to be in a result state.
    """
    try:
        result_containers = driver.execute_script("""
            var sels=[
                '[class*="result"]','[class*="shipment"]','[class*="tracking"]',
                '[class*="timeline"]','[class*="package"]','[class*="delivery"]',
                '[class*="parcel"]','[class*="trace"]',
                '[id*="result"]','[id*="tracking"]','[id*="shipment"]',
                '.track-result','.tracking-result','table.track'
            ];
            var found=[];
            for(var i=0;i<sels.length;i++){
                var els=document.querySelectorAll(sels[i]);
                for(var j=0;j<els.length;j++){
                    var el=els[j];
                    if(el.offsetParent!==null&&el.textContent.trim().length>50){
                        found.push({sel:sels[i],len:el.textContent.trim().length});
                        break;
                    }
                }
                if(found.length>=2) break;
            }
            return found;
        """) or []
        if result_containers:
            trace.append(f"  [result_state] result container(s) found: {result_containers[:2]}")
            return True

        if input_value:
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text or ""
                echo_count = body_text.upper().count(input_value.upper())
                if echo_count >= 2:
                    trace.append(f"  [result_state] input value echoed {echo_count}× in body")
                    return True
            except Exception:
                pass

        growth = len(_dom_snapshot(driver)) - len(dom_baseline)
        if growth >= min_growth:
            trace.append(f"  [result_state] DOM grew {growth} chars (threshold={min_growth})")
            return True

        trace.append(f"  [result_state_not_detected] containers={len(result_containers)}, growth={len(_dom_snapshot(driver))-len(dom_baseline)}")
        return False
    except Exception as e:
        trace.append(f"  [result_state_check_error] {str(e)[:60]}")
        return False


def _verified_submit(driver, input_el, trace: list, session_name: str, domain: str = "") -> tuple[bool, str]:
    """Attempt to submit a form using 7 strategies with DOM change + interference verification.

    For each strategy:
        1. Snapshot DOM before
        2. Perform action
        3. Wait up to 20s watching for DOM change
        4. If DOM changed → success
        5. If not → try next strategy

    Strategy order is evidence-based: get_optimized_strategy_order() returns
    strategies sorted by efficiency_score from prior executions on this domain.
    Falls back to default order when no data exists.

    After each click, interference detection runs: if a dropdown/menu opened instead
    of a real form submit, the interference is closed and the next strategy is tried.

    Returns (dom_changed: bool, strategy_used: str)
    """
    # Use evidence-based strategy order from reflection engine
    try:
        from ..intent.reflection_engine import get_optimized_strategy_order
        strategies = get_optimized_strategy_order(domain)
    except Exception:
        strategies = [
            "sibling_button_click",   # nearest container button — most reliable for tracking sites
            "enter_key",
            "scrollintoview_click",
            "mouse_event_dispatch",
            "form_submit_js",
            "button_text_discovery",
            "queryselector_js_click",
        ]

    for strategy in strategies:
        dom_before = _dom_snapshot(driver)
        try:
            _url_before_strategy = driver.current_url
        except Exception:
            _url_before_strategy = ""
        trace.append(f"  [click_attempt] strategy={strategy} dom_before_len={len(dom_before)}")

        try:
            if strategy == "sibling_button_click":
                # Walk up from input through parent containers; return nearest visible
                # submit/action button (including div/span acting as buttons).
                # Broader selector set handles React/Vue SPAs that use div[class*="search-area"]
                # instead of real button elements (e.g. 17track.net).
                btn_el = driver.execute_script("""
                    var inp=arguments[0];
                    var a=inp.parentElement;
                    for(var i=0;i<6&&a;i++){
                        var sels=[
                            "[title*='Track the']",
                            "[title*='Search']",
                            "input[type='submit']",
                            "[class*='track-btn']",
                            "[class*='submit-btn']",
                            "[class*='search-area']:not(.hide)",
                            "[role='button']",
                            "button:not([type='reset'])",
                            "div.cursor-pointer"
                        ];
                        for(var s=0;s<sels.length;s++){
                            var btns=a.querySelectorAll(sels[s]);
                            for(var j=0;j<btns.length;j++){
                                var b=btns[j];
                                if(b===inp||b.offsetParent===null||b.disabled) continue;
                                var cls=(b.className||'').toLowerCase();
                                var txt=(b.textContent||b.value||b.title||'').toLowerCase().trim();
                                if(cls.indexOf('nav')>=0||cls.indexOf('lang')>=0
                                   ||cls.indexOf('dropdown')>=0
                                   ||cls.indexOf('filter')>=0) continue;
                                if(txt.indexOf('filter')>=0||txt.indexOf('remove')>=0||txt.indexOf('clear')>=0) continue;
                                if(txt.length>0&&txt.length<80) return b;
                            }
                        }
                        a=a.parentElement;
                    }
                    return null;
                """, input_el)
                if btn_el:
                    if _validate_click_target(driver, btn_el, trace, input_el):
                        lbl = (btn_el.text or btn_el.get_attribute("value") or btn_el.get_attribute("aria-label") or "?")[:30]
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn_el)
                        time.sleep(0.2)
                        btn_el.click()
                        trace.append(f"  [click_attempt] sibling_button_click → '{lbl}'")
                    else:
                        trace.append("  [click_skipped] sibling_button_click target failed pre-click validation")
                        continue
                else:
                    trace.append("  [click_failed] sibling_button_click: no valid sibling button found")
                    continue

            elif strategy == "enter_key":
                input_el.send_keys(Keys.RETURN)

            elif strategy == "scrollintoview_click":
                # Find submit button by CSS selectors
                _CSS_SUBMIT = [
                    "[title*='Track the']",               # title-specific: 17track's TRACK button
                    "[title*='Search']",
                    "button[type='submit']", "input[type='submit']",
                    "button.search-btn", "button[class*='track']",
                    "button[class*='search']", "button[class*='submit']",
                    ".btn-search", ".btn-track", "form button",
                    "[class*='search-area']:not(.hide)",  # 17track-style div buttons (fallback)
                    "button", "[role='button']",
                ]
                btn = None
                driver.implicitly_wait(0)
                try:
                    for sel in _CSS_SUBMIT:
                        try:
                            els = driver.find_elements(By.CSS_SELECTOR, sel)
                            for el in els:
                                if el.is_displayed() and el.is_enabled():
                                    btn = el
                                    break
                        except Exception:
                            pass
                        if btn:
                            break
                finally:
                    driver.implicitly_wait(3)

                if btn:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                    time.sleep(0.2)
                    btn.click()
                    trace.append(f"  [click_attempt] scrollIntoView+click on <{btn.tag_name}>")
                else:
                    trace.append("  [click_failed] no visible button for scrollintoview_click")
                    continue

            elif strategy == "mouse_event_dispatch":
                btn = _find_submit_button_by_text(driver) or (
                    driver.find_elements(By.CSS_SELECTOR, "button, input[type='submit']") or [None]
                )[0]
                if btn and btn.is_displayed():
                    driver.execute_script(
                        "arguments[0].dispatchEvent(new MouseEvent('click',"
                        "{bubbles:true,cancelable:true,view:window}));",
                        btn,
                    )
                    trace.append(f"  [click_attempt] MouseEvent dispatch on <{btn.tag_name}>")
                else:
                    # Fallback: dispatch on input element itself
                    driver.execute_script(
                        "arguments[0].dispatchEvent(new MouseEvent('click',"
                        "{bubbles:true,cancelable:true,view:window}));",
                        input_el,
                    )
                    trace.append("  [click_attempt] MouseEvent dispatch on input element")

            elif strategy == "form_submit_js":
                result = driver.execute_script(
                    "var el=arguments[0];"
                    "var f=el.form || el.closest('form');"
                    "if(f){f.submit();return 'form.submit()';};"
                    "var btn=document.querySelector(\"button[type='submit'],input[type='submit']\");"
                    "if(btn){btn.click();return 'btn.click()';};"
                    "return 'no_form';",
                    input_el,
                )
                trace.append(f"  [click_attempt] form_submit_js → {result}")
                if result == "no_form":
                    trace.append("  [click_failed] no form found for form_submit_js")
                    continue

            elif strategy == "button_text_discovery":
                btn = _find_submit_button_by_text(driver)
                if btn:
                    label = (btn.text or btn.get_attribute("value") or "?")[:30]
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                    time.sleep(0.2)
                    btn.click()
                    trace.append(f"  [click_attempt] button_text_discovery → '{label}'")
                else:
                    trace.append("  [click_failed] no keyword-matching button found")
                    continue

            elif strategy == "queryselector_js_click":
                result = driver.execute_script(
                    "var selectors=["
                    "\"[title*='Track the']\","
                    "\"[title*='Search']\","
                    "\"button[type='submit']\",\"input[type='submit']\","
                    "\"button.search-btn\","
                    "\"[class*='search-area']:not(.hide)\","
                    "\"button\",\"[role='button']\""
                    "];"
                    "for(var i=0;i<selectors.length;i++){"
                    "  var el=document.querySelector(selectors[i]);"
                    "  if(el&&el.offsetParent!==null){el.click();return selectors[i];}"
                    "}"
                    "return null;",
                )
                if result:
                    trace.append(f"  [click_attempt] querySelectorAll JS click → {result}")
                else:
                    trace.append("  [click_failed] queryselector_js_click found nothing")
                    continue

        except Exception as e:
            trace.append(f"  [click_failed] strategy={strategy} exception={str(e)[:80]}")
            continue

        # DOM change verification: wait up to 20s watching for MEANINGFUL content change.
        # Uses abs(_delta) to handle BOTH:
        #   • DOM growth: results loaded inline (SPA update)
        #   • DOM shrinkage/replacement: page navigated to a results URL (full page load)
        # Without abs(), a page navigation from 8912→1362 chars (delta=-7550) is falsely
        # classified as "no change" and the strategy is rejected even though submit worked.
        # Threshold: abs > 200 chars OR abs > 10% of original DOM size.
        # UI noise guard: interference detection runs after any meaningful change for
        # click-based strategies; enter_key / form.submit() cannot open dropdowns.
        _click_based = strategy in (
            "sibling_button_click", "scrollintoview_click",
            "mouse_event_dispatch", "button_text_discovery", "queryselector_js_click",
        )
        dom_changed_flag = False
        _interference_found = False
        deadline = time.time() + 25
        checks = 0
        while time.time() < deadline:
            time.sleep(0.8)
            dom_after = _dom_snapshot(driver)
            checks += 1
            _delta = len(dom_after) - len(dom_before)
            # URL change detection: navigation to a results page is always meaningful,
            # even if the new page DOM is smaller than the original (common for SPAs).
            try:
                _url_now = driver.current_url
                _url_changed = (
                    _url_now != _url_before_strategy
                    and _url_now not in ("data:,", "about:blank", "")
                )
            except Exception:
                _url_changed = False
            _meaningful = (
                abs(_delta) > 200  # significant DOM change in either direction
                or (len(dom_before) > 0 and abs(_delta) > len(dom_before) * 0.10)
                or _url_changed  # navigation to a different URL is always a valid submit
            )
            if _meaningful:
                # Interference check: only for click-based strategies.
                # Enter key and form.submit() never open dropdowns.
                if _click_based and _detect_ui_interference(driver, trace):
                    _close_ui_interference(driver, trace)
                    trace.append(
                        f"  [interference_rejected] strategy={strategy} DOM change discarded "
                        f"(delta={_delta}) — dropdown/menu opened instead of form submit"
                    )
                    _interference_found = True
                    break  # exit wait loop → dom_changed_flag stays False → next strategy
                dom_changed_flag = True
                trace.append(
                    f"  [dom_changed] strategy={strategy} "
                    f"before={len(dom_before)} after={len(dom_after)} delta={_delta} checks={checks}"
                )
                break

        if not dom_changed_flag:
            dom_after = _dom_snapshot(driver)
            _delta = len(dom_after) - len(dom_before)
            trace.append(
                f"  [dom_unchanged] strategy={strategy} "
                f"before={len(dom_before)} after={len(dom_after)} delta={_delta} — trying next strategy"
            )
            continue

        # DOM changed meaningfully — submit was effective
        trace.append(f"  [fallback_used] strategy={strategy} confirmed effective via DOM change")
        return True, strategy

    # All strategies exhausted with no DOM change
    trace.append("  [click_failed] ALL strategies failed — no DOM change detected after any attempt")
    return False, "none"


# ── Shared execution primitives ───────────────────────────────────────────────
# Used by both _do_track and _do_form_submit.
# Extracted to eliminate duplication — _do_track delegates to these.

# Semantic label → CSS selector candidates (checked in order, first match wins)
_LABEL_TO_SELECTORS: dict[str, list[str]] = {
    "email":    ["input[type='email']", "input[name*='email']", "input[id*='email']",
                 "input[placeholder*='email' i]"],
    "password": ["input[type='password']", "input[name*='pass']", "input[id*='pass']"],
    "search":   ["input[type='search']", "input[name='q']", "input[name*='search']",
                 "input[placeholder*='search' i]", "input[placeholder*='buscar' i]"],
    "username": ["input[name*='user']", "input[id*='user']", "input[name*='login']",
                 "input[id*='login']", "input[placeholder*='user' i]"],
    "tracking": ["input.search-input", "input[placeholder*='rack' i]",
                 "input[name*='track']", "input[id*='track']", "input[type='search']",
                 "textarea[placeholder*='rack' i]", "textarea[class*='track']",
                 "textarea",
                 "input[type='text']"],
    "name":     ["input[name*='name']", "input[id*='name']", "input[placeholder*='name' i]"],
    "phone":    ["input[type='tel']", "input[name*='phone']", "input[name*='mobile']"],
    "text":     ["input[type='text']", "textarea",
                 "input:not([type='hidden']):not([type='submit'])"],
    "query":    ["input[type='search']", "input[name='q']", "input[name*='query']",
                 "input[type='text']"],
    "number":   ["input[type='number']", "input[name*='amount']", "input[name*='qty']"],
}


def _resolve_field(
    driver,
    selectors: list[str],
    trace: list,
    field_label: str = "",
    domain: str = "",
) -> tuple:
    """Find first visible, enabled input matching selector list.

    If domain + field_label are given, checks _SELECTOR_REGISTRY (learned selectors) first.
    Falls back to JS (first visible non-hidden input) when all CSS selectors fail.

    Returns (element | None, selector_used: str).
    """
    # 1. Check learned selector registry
    if domain and field_label:
        try:
            import importlib
            ep = importlib.import_module("src.intent.execution_planner")
            known_sel, known = ep.get_selector(domain, field_label)
            if known and known_sel:
                driver.implicitly_wait(0)
                try:
                    for el in driver.find_elements(By.CSS_SELECTOR, known_sel):
                        if el.is_displayed() and el.is_enabled():
                            trace.append(f"  [registry_hit] label={field_label!r} selector={known_sel!r}")
                            return el, known_sel
                except Exception:
                    pass
                finally:
                    driver.implicitly_wait(3)
        except Exception:
            pass

    # 2. Try CSS selectors in order
    driver.implicitly_wait(0)
    input_el = None
    found_selector = ""
    try:
        for sel in selectors:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed() and el.is_enabled():
                        input_el = el
                        found_selector = sel
                        break
            except Exception:
                pass
            if input_el:
                break
    finally:
        driver.implicitly_wait(3)

    if not input_el:
        # 3. JS fallback: first visible non-hidden input
        try:
            input_el = driver.execute_script(
                "return Array.from(document.querySelectorAll('input,textarea')).find("
                "  el=>!['hidden','submit','checkbox','radio','button'].includes(el.type)"
                "  && el.offsetParent!==null);"
            )
            if input_el:
                found_selector = "js:first-visible-input"
        except Exception:
            pass

    return input_el, found_selector


def _fill_field(driver, el, value: str, trace: list, field_label: str = "") -> bool:
    """Type value into element. Uses send_keys + JS fallback for React/Vue controlled inputs.

    Returns True if value was successfully entered.
    """
    lbl = f" ({field_label})" if field_label else ""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        time.sleep(0.3)
        el.clear()
        el.send_keys(value)
        time.sleep(0.4)
        typed_val = el.get_attribute("value") or ""
        if value.upper() not in typed_val.upper():
            # React/Vue controlled input — needs JS dispatch
            driver.execute_script(
                "arguments[0].value=arguments[1];"
                "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                el, value,
            )
            time.sleep(0.3)
            typed_val = el.get_attribute("value") or ""
        trace.append(f"  [field_filled{lbl}] value={typed_val[:40]!r}")
        return True
    except Exception as e:
        trace.append(f"  [field_fill_failed{lbl}] {str(e)[:80]}")
        return False


def _wait_for_result(
    driver,
    dom_baseline: str,
    trace: list,
    wait_s: int = 15,
    keywords_re=None,
    echo_value: str = "",
    success_selector: str = "",
) -> tuple:
    """Poll DOM until keywords / success_selector / echo appear or timeout.

    Returns (result_found: bool, final_body_text: str).
    keywords_re: compiled regex or None.
    """
    result_found = False
    final_text = dom_baseline
    deadline = time.time() + wait_s

    while time.time() < deadline:
        time.sleep(1)
        curr = _dom_snapshot(driver)
        has_kw = bool(keywords_re.search(curr)) if keywords_re else False
        has_echo = echo_value.upper() in curr.upper() if echo_value else False
        grew = len(curr) > len(dom_baseline) + 50
        has_sel = False
        if success_selector:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, success_selector)
                has_sel = any(e.is_displayed() for e in els)
            except Exception:
                pass

        if has_kw or has_sel or (has_echo and grew):
            result_found = True
            final_text = curr
            trace.append(
                f"  [result_loaded] keywords={has_kw} echo={has_echo} "
                f"grew={grew} selector={has_sel}"
            )
            break

        final_text = curr

    if not result_found:
        trace.append(f"  [result_wait_timeout] {wait_s}s elapsed — no explicit signal")
        time.sleep(1)
        final_text = _dom_snapshot(driver)

    return result_found, final_text


def _run_execution_reflection(
    trace_output: str,
    domain: str,
    action_type: str,
    ref_id: str,          # tracking_code for track, url for form_submit
    elapsed_ms: int,
) -> None:
    """Fire-and-forget reflection + EIM recording. Never raises.

    Generalized from _run_reflection — supports any action_type.
    """
    try:
        import importlib
        _rf_mod = importlib.import_module("src.intent.reflection_engine")
        _rf_mod.reflect_on_execution(
            trace_output=trace_output,
            domain=domain,
            action_type=action_type,
            tracking_code=ref_id,
            plan_id=f"{action_type}_{ref_id[:40]}",
            total_time_ms=elapsed_ms,
        )
    except Exception as _rf_err:
        logger.warning("execution.reflection_failed", error=str(_rf_err)[:80])

    # EIM counters — only for tracking actions
    if action_type == "package_tracking":
        try:
            import importlib as _il
            _eim = _il.import_module("src.intent.execution_intelligence")
            _ep = _il.import_module("src.intent.execution_persistence")
            _redis_url = getattr(_ep, "_redis_url", "")
            if _redis_url and domain:
                if "[TRACK_STATUS: FOUND]" in trace_output:
                    _eim_status = "FOUND"
                elif "[TRACK_STATUS: PARTIAL]" in trace_output:
                    _eim_status = "PARTIAL"
                elif "[TRACK_STATUS: NOT_FOUND]" in trace_output:
                    _eim_status = "NOT_FOUND"
                else:
                    _eim_status = "FAILED"
                _eim.record_execution_event(domain, ref_id, _eim_status, _redis_url)
        except Exception:
            pass


def _run_reflection(trace_output: str, domain: str, tracking_code: str, elapsed_ms: int) -> None:
    """Backward-compat wrapper — delegates to _run_execution_reflection."""
    _run_execution_reflection(trace_output, domain, "package_tracking", tracking_code, elapsed_ms)


def _capture_validated_screenshot(
    driver,
    dom_baseline: str,
    input_value: str,
    session_name: str,
    chat_id: str,
    user_id: str,
    trace: list,
    max_attempts: int = 2,
) -> tuple[str, bool]:
    """Capture a screenshot that is guaranteed to represent the validated result state.

    Before capture:
      - Dismisses any open UI interference (dropdowns, menus)
      - Scrolls the result container into the viewport

    After capture:
      - Re-validates result state is still visible (no overlay appeared mid-capture)
      - Re-checks no UI interference appeared

    Retries up to max_attempts times if validation fails.

    Returns (screenshot_path_or_empty, is_valid).
    """
    for attempt in range(1, max_attempts + 1):
        try:
            # ── Pre-capture: dismiss interference and scroll to result area ──────
            if _detect_ui_interference(driver, trace):
                trace.append(f"  [screenshot_prep] interference found before capture (attempt {attempt}) — closing")
                _close_ui_interference(driver, trace)
                time.sleep(0.4)

            # Scroll to first visible result container
            scrolled = driver.execute_script("""
                var sels=[
                    '[class*="result"]','[class*="shipment"]','[class*="tracking"]',
                    '[class*="timeline"]','[class*="package"]','[class*="delivery"]',
                    '[class*="parcel"]','[class*="trace"]',
                    '[id*="result"]','[id*="tracking"]',
                    '.track-result','.tracking-result'
                ];
                for(var i=0;i<sels.length;i++){
                    var els=document.querySelectorAll(sels[i]);
                    for(var j=0;j<els.length;j++){
                        var el=els[j];
                        if(el.offsetParent!==null&&el.textContent.trim().length>50){
                            el.scrollIntoView({block:'center',behavior:'instant'});
                            return sels[i];
                        }
                    }
                }
                window.scrollTo(0,300);
                return null;
            """)
            if scrolled:
                trace.append(f"  [screenshot_prep] scrolled to result container '{scrolled}'")
            time.sleep(0.3)  # Let scroll settle

            # ── Capture ──────────────────────────────────────────────────────────
            shot = _do_screenshot("", session_name, chat_id, user_id)
            trace.append(f"  [screenshot_captured] attempt={attempt} path={shot}")

            # ── Post-capture validation ──────────────────────────────────────────
            # 1. Check UI interference didn't appear during capture
            if _detect_ui_interference(driver, trace):
                trace.append(f"  [screenshot_invalid] interference appeared during capture (attempt {attempt})")
                _close_ui_interference(driver, trace)
                continue  # retry

            # 2. Re-validate result state still present (container still visible)
            post_valid = _detect_result_state(driver, dom_baseline, input_value, trace, min_growth=200)
            if not post_valid:
                trace.append(f"  [screenshot_invalid] result state no longer detected post-capture (attempt {attempt})")
                continue  # retry

            trace.append(f"  [screenshot_valid] attempt={attempt} — result state confirmed post-capture")
            return shot, True

        except Exception as e:
            trace.append(f"  [screenshot_error] attempt={attempt}: {str(e)[:80]}")

    trace.append(f"  [screenshot_failed] all {max_attempts} attempts failed — result exists but cannot be visually confirmed")
    return "", False


def _do_track(tracking_number: str, site: str = "", session_name: str = "track1",
              chat_id: str = "", user_id: str = "") -> str:
    """Dedicated package tracking: navigate → find input → type code → verified submit → results.

    Uses a 6-strategy verified submit system. Each strategy is only considered
    successful when DOM content actually changes — not just when the action fires.

    Tries up to 2 URLs (primary site then 17track fallback).
    Returns a step-by-step diagnostic trace so failures are clearly explained.
    """
    if not tracking_number:
        return "Error: tracking_number is required for track action."

    _t0 = time.time()  # Wall-clock start — used by reflection engine for total_time_ms

    session_name = session_name or "track1"
    tracking_number = tracking_number.strip()

    _RESULT_STATUS_RE = re.compile(
        r"\b(?:"
        r"delivered|delivery|transit|in\s+transit|shipped|dispatched|out\s+for\s+delivery|"
        r"customs|clearance|arrived|departed|picked\s+up|exception|returned|held|"
        r"entregado|entrega|tr[aá]nsito|en\s+tr[aá]nsito|enviado|despachado|"
        r"aduana|lleg[oó]|sali[oó]|recogido|excepci[oó]n|devuelto|retenido|"
        r"\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}|"   # dates
        r"[A-Z]{2}\d{7,11}[A-Z]{2}"                # tracking code echo
        r")\b",
        re.IGNORECASE,
    )

    # Caller (LLM) is responsible for providing the tracking URL — the
    # browser skill no longer falls back to a default aggregator. If site
    # is empty, return an error so the caller picks via web_search.
    if not site.strip():
        return (
            "Error: track action requires url= (the carrier or aggregator page). "
            "Run web_search to find the official tracking page for this carrier first."
        )
    primary_url = site.strip()
    if not primary_url.startswith("http"):
        primary_url = "https://" + primary_url
    urls_to_try = [primary_url]

    _winning_domain = re.sub(r"^www\.", "", primary_url.split("//")[-1].split("/")[0].lower())

    _TRACKING_SELECTORS = [
        "input.search-input",
        "input[placeholder*='rack']",
        "input[placeholder*='racking']",
        "input[name*='track']",
        "input[id*='track']",
        "input[class*='track']",
        "input[type='search']",
        "input[type='text']",
        "textarea[placeholder*='rack']",
        "input:not([type='hidden']):not([type='submit']):not([type='checkbox'])",
    ]

    trace = []

    for url_attempt, url in enumerate(urls_to_try):
        _winning_domain = re.sub(r"^www\.", "", url.split("//")[-1].split("/")[0].lower())
        # ── Step 1: Navigate ────────────────────────────────────────────────
        trace.append(f"[Step 1] Navigating to {url} ...")
        try:
            _do_navigate(url, session_name, _extended_timeout=True)
            driver = _get_driver(session_name)
            current_url = driver.current_url
            page_title = driver.title or ""
            if current_url in ("data:,", "about:blank", ""):
                trace.append(f"[Step 1] FAILED: blocked/blank page at {current_url!r}")
                if url_attempt < len(urls_to_try) - 1:
                    _close_driver(session_name)
                    continue
                break
            trace.append(f"[Step 1] OK — title={page_title[:60]!r} url={current_url[:80]}")
        except Exception as e:
            trace.append(f"[Step 1] FAILED: {str(e)[:100]}")
            if url_attempt < len(urls_to_try) - 1:
                continue
            break

        _dismiss_overlays(session_name)
        time.sleep(0.5)

        # ── Step 2: Find tracking input ─────────────────────────────────────
        trace.append("[Step 2] Locating tracking input field ...")
        driver = _get_driver(session_name)
        input_el, found_selector = _resolve_field(
            driver, _TRACKING_SELECTORS, trace,
            field_label="tracking_input", domain=_winning_domain,
        )

        if not input_el:
            trace.append("[Step 2] FAILED: no tracking input found")
            if url_attempt < len(urls_to_try) - 1:
                _close_driver(session_name)
                continue
            shot = _do_screenshot("", session_name, chat_id, user_id)
            trace.append(f"[Step 2] Screenshot: {shot}")
            trace.append("\n[TRACK_STATUS: FAILED] — No input field found on page.")
            break  # → single exit

        trace.append(f"[Step 2] OK — selector={found_selector!r}")

        # ── Step 3: Type tracking number ─────────────────────────────────────
        trace.append(f"[Step 3] Typing tracking number: {tracking_number} ...")
        if not _fill_field(driver, input_el, tracking_number, trace, "tracking"):
            trace.append("\n[TRACK_STATUS: FAILED] — Could not type into input field.")
            break  # → single exit
        trace.append(f"[Step 3] OK")

        # ── Step 4: Verified submit (6-strategy with evidence-based ordering) ─
        trace.append("[Step 4] Verified submit — trying strategies until DOM changes ...")
        submit_ok, strategy_used = _verified_submit(driver, input_el, trace, session_name, _winning_domain)

        if not submit_ok:
            trace.append("[Step 4] FAILED: All submit strategies exhausted — no DOM change")
            if url_attempt < len(urls_to_try) - 1:
                _close_driver(session_name)
                trace.append("[Step 4] Retrying with next URL ...")
                continue
            shot = _do_screenshot("", session_name, chat_id, user_id)
            trace.append(f"[Step 4] Screenshot of failed state: {shot}")
            trace.append("\n[TRACK_STATUS: FAILED] — Form submission did not produce DOM change.")
            break  # → single exit

        trace.append(f"[Step 4] OK — submit confirmed via DOM change (strategy: {strategy_used})")

        # ── Step 5: Wait for full results to load (universal result-state detection) ─
        # Uses _detect_result_state: result-container JS search + echo count + DOM growth.
        # If not detected in 15s, retry with explicit sibling button click, then re-check.
        trace.append("[Step 5] Waiting for tracking results ...")
        _result_loaded = False
        dom_post_submit = _dom_snapshot(driver)
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                time.sleep(1.5)
                if _detect_result_state(driver, dom_post_submit, tracking_number, trace, min_growth=300):
                    _result_loaded = True
                    trace.append("[Step 5] OK — result state confirmed by universal detection")
                    break
                # Also check classic status-keyword path as a fast-path
                curr = _dom_snapshot(driver)
                if bool(_RESULT_STATUS_RE.search(curr)):
                    _result_loaded = True
                    trace.append("[Step 5] OK — result status keywords detected")
                    break
            if not _result_loaded:
                # Results not detected — retry with sibling_button_click approach then re-check
                trace.append("[Step 5] No results yet — attempting explicit button click retry ...")
                try:
                    _retry_btn = driver.execute_script("""
                        var inp=arguments[0];
                        var a=inp.parentElement;
                        for(var i=0;i<6&&a;i++){
                            var sels=[
                                "button:not([type='reset'])",
                                "input[type='submit']",
                                "[role='button']",
                                "[class*='search-area']:not(.hide)",
                                "[title*='Track the']",
                                "[title*='Search']",
                                "div.cursor-pointer"
                            ];
                            for(var s=0;s<sels.length;s++){
                                var btns=a.querySelectorAll(sels[s]);
                                for(var j=0;j<btns.length;j++){
                                    var b=btns[j];
                                    if(b===inp||b.offsetParent===null||b.disabled) continue;
                                    var cls=(b.className||'').toLowerCase();
                                    if(cls.indexOf('lang')>=0||cls.indexOf('nav')>=0
                                       ||cls.indexOf('dropdown')>=0) continue;
                                    return b;
                                }
                            }
                            a=a.parentElement;
                        }
                        return null;
                    """, input_el)
                    if _retry_btn and _retry_btn.is_displayed():
                        if _validate_click_target(driver, _retry_btn, trace, input_el):
                            driver.execute_script(
                                "arguments[0].scrollIntoView({block:'center'});", _retry_btn
                            )
                            time.sleep(0.3)
                            _retry_btn.click()
                            trace.append("  [step5_retry] clicked sibling button — waiting 10s")
                            _retry_deadline = time.time() + 10
                            while time.time() < _retry_deadline:
                                time.sleep(1.5)
                                if _detect_result_state(driver, dom_post_submit, tracking_number, trace, min_growth=200):
                                    _result_loaded = True
                                    dom_post_submit = _dom_snapshot(driver)
                                    trace.append("  [step5_retry] OK — results loaded after retry")
                                    break
                        else:
                            trace.append("  [step5_retry] retry button failed pre-click validation")
                    if not _result_loaded:
                        # Last resort: Enter key
                        input_el.send_keys(Keys.RETURN)
                        trace.append("  [step5_retry] sent Enter key — waiting 8s")
                        time.sleep(8)
                        if _detect_result_state(driver, dom_post_submit, tracking_number, trace, min_growth=200):
                            _result_loaded = True
                            dom_post_submit = _dom_snapshot(driver)
                            trace.append("  [step5_retry] OK — results loaded after Enter retry")
                except Exception as _e5:
                    trace.append(f"  [step5_retry_failed] {str(_e5)[:80]}")
                if not _result_loaded:
                    trace.append("[Step 5] WARNING: results not confirmed after retry")
        except Exception:
            trace.append("[Step 5] WARNING: wait loop error")

        _dismiss_overlays(session_name)

        # ── Step 6: Extract results + validated screenshot + TRACK_STATUS ─────
        # STRICT classification rules:
        # FOUND   — status keywords detected in page body
        # PARTIAL — Step 5 confirmed results loaded, but no clear status keyword
        # NOT_FOUND — Step 5 timed out: submit did not produce real result content.
        #
        # Screenshot is ONLY accepted if _capture_validated_screenshot confirms:
        #   • No UI interference at capture time
        #   • Result container still visible in viewport post-capture
        # If screenshot cannot be validated: result is still reported but with a
        # clear note that visual evidence could not be confirmed.
        trace.append("[Step 6] Capturing results and validating screenshot ...")
        try:
            body_text = _dom_snapshot(driver)

            _has_status = bool(_RESULT_STATUS_RE.search(body_text))
            # Require code to appear 2+ times: once in input, once in result section.
            _body_code_count = body_text.upper().count(tracking_number.upper())
            _has_code_in_results = _body_code_count >= 2

            if _has_status and _has_code_in_results:
                track_status = "FOUND"
                status_note  = "Tracking status and code confirmed in results section."
            elif _has_status:
                track_status = "FOUND"
                status_note  = "Tracking status keywords found in page content."
            elif _result_loaded and _has_code_in_results:
                track_status = "PARTIAL"
                status_note  = "Results section loaded (Step 5 confirmed) but status keywords not parsed."
            else:
                track_status = "NOT_FOUND"
                status_note  = (
                    "No tracking results visible after submit. "
                    "Form submit may not have triggered result load "
                    f"(Step 5 result_loaded={_result_loaded}, code_count={_body_code_count})."
                )

            # Only attempt validated screenshot when we have a real result
            if track_status in ("FOUND", "PARTIAL"):
                shot, shot_valid = _capture_validated_screenshot(
                    driver, dom_post_submit, tracking_number,
                    session_name, chat_id, user_id, trace,
                )
                if shot_valid:
                    trace.append(f"[Step 6] OK — screenshot validated: {shot}")
                else:
                    # Result detected but visual evidence unconfirmable
                    trace.append("[Step 6] WARNING — result detected but screenshot validation failed")
                    status_note += " Se obtuvo el resultado, pero no se pudo capturar correctamente la evidencia visual."
            else:
                # For NOT_FOUND: take a plain screenshot for diagnostic purposes only
                shot = _do_screenshot("", session_name, chat_id, user_id)
                shot_valid = False
                trace.append(f"[Step 6] diagnostic screenshot (NOT_FOUND): {shot}")

            # TRACK_STATUS MUST appear before the body dump so it survives action_history
            # output truncation (2000-4096 chars). Body text can push it past the limit.
            trace.append(f"\n[TRACK_STATUS: {track_status}] — {status_note}")
            trace.append(f"\nTracking results for {tracking_number}:\n{body_text[:3000]}")
        except Exception as e:
            trace.append(f"[Step 6] FAILED: {str(e)[:80]}")
            trace.append("\n[TRACK_STATUS: FAILED] — Could not capture results.")

        break  # Success or handled failure — exit URL loop

    # ── Single exit point: always run reflection before returning ─────────────
    _output = "\n".join(trace) if trace else f"Tracking failed for {tracking_number}: all URLs exhausted."
    _elapsed_ms = int((time.time() - _t0) * 1000)
    _run_reflection(_output, _winning_domain, tracking_number, _elapsed_ms)
    return _output


def _do_form_submit(
    url: str,
    fields: "list | str",
    session_name: str = "form1",
    chat_id: str = "",
    user_id: str = "",
    success_keywords: str = "",
    success_selector: str = "",
    wait_seconds: int = 15,
    action_type: str = "form_submit",
) -> str:
    """Universal form interaction engine.

    Navigate to URL → fill fields → DOM-validated submit → classify result.
    Integrates with Reflection Engine, Strategy Scoring, and Selector Learning.

    Fields format (JSON string or list of dicts):
      [{"selector": "input[name='q']", "value": "python docs"}]
      [{"label": "email", "value": "user@example.com"},
       {"label": "password", "value": "secret"}]

    Supported labels: email, password, search, username, tracking, name,
                      phone, text, query, number — or any custom string.

    success_keywords: comma-separated words that confirm success (e.g. "Welcome,Dashboard")
    success_selector: CSS selector that appears on success (e.g. ".alert-success")
    wait_seconds: how long to poll for result (default 15)
    action_type: label for reflection engine (e.g. "login", "search", "form_submit")

    Returns step-by-step diagnostic trace with [FORM_STATUS: SUCCESS/SUBMITTED/ERROR/FAILED].
    """
    import json as _json

    if not url:
        return "Error: url is required for form_submit."
    if not fields:
        return "Error: fields is required for form_submit."

    if isinstance(fields, str):
        try:
            fields = _json.loads(fields)
        except Exception as exc:
            return f"Error: fields must be a JSON array. {str(exc)[:80]}"

    if not isinstance(fields, list) or not fields:
        return "Error: fields must be a non-empty list."

    _t0 = time.time()
    session_name = session_name or "form1"

    if not url.startswith("http"):
        url = "https://" + url
    domain = re.sub(r"^www\.", "", url.split("//")[-1].split("/")[0].lower())

    _ERROR_RE = re.compile(
        r"\b(?:error|invalid|incorrect|failed|wrong|not found|no result"
        r"|inválido|incorrecto|no encontrado|sin resultado|falló)\b",
        re.IGNORECASE,
    )

    _kw_re = None
    if success_keywords:
        kw_list = [re.escape(k.strip()) for k in success_keywords.split(",") if k.strip()]
        if kw_list:
            _kw_re = re.compile("|".join(kw_list), re.IGNORECASE)

    trace: list[str] = []
    trace.append(f"[form_submit] url={url} fields={len(fields)} action={action_type}")

    # ── Step 1: Navigate ──────────────────────────────────────────────────────
    trace.append(f"[Step 1] Navigating to {url} ...")
    try:
        _do_navigate(url, session_name, _extended_timeout=True)
        driver = _get_driver(session_name)
        page_title = driver.title or ""
        current_url = driver.current_url
        if current_url in ("data:,", "about:blank", ""):
            trace.append("[Step 1] FAILED: blank/blocked page")
            trace.append("\n[FORM_STATUS: FAILED] — Page blocked or not loaded.")
            return "\n".join(trace)
        trace.append(f"[Step 1] OK — title={page_title[:60]!r}")
    except Exception as e:
        trace.append(f"[Step 1] FAILED: {str(e)[:100]}")
        trace.append("\n[FORM_STATUS: FAILED] — Navigation failed.")
        return "\n".join(trace)

    _dismiss_overlays(session_name)
    time.sleep(0.5)

    # ── Step 2: Resolve and fill fields ───────────────────────────────────────
    driver = _get_driver(session_name)
    _last_filled_el = None
    fields_filled = 0
    fields_failed: list[str] = []
    learned_selectors: list[tuple[str, str]] = []

    for i, field_spec in enumerate(fields, 1):
        label    = str(field_spec.get("label", ""))
        selector = str(field_spec.get("selector", ""))
        value    = str(field_spec.get("value", ""))
        required = field_spec.get("required", True)

        trace.append(
            f"[Step 2.{i}] label={label!r} selector={selector!r} value={value[:30]!r}"
        )

        if selector:
            selectors = [selector]
        elif label:
            selectors = _LABEL_TO_SELECTORS.get(label.lower()) or [
                f"input[placeholder*='{label}' i]",
                f"input[name*='{label}']",
                f"input[id*='{label}']",
                "input[type='text']",
            ]
        else:
            trace.append("  [field_skip] no selector or label — skipping")
            continue

        el, found_sel = _resolve_field(
            driver, selectors, trace,
            field_label=label or selector,
            domain=domain,
        )

        if not el:
            trace.append(f"  [field_not_found] label={label!r} selector={selector!r}")
            if required:
                fields_failed.append(label or selector)
            continue

        trace.append(f"  [field_resolved] selector={found_sel!r}")
        if _fill_field(driver, el, value, trace, label):
            _last_filled_el = el
            fields_filled += 1
            if label and found_sel and found_sel not in ("js:first-visible-input",):
                learned_selectors.append((label, found_sel))
        elif required:
            fields_failed.append(label or selector)

    if fields_failed:
        trace.append(f"[Step 2] FAILED: required fields not filled: {fields_failed}")
        shot = _do_screenshot("", session_name, chat_id, user_id)
        trace.append(f"[Step 2] Screenshot: {shot}")
        trace.append("\n[FORM_STATUS: FAILED] — Required fields could not be located.")
        _run_execution_reflection("\n".join(trace), domain, action_type, url, int((time.time()-_t0)*1000))
        return "\n".join(trace)

    trace.append(f"[Step 2] OK — {fields_filled}/{len(fields)} fields filled")

    # ── Step 3: Verified submit ───────────────────────────────────────────────
    trace.append("[Step 3] Verified submit — trying strategies until DOM changes ...")

    # Use last filled element as the submit anchor
    submit_el = _last_filled_el
    if not submit_el:
        try:
            els = driver.find_elements(
                By.CSS_SELECTOR,
                "textarea, input:not([type='hidden']):not([type='submit'])",
            )
            submit_el = els[-1] if els else None
        except Exception:
            pass

    if not submit_el:
        trace.append("[Step 3] FAILED: no input element for submit")
        trace.append("\n[FORM_STATUS: FAILED] — No submittable element found.")
        _run_execution_reflection("\n".join(trace), domain, action_type, url, int((time.time()-_t0)*1000))
        return "\n".join(trace)

    submit_ok, strategy_used = _verified_submit(driver, submit_el, trace, session_name, domain)

    if not submit_ok:
        trace.append("[Step 3] FAILED: all submit strategies exhausted")
        shot = _do_screenshot("", session_name, chat_id, user_id)
        trace.append(f"[Step 3] Screenshot: {shot}")
        trace.append("\n[FORM_STATUS: FAILED] — Form submission did not produce DOM change.")
        _run_execution_reflection("\n".join(trace), domain, action_type, url, int((time.time()-_t0)*1000))
        return "\n".join(trace)

    trace.append(f"[Step 3] OK — submit confirmed via DOM change (strategy: {strategy_used})")

    # ── Step 4: Wait for result ───────────────────────────────────────────────
    trace.append(f"[Step 4] Waiting up to {wait_seconds}s for result ...")
    dom_post_submit = _dom_snapshot(driver)
    _first_echo = str(fields[0].get("value", "")) if fields else ""
    _result_found, body_text = _wait_for_result(
        driver, dom_post_submit, trace,
        wait_s=int(wait_seconds),
        keywords_re=_kw_re,
        echo_value=_first_echo,
        success_selector=success_selector,
    )
    _dismiss_overlays(session_name)

    # ── Step 5: Classify result + screenshot ─────────────────────────────────
    trace.append("[Step 5] Classifying result ...")
    body_final = _dom_snapshot(driver)

    dom_grew   = len(body_final) > len(dom_post_submit) + 80
    has_error  = bool(_ERROR_RE.search(body_final))
    has_kw     = bool(_kw_re.search(body_final)) if _kw_re else False
    has_sel    = False
    if success_selector:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, success_selector)
            has_sel = any(e.is_displayed() for e in els)
        except Exception:
            pass

    if has_kw or has_sel:
        form_status = "SUCCESS"
        status_note = "Success signal confirmed."
    elif dom_grew and not has_error:
        form_status = "SUBMITTED"
        status_note = "DOM changed — form accepted. No explicit success/error marker."
    elif has_error:
        form_status = "ERROR"
        status_note = "Error keywords detected in page response."
    else:
        form_status = "FAILED"
        status_note = "No DOM change after submit."

    # Screenshot: validated when result detected, plain otherwise
    if form_status in ("SUCCESS", "SUBMITTED"):
        shot, shot_valid = _capture_validated_screenshot(
            driver, dom_post_submit, _first_echo,
            session_name, chat_id, user_id, trace,
        )
        if not shot:
            shot = _do_screenshot("", session_name, chat_id, user_id)
    else:
        shot = _do_screenshot("", session_name, chat_id, user_id)

    trace.append(f"[Step 5] OK — {shot}")
    trace.append(f"\nPage response:\n{body_final[:3000]}")
    trace.append(f"\n[FORM_STATUS: {form_status}] — {status_note}")

    # ── Selector learning ─────────────────────────────────────────────────────
    if form_status in ("SUCCESS", "SUBMITTED") and learned_selectors:
        try:
            import importlib as _il
            ep = _il.import_module("src.intent.execution_planner")
            for lbl, sel in learned_selectors:
                ep.register_learned_selector(domain, lbl, sel)
                trace.append(f"[selector_learned] domain={domain} label={lbl!r} selector={sel!r}")
        except Exception:
            pass

    _output = "\n".join(trace)
    _elapsed_ms = int((time.time() - _t0) * 1000)
    _run_execution_reflection(_output, domain, action_type, url, _elapsed_ms)
    return _output


# Action dispatch table — all actions receive full kwargs dict including session
_ACTIONS = {
    "navigate": lambda kwargs: _do_navigate(kwargs.get("url", ""), kwargs.get("session", "")),
    "capture": lambda kwargs: _do_capture(kwargs.get("url", ""), kwargs.get("session", "s1"), kwargs.get("selector", ""), kwargs.get("chat_id", ""), kwargs.get("user_id", ""), kwargs.get("task_hint", "")),
    "click": lambda kwargs: _do_click(kwargs.get("selector", ""), kwargs.get("session", "")),
    "type": lambda kwargs: _do_type(
        kwargs.get("selector", ""),
        kwargs.get("text", ""),
        kwargs.get("submit", "").lower() in ("true", "1", "yes"),
        kwargs.get("session", ""),
    ),
    "get_text": lambda kwargs: _do_get_text(kwargs.get("selector", ""), kwargs.get("session", "")),
    "screenshot": lambda kwargs: _do_screenshot(kwargs.get("selector", ""), kwargs.get("session", ""), kwargs.get("chat_id", ""), kwargs.get("user_id", "")),
    "find_elements": lambda kwargs: _do_find_elements(kwargs.get("selector", "")),
    "execute_js": lambda kwargs: _do_execute_js(kwargs.get("script", "")),
    "scroll_capture": lambda kwargs: _do_scroll_capture(kwargs.get("url", ""), kwargs.get("session", "s1"), int(kwargs.get("scroll_count", 0)), kwargs.get("chat_id", ""), kwargs.get("user_id", "")),
    "scroll": lambda kwargs: _do_scroll(kwargs.get("direction", "down"), kwargs.get("session", "")),
    "form_submit": lambda kwargs: _do_form_submit(
        url=kwargs.get("url", ""),
        fields=kwargs.get("fields", []),
        session_name=kwargs.get("session", "form1"),
        chat_id=kwargs.get("chat_id", ""),
        user_id=kwargs.get("user_id", ""),
        success_keywords=kwargs.get("success_keywords", ""),
        success_selector=kwargs.get("success_selector", ""),
        wait_seconds=int(kwargs.get("wait_seconds", 15)),
        action_type=kwargs.get("action_type", "form_submit"),
    ),
    "back": lambda _: _do_back(),
    "submit": lambda kwargs: _do_submit(kwargs.get("selector", ""), kwargs.get("session", "")),
    "close": lambda kwargs: _do_close(kwargs.get("session", "")),
    "sessions": lambda _: _list_sessions(),
    "track": lambda kwargs: _do_track(
        kwargs.get("tracking_number", kwargs.get("text", "")),
        kwargs.get("url", kwargs.get("site", "")),
        kwargs.get("session", "track1"),
        kwargs.get("chat_id", ""),
        kwargs.get("user_id", ""),
    ),
}


class BrowserSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="browser",
            description=(
                "Control a headless Chromium browser. "
                "PACKAGE TRACKING: track(tracking_number, url=<carrier_url>, session='track1') — "
                "full automated tracking: navigate → find input → enter code → submit → capture results. "
                "The url= argument is required: pick the carrier's page (resolve via web_search if unknown) — "
                "no default aggregator is assumed. "
                "SCREENSHOT ACTIONS: "
                "capture(url, session='s1', task_hint='brief intent') — navigate + screenshot + CONTENT VALIDATION in ONE call. "
                "Pass task_hint so the validator can confirm the correct content loaded (e.g. task_hint='BTC/USDT chart'). "
                "Returns [CAPTURE_STATUS: SUCCESS] or [CAPTURE_STATUS: FAILED] with reason. "
                "scroll_capture(url, session='s1', scroll_count=0) — navigate then capture the FULL page by scrolling. "
                "Other actions: navigate(url, session='') returns page TEXT only (no image); "
                "screenshot(session='s1') captures current page (must navigate first, same session); "
                "click(selector, session=''); type(selector, text, submit, session=''); get_text(selector, session=''); "
                "find_elements(selector); execute_js(script); scroll(direction, session=''); "
                "back; close(session=''); sessions. "
                "IMPORTANT: After capture/scroll_capture/track, images are sent to the user automatically. "
                "In your response text write ONE short sentence saying what you captured — do NOT include image markdown or file paths. "
                "If [CAPTURE_STATUS: FAILED] is returned, report honestly: what was blocking the content."
            ),
            params=[
                SkillParam(
                    name="action",
                    param_type=ParamType.STRING,
                    description="Action: track, capture, scroll_capture, navigate, screenshot, click, type, get_text, find_elements, execute_js, scroll, back, close, sessions",
                ),
                SkillParam(
                    name="url",
                    param_type=ParamType.STRING,
                    description="URL for navigate action",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="session",
                    param_type=ParamType.STRING,
                    description="Named browser session (persists cookies/login). E.g. 'twitter', 'gmail'. Default is shared stateless browser.",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="selector",
                    param_type=ParamType.STRING,
                    description="CSS selector for click/type/get_text/find_elements",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="text",
                    param_type=ParamType.STRING,
                    description="Text to type",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="submit",
                    param_type=ParamType.STRING,
                    description="Press Enter after typing (true/false)",
                    required=False,
                    default="false",
                ),
                SkillParam(
                    name="script",
                    param_type=ParamType.STRING,
                    description="JavaScript code for execute_js",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="direction",
                    param_type=ParamType.STRING,
                    description="Scroll direction: down, up, top, bottom",
                    required=False,
                    default="down",
                ),
                SkillParam(
                    name="tracking_number",
                    param_type=ParamType.STRING,
                    description="Package tracking code for track action (e.g. LJ040128393CN)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="site",
                    param_type=ParamType.STRING,
                    description="Tracking site URL for track action (default: 17track.net)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="task_hint",
                    param_type=ParamType.STRING,
                    description="Optional: brief description of the intended content (e.g. 'BTC/USDT price chart'). Used for page validation to confirm the correct content loaded.",
                    required=False,
                    default="",
                ),
            ],
            category="web",
            timeout_seconds=120.0,  # track action needs up to 2 min for slow sites
        )

    async def execute(self, action: str = "navigate", **kwargs) -> SkillResult:
        action = action.lower().strip()

        # ── LLM-friendly action aliases ────────────────────────────────
        # The model occasionally invents action names that don't exist;
        # map common ones to the closest real action so a single round
        # isn't wasted on "Unknown action: <name>".
        # NOTE: `submit` is a first-class action (not aliased) because the
        # LLM frequently calls it with no selector to mean "submit the
        # current form" — aliasing to `click` would require a selector.
        _ACTION_ALIASES = {
            "fill": "type",
            "input": "type",
            "press": "click",
            "tap": "click",
            "open": "navigate",
            "goto": "navigate",
        }
        if action in _ACTION_ALIASES:
            action = _ACTION_ALIASES[action]

        handler = _ACTIONS.get(action)
        if not handler:
            return SkillResult(
                skill_name="browser",
                success=False,
                output="",
                error=f"Unknown action: {action}. Use: {', '.join(_ACTIONS.keys())}",
            )

        # ── Engine routing ─────────────────────────────────────────────
        # Two layers of decision-making coexist:
        #
        # 1. Session engine binding (durable). Once a session has done a
        #    successful action on one engine, subsequent actions stick to
        #    the same engine — cookies and login state survive in-process.
        #    The binding lives in Redis (`browser:engine:{session}`,
        #    TTL 1 h), refreshed on every successful call.
        #
        # 2. Intent-aware first-call routing. For an UNBOUND session,
        #    navigate / capture try nodriver first (stealth-friendly);
        #    everything else goes to Selenium. The successful engine is
        #    then bound to the session.
        #
        # When a session is bound to nodriver but the requested action
        # has no nodriver implementation (execute_js / scroll_capture /
        # form_submit / find_elements / track / sessions), we log
        # `engine_drift`, fall through to Selenium, and re-bind the
        # session — accepting the cookie discontinuity rather than
        # blocking the action.
        _NODRIVER_PRIMARY_ACTIONS = {"navigate", "capture"}
        _ND_HANDLERS = {
            "navigate": "navigate",
            "capture": "navigate_capture",
            "click": "click",
            "type": "type_text",
            "submit": "submit",
            "screenshot": "screenshot",
            "get_text": "get_text",
            "scroll": "scroll",
            "back": "back",
            "close": "close",
        }
        # Compound / Selenium-specific actions that bypass per-call routing
        # — they shouldn't write a session binding because the LLM may
        # follow them with regular routable actions.
        _BYPASS_ROUTING_ACTIONS = {
            "track", "form_submit", "scroll_capture", "find_elements",
            "execute_js", "sessions",
        }
        _url = kwargs.get("url", "") or ""
        _session = kwargs.get("session", "s1") or "s1"
        _route_via_nodriver = action in _NODRIVER_PRIMARY_ACTIONS and bool(_url)

        async def _drain_visual_queue() -> None:
            try:
                from ...memory.visual import store_screenshot as _vm_store
                while True:
                    try:
                        _vm_meta = _visual_memory_queue.get_nowait()
                    except queue.Empty:
                        break
                    asyncio.ensure_future(_vm_store(**_vm_meta))
            except Exception:
                pass

        async def _tm_bump_safe(field: str) -> None:
            try:
                from ...observability.truth_metrics import bump as _bump
                import os as _os
                await _bump(_os.environ.get("REDIS_URL", ""), field)
            except Exception:
                pass

        async def _get_session_engine(sess: str) -> str | None:
            try:
                import os as _os
                from redis import asyncio as aioredis
                _url2 = _os.environ.get("REDIS_URL", "")
                if not _url2:
                    return None
                cli = aioredis.from_url(_url2, decode_responses=True)
                try:
                    return await cli.get(f"browser:engine:{sess}")
                finally:
                    await cli.aclose()
            except Exception:
                return None

        async def _set_session_engine(sess: str, engine: str) -> None:
            try:
                import os as _os
                from redis import asyncio as aioredis
                _url2 = _os.environ.get("REDIS_URL", "")
                if not _url2:
                    return
                cli = aioredis.from_url(_url2, decode_responses=True)
                try:
                    await cli.set(f"browser:engine:{sess}", engine, ex=3600)
                finally:
                    await cli.aclose()
            except Exception:
                pass

        async def _call_nodriver_handler(handler_name: str) -> str | None:
            from . import browser_nodriver as _nd
            fn = getattr(_nd, handler_name, None)
            if fn is None:
                return None
            try:
                if handler_name == "navigate_capture":
                    return await asyncio.wait_for(
                        fn(
                            url=_url,
                            session_name=_session,
                            chat_id=kwargs.get("chat_id", ""),
                            user_id=kwargs.get("user_id", ""),
                            task_hint=kwargs.get("task_hint", ""),
                        ),
                        timeout=45.0,
                    )
                if handler_name == "navigate":
                    return await asyncio.wait_for(
                        fn(
                            url=_url,
                            session_name=_session,
                            chat_id=kwargs.get("chat_id", ""),
                            user_id=kwargs.get("user_id", ""),
                            task_hint=kwargs.get("task_hint", ""),
                        ),
                        timeout=45.0,
                    )
                if handler_name == "click":
                    return await asyncio.wait_for(
                        fn(selector=kwargs.get("selector", ""), session_name=_session),
                        timeout=20.0,
                    )
                if handler_name == "type_text":
                    submit_flag = str(kwargs.get("submit", "")).lower() in ("true", "1", "yes")
                    return await asyncio.wait_for(
                        fn(
                            selector=kwargs.get("selector", ""),
                            text=kwargs.get("text", ""),
                            submit=submit_flag,
                            session_name=_session,
                        ),
                        timeout=20.0,
                    )
                if handler_name == "screenshot":
                    return await asyncio.wait_for(
                        fn(
                            selector=kwargs.get("selector", ""),
                            session_name=_session,
                            chat_id=kwargs.get("chat_id", ""),
                            user_id=kwargs.get("user_id", ""),
                            task_hint=kwargs.get("task_hint", ""),
                        ),
                        timeout=30.0,
                    )
                if handler_name == "get_text":
                    return await asyncio.wait_for(
                        fn(selector=kwargs.get("selector", ""), session_name=_session),
                        timeout=15.0,
                    )
                if handler_name == "scroll":
                    return await asyncio.wait_for(
                        fn(direction=kwargs.get("direction", "down"), session_name=_session),
                        timeout=10.0,
                    )
                if handler_name == "back":
                    return await asyncio.wait_for(fn(session_name=_session), timeout=10.0)
                if handler_name == "close":
                    return await asyncio.wait_for(fn(session_name=_session), timeout=10.0)
                return None
            except asyncio.TimeoutError:
                return f"[NODRIVER_FAIL: {handler_name} timed out]"
            except Exception as e:
                logger.warning(
                    "browser.nodriver_handler_exception",
                    handler=handler_name, error=str(e)[:200],
                )
                return f"[NODRIVER_FAIL: {str(e)[:200]}]"

        bound_engine = await _get_session_engine(_session)

        # ── Bound to nodriver: try nodriver first ──────────────────────
        if bound_engine == "nodriver":
            nd_handler = _ND_HANDLERS.get(action)
            if nd_handler is not None:
                logger.info(
                    "browser.nodriver_session_call",
                    action=action, session=_session,
                )
                nd_result = await _call_nodriver_handler(nd_handler)
                await _drain_visual_queue()
                # Cloudflare block — terminal. Do NOT drift to Selenium:
                # both engines hit the same WAF, retry just wastes rounds.
                if isinstance(nd_result, str) and "[CLOUDFLARE_BLOCKED:" in nd_result:
                    await _tm_bump_safe("browser_cloudflare_blocked")
                    return SkillResult(skill_name="browser", success=False, output=nd_result)
                if isinstance(nd_result, str) and not nd_result.startswith("[NODRIVER_FAIL"):
                    success_flag = "[CAPTURE_VALID: false]" not in nd_result
                    if success_flag:
                        await _set_session_engine(_session, "nodriver")
                    return SkillResult(skill_name="browser", success=True, output=nd_result)
                # nodriver returned a failure marker → drift to Selenium.
                logger.info(
                    "browser.engine_drift_to_selenium",
                    session=_session, action=action,
                    reason=(nd_result or "")[:120],
                )
                await _tm_bump_safe("browser_engine_drift_to_selenium")
                # fall through
            else:
                # action has no nodriver implementation → drift to Selenium.
                logger.info(
                    "browser.engine_drift_to_selenium",
                    session=_session, action=action,
                    reason="no_nodriver_handler",
                )
                await _tm_bump_safe("browser_engine_drift_to_selenium_no_handler")
                # fall through

        # ── Unbound + navigate/capture: try nodriver primary ───────────
        elif bound_engine != "selenium" and _route_via_nodriver:
            await _tm_bump_safe("browser_nodriver_primary_attempted")
            logger.info(
                "browser.nodriver_primary_attempt",
                action=action, url=_url[:80], session=_session,
            )
            # `capture` runs the validator; `navigate` does not. Pick the
            # correct nodriver handler so plain navigate doesn't pay the
            # screenshot cost.
            nd_handler = "navigate_capture" if action == "capture" else "navigate"
            nd_result = await _call_nodriver_handler(nd_handler)
            await _drain_visual_queue()

            # Cloudflare block on primary attempt — terminal. Selenium will
            # hit the same WAF, so don't waste a fallback round.
            if isinstance(nd_result, str) and "[CLOUDFLARE_BLOCKED:" in nd_result:
                await _tm_bump_safe("browser_cloudflare_blocked")
                return SkillResult(skill_name="browser", success=False, output=nd_result)

            nd_failed = (
                not isinstance(nd_result, str)
                or nd_result.startswith("[NODRIVER_FAIL")
                or "[CAPTURE_VALID: false]" in nd_result
            )
            if not nd_failed:
                await _tm_bump_safe("browser_nodriver_primary_succeeded")
                logger.info("browser.nodriver_primary_succeeded", url=_url[:80])
                await _set_session_engine(_session, "nodriver")
                return SkillResult(skill_name="browser", success=True, output=nd_result)

            await _tm_bump_safe("browser_nodriver_primary_failed")
            logger.info(
                "browser.nodriver_primary_failed",
                url=_url[:80],
                preview=(nd_result[:120] if isinstance(nd_result, str) else "exception"),
            )
            await _tm_bump_safe("browser_selenium_fallback_attempted")
            # fall through to Selenium

        # ── Selenium path ──────────────────────────────────────────────
        try:
            result = await asyncio.to_thread(handler, kwargs)
            await _drain_visual_queue()

            # Cloudflare block detected by Selenium too — return terminal,
            # do NOT bind the session to selenium.
            if isinstance(result, str) and "[CLOUDFLARE_BLOCKED:" in result:
                await _tm_bump_safe("browser_cloudflare_blocked")
                return SkillResult(skill_name="browser", success=False, output=result)

            if isinstance(result, str):
                clean = (
                    "[NODRIVER_FAIL" not in result
                    and not result.startswith("Message:")
                    and not result.startswith("Unknown action:")
                )
                # Write a session binding only when Selenium was the
                # natural choice for this call:
                #   - the result was clean, AND
                #   - the session was not already bound, AND
                #   - the action is not a Selenium-only bypass (track,
                #     execute_js, etc.), AND
                #   - Selenium did NOT run as a recovery fallback after
                #     nodriver failed — in that case give nodriver another
                #     chance on the next call instead of locking selenium
                #     in for an hour.
                if (
                    clean
                    and bound_engine is None
                    and action not in _BYPASS_ROUTING_ACTIONS
                    and not _route_via_nodriver
                ):
                    await _set_session_engine(_session, "selenium")

                if _route_via_nodriver:
                    # For `capture` we have a validation marker; for plain
                    # `navigate` success is "no NODRIVER_FAIL and not an
                    # invalid capture marker".
                    if action == "capture":
                        sel_ok = "[CAPTURE_VALID: true]" in result
                    else:
                        sel_ok = clean
                    if sel_ok:
                        await _tm_bump_safe("browser_selenium_fallback_succeeded")
                        logger.info("browser.selenium_fallback_succeeded", url=_url[:80])
                    else:
                        await _tm_bump_safe("browser_selenium_fallback_failed")
                        logger.info(
                            "browser.selenium_fallback_failed",
                            url=_url[:80],
                            preview=result[:120],
                        )

            return SkillResult(skill_name="browser", success=True, output=result)
        except Exception as e:
            logger.exception("browser.action_error", action=action)
            return SkillResult(skill_name="browser", success=False, output="", error=str(e))
