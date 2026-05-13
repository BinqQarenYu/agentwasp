"""Generic page validation and screenshot truth system.

Validates whether the page shown to the browser actually contains the intended
content, rather than a blocking UI (login wall, captcha, cookie gate, etc.).

No site-specific rules.  All detection is generic.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from selenium import webdriver

logger = structlog.get_logger()


# ── Negative signal patterns (text-based) ────────────────────────────────────

_BLOCK_TEXT_PATTERNS: list[tuple[str, str]] = [
    # (signal_id, regex_pattern)
    ("login_wall",        r"\b(?:sign\s+in|log\s+in|login|iniciar\s+sesi[oó]n|entrar|acceder)\b"),
    ("signup_wall",       r"\b(?:sign\s+up|create\s+(?:an?\s+)?account|register|registr[ae]rse|crear\s+cuenta)\b"),
    ("welcome_gate",      r"\b(?:welcome\s+back|bienvenido|welcome\s+to\b)"),
    ("password_prompt",   r"\b(?:enter\s+(?:your\s+)?password|your\s+password|contrase[ñn]a)\b"),
    ("captcha_text",      r"\b(?:prove\s+you.re\s+(?:not|human)|verify\s+you.re|captcha|im\s+not\s+a\s+robot|robot|recaptcha|hcaptcha)\b"),
    ("privacy_gate",      r"\b(?:accept\s+(?:the\s+)?privacy|cookie\s+policy|we\s+use\s+cookies\b.*\baccept)\b"),
    ("access_denied",     r"\b(?:access\s+denied|403\s+forbidden|not\s+authorized|unauthorized|forbidden|blocked|suspendido|bloqueado)\b"),
    ("geo_block",         r"\b(?:not\s+available\s+in\s+your\s+(?:region|country)|unavailable\s+in\s+your|no\s+disponible\s+en\s+tu)\b"),
    ("age_gate",          r"\b(?:confirm\s+your\s+age|are\s+you\s+(?:18|21)|verify\s+age|verificar\s+edad)\b"),
    ("continue_oauth",    r"\b(?:continue\s+with\s+(?:google|facebook|apple|twitter)|sign\s+in\s+with\s+(?:google|apple))\b"),
    ("error_page",        r"\b(?:page\s+not\s+found|404|something\s+went\s+wrong|server\s+error|500|oops|lo\s+sentimos)\b"),
    ("maintenance",       r"\b(?:under\s+maintenance|site\s+is\s+down|temporarily\s+unavailable|mantenimiento)\b"),
]

_BLOCK_TEXT_RES: list[tuple[str, re.Pattern]] = [
    (sid, re.compile(pat, re.IGNORECASE | re.DOTALL))
    for sid, pat in _BLOCK_TEXT_PATTERNS
]

# Context type detection from URL path/task text
_CONTEXT_PATTERNS: list[tuple[str, re.Pattern, list[str]]] = [
    # (context_type, url_pattern, expected_dom_tags)
    ("financial_chart",  re.compile(r"chart|trade|trading|spot|futures|k-line|candlestick", re.I), ["canvas", "svg"]),
    ("price_ticker",     re.compile(r"price|ticker|market|coin|crypto|btc|eth|usdt|usd", re.I),    []),
    ("package_tracking", re.compile(r"track|shipment|parcel|delivery|package|seguimiento", re.I),  []),
    ("news_article",     re.compile(r"news|article|blog|post|press", re.I),                        ["article", "h1", "h2"]),
    ("search_results",   re.compile(r"search|query|results|buscar", re.I),                         []),
    ("dashboard",        re.compile(r"dashboard|panel|admin|overview|analytics", re.I),            []),
    ("social_feed",      re.compile(r"feed|timeline|tweet|post|social", re.I),                     []),
]

# Ticker symbols and common financial keywords for financial context
_TICKER_RE = re.compile(r"\b([A-Z]{2,6}(?:/[A-Z]{2,6})?(?:USDT?|USD|EUR|BTC|ETH)?)\b")


@dataclass
class ExpectedSignals:
    keywords: list[str] = field(default_factory=list)
    dom_tags: list[str] = field(default_factory=list)
    context_type: str = "generic"


def extract_expected_signals(url: str, task_hint: str = "") -> ExpectedSignals:
    """Derive expected page signals from URL and optional task description."""
    combined = (url + " " + task_hint).lower()
    parsed = urllib.parse.urlparse(url)
    path_and_query = (parsed.path + " " + parsed.query + " " + parsed.fragment).replace("/", " ").replace("-", " ").replace("_", " ")

    sig = ExpectedSignals()

    # Context detection
    for ctx_type, pat, dom_tags in _CONTEXT_PATTERNS:
        if pat.search(combined):
            sig.context_type = ctx_type
            sig.dom_tags.extend(dom_tags)
            break

    # Extract keyword signals from URL tokens + task hint
    tokens = re.findall(r"[A-Za-z0-9]+", path_and_query + " " + task_hint)
    # Keep meaningful tokens (len >= 3, not common URL noise)
    _NOISE = {"www", "com", "net", "org", "http", "https", "html", "php", "asp", "the", "and", "for", "with"}
    keywords = [t for t in tokens if len(t) >= 3 and t.lower() not in _NOISE]
    # Deduplicate, keep max 8 most distinctive (longer = more specific)
    seen: set[str] = set()
    for kw in sorted(keywords, key=len, reverse=True):
        if kw.lower() not in seen:
            seen.add(kw.lower())
            sig.keywords.append(kw.upper())
            if len(sig.keywords) >= 8:
                break

    # Always include domain name as an expected keyword
    domain = parsed.netloc.replace("www.", "")
    if domain:
        brand = domain.split(".")[0].upper()
        if brand not in seen and len(brand) >= 3:
            sig.keywords.append(brand)

    return sig


def _get_page_text(driver) -> str:
    try:
        body = driver.find_element("tag name", "body")
        return body.text or ""
    except Exception:
        return ""


def _check_password_input(driver) -> bool:
    """Return True if a visible password input field is present."""
    try:
        driver.implicitly_wait(0)
        els = driver.find_elements("css selector", "input[type='password']")
        return any(e.is_displayed() for e in els)
    except Exception:
        return False
    finally:
        try:
            driver.implicitly_wait(3)
        except Exception:
            pass


def _check_captcha_element(driver) -> bool:
    """Return True if a captcha iframe/widget is present."""
    try:
        driver.implicitly_wait(0)
        captcha_sel = (
            "iframe[src*='recaptcha'],"
            "iframe[src*='hcaptcha'],"
            "div.g-recaptcha,"
            "div[class*='captcha'],"
            "[data-sitekey]"
        )
        els = driver.find_elements("css selector", captcha_sel)
        return any(e.is_displayed() for e in els)
    except Exception:
        return False
    finally:
        try:
            driver.implicitly_wait(3)
        except Exception:
            pass


def _check_expected_dom(driver, dom_tags: list[str]) -> int:
    """Return count of expected DOM elements found (canvas, svg, article, etc.)."""
    if not dom_tags:
        return 0
    count = 0
    try:
        driver.implicitly_wait(0)
        for tag in dom_tags:
            try:
                els = driver.find_elements("tag name", tag)
                if any(e.is_displayed() for e in els):
                    count += 1
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            driver.implicitly_wait(3)
        except Exception:
            pass
    return count


@dataclass
class ValidationResult:
    valid: bool
    score: int
    blocking_signals: list[str]
    positive_matches: list[str]
    reason: str


def validate_page(driver, url: str = "", task_hint: str = "") -> ValidationResult:
    """
    Full page content validation.

    Score = positive_matches - (2 × negative_matches)
    valid_score > 0  →  VALID
    valid_score <= 0 →  INVALID (blocking UI or wrong content)
    """
    page_text = _get_page_text(driver)
    page_text_sample = page_text[:8000]  # cap for pattern matching

    # ── Negative signals ──────────────────────────────────────────────────────
    blocking: list[str] = []
    for sid, pat in _BLOCK_TEXT_RES:
        if pat.search(page_text_sample):
            blocking.append(sid)

    # DOM-level negative signals
    if _check_password_input(driver):
        blocking.append("password_input_visible")
    if _check_captcha_element(driver):
        blocking.append("captcha_widget_present")

    # ── Positive signals ──────────────────────────────────────────────────────
    expected = extract_expected_signals(url, task_hint)
    positive: list[str] = []

    page_upper = page_text_sample.upper()
    for kw in expected.keywords:
        if kw.upper() in page_upper:
            positive.append(f"keyword:{kw}")

    dom_hits = _check_expected_dom(driver, expected.dom_tags)
    for tag in expected.dom_tags[:dom_hits]:
        positive.append(f"dom:{tag}")

    # Minimum page length is a weak positive signal (non-trivial content loaded)
    if len(page_text) > 500:
        positive.append("content:non_trivial")
    # Strong positive signal: very long body text means the page actually rendered
    # content. News homepages typically have 3000+ chars. Counts as strong evidence
    # the page is NOT a blocking gate, even if footer/header has "iniciar sesión".
    if len(page_text) > 3000:
        positive.append("content:substantial")
    if len(page_text) > 8000:
        positive.append("content:very_long")

    # ── Soft blocking signals — false-positive guard ─────────────────────────
    # "login_wall", "signup_wall", "welcome_gate" are commonly present in headers/
    # footers/menus of news/info homepages. If the page also has substantial body
    # text AND no DOM-level password input or captcha, downgrade these to ignored.
    # Hard blockers (captcha, password input, access_denied, geo_block) are NOT
    # downgraded — those are unambiguous.
    _SOFT_BLOCKERS = {"login_wall", "signup_wall", "welcome_gate", "password_prompt",
                       "privacy_gate", "continue_oauth"}
    _HARD_DOM_BLOCKERS = {"password_input_visible", "captcha_widget_present"}
    has_hard_dom_block = bool(set(blocking) & _HARD_DOM_BLOCKERS)
    # Phase 6: soft-blocker downgrade. The previous gate `len(positive) >= 3`
    # was unreachable for generic screenshot requests on news/portal sites that
    # have a "Sign in" / "Suscríbete" link in the header — every news homepage
    # was permanently flagged invalid even with thousands of chars of real
    # content. New rule: no hard DOM blocker AND substantial body text → drop
    # soft blockers. Hard blockers (captcha, password input) still count.
    if (
        not has_hard_dom_block
        and len(page_text) > 3000
    ):
        # Only soft blockers + substantial content → likely a homepage menu link,
        # not a blocking gate. Drop the soft blockers from the count.
        _filtered_blocking = [b for b in blocking if b not in _SOFT_BLOCKERS]
        if len(_filtered_blocking) < len(blocking):
            logger.info(
                "browser.soft_blocker_downgraded",
                url=(url or "")[:80],
                dropped=[b for b in blocking if b in _SOFT_BLOCKERS],
                kept=_filtered_blocking,
                page_text_len=len(page_text),
            )
            blocking = _filtered_blocking

    # ── Score ─────────────────────────────────────────────────────────────────
    # Negative signals are weighted 2× to err on the side of caution
    score = len(positive) - (2 * len(blocking))

    valid = score > 0

    # Build human-readable reason
    if not valid:
        if blocking:
            reason = f"Blocking UI detected: {', '.join(blocking[:4])}."
        else:
            reason = "Page has insufficient content signals."
    else:
        if blocking:
            reason = f"Content present (score={score}), minor blocking signals: {', '.join(blocking[:2])}."
        else:
            reason = f"Content validated (score={score}, signals={len(positive)})."

    logger.info(
        "browser.page_validated",
        url=(url or "")[:80],
        valid=valid,
        score=score,
        blocking=blocking[:4],
        positive=positive[:4],
    )

    return ValidationResult(
        valid=valid,
        score=score,
        blocking_signals=blocking,
        positive_matches=positive,
        reason=reason,
    )


def validate_page_from_text(
    page_text: str,
    url: str = "",
    task_hint: str = "",
) -> ValidationResult:
    """Driver-less page validation for engines that don't expose a Selenium
    WebDriver (nodriver fallback). Runs the same text-based regex checks as
    ``validate_page`` but skips the DOM probes (password_input_visible,
    captcha_widget_present). Slightly less precise — when the engine offers
    DOM access, prefer ``validate_page``.
    """
    page_text = page_text or ""
    page_text_sample = page_text[:8000]

    blocking: list[str] = []
    for sid, pat in _BLOCK_TEXT_RES:
        if pat.search(page_text_sample):
            blocking.append(sid)

    expected = extract_expected_signals(url, task_hint)
    positive: list[str] = []
    page_upper = page_text_sample.upper()
    for kw in expected.keywords:
        if kw.upper() in page_upper:
            positive.append(f"keyword:{kw}")

    if len(page_text) > 500:
        positive.append("content:non_trivial")
    if len(page_text) > 3000:
        positive.append("content:substantial")
    if len(page_text) > 8000:
        positive.append("content:very_long")

    _SOFT_BLOCKERS = {"login_wall", "signup_wall", "welcome_gate", "password_prompt",
                       "privacy_gate", "continue_oauth"}
    if len(page_text) > 3000:
        _filtered_blocking = [b for b in blocking if b not in _SOFT_BLOCKERS]
        if len(_filtered_blocking) < len(blocking):
            logger.info(
                "browser.soft_blocker_downgraded",
                url=(url or "")[:80],
                dropped=[b for b in blocking if b in _SOFT_BLOCKERS],
                kept=_filtered_blocking,
                page_text_len=len(page_text),
                engine="nodriver",
            )
            blocking = _filtered_blocking

    score = len(positive) - (2 * len(blocking))
    valid = score > 0
    if not valid:
        if blocking:
            reason = f"Blocking UI detected: {', '.join(blocking[:4])}."
        else:
            reason = "Page has insufficient content signals."
    else:
        if blocking:
            reason = f"Content present (score={score}), minor blocking signals: {', '.join(blocking[:2])}."
        else:
            reason = f"Content validated (score={score}, signals={len(positive)})."

    logger.info(
        "browser.page_validated",
        url=(url or "")[:80],
        valid=valid,
        score=score,
        blocking=blocking[:4],
        positive=positive[:4],
        engine="nodriver",
    )
    return ValidationResult(
        valid=valid,
        score=score,
        blocking_signals=blocking,
        positive_matches=positive,
        reason=reason,
    )


# Blocking signals that mean the site actively rejected the request (login wall, captcha, etc.)
# as opposed to just bad/empty content.
_BLOCKED_SOURCE_SIGNALS = frozenset({
    "login_wall", "signup_wall", "welcome_gate", "password_prompt",
    "captcha_text", "continue_oauth", "access_denied", "geo_block",
    "age_gate", "password_input_visible", "captcha_widget_present",
})


def is_blocked_source(vr: ValidationResult) -> bool:
    """Return True if the page was actively blocked (login wall, captcha, geo-block, etc.)
    rather than just having insufficient or wrong content."""
    return bool(set(vr.blocking_signals) & _BLOCKED_SOURCE_SIGNALS)


def format_validation_failure(url: str, vr: ValidationResult, shot_path: str = "") -> str:
    """Format a clear, honest failure message for the agent to return."""
    blocked = is_blocked_source(vr)
    parts = [
        "[BLOCKED_SOURCE]" if blocked else "[CAPTURE_STATUS: FAILED]",
        f"Could not capture the intended content from {url}",
        f"Reason: {vr.reason}",
    ]
    if vr.blocking_signals:
        parts.append(f"Blocking signals detected: {', '.join(vr.blocking_signals)}")
    if shot_path:
        parts.append(f"Diagnostic screenshot saved to {shot_path} (shows the blocking page).")
    if blocked:
        parts.append(
            "The site is blocking automated access. "
            "Search for an alternative public source to complete the task."
        )
    else:
        parts.append(
            "The page is not showing the expected content. "
            "Try a different URL, or the site may require authentication."
        )
    return "\n".join(parts)


def format_multi_result(results: dict[str, str]) -> str:
    """Summarize multi-URL capture results honestly.

    results: {url: "success" | "failed" | "partial"}
    """
    success = [u for u, s in results.items() if s == "success"]
    failed  = [u for u, s in results.items() if s == "failed"]
    partial = [u for u, s in results.items() if s == "partial"]

    total = len(results)
    if len(success) == total:
        return f"[MULTI_CAPTURE: ALL_SUCCESS] All {total} URLs captured successfully."
    if len(failed) == total:
        return f"[MULTI_CAPTURE: ALL_FAILED] All {total} URLs failed to capture."

    lines = [f"[MULTI_CAPTURE: PARTIAL] {len(success)}/{total} succeeded."]
    if success:
        lines.append(f"Success: {', '.join(success)}")
    if partial:
        lines.append(f"Partial: {', '.join(partial)}")
    if failed:
        lines.append(f"Failed: {', '.join(failed)}")
    return "\n".join(lines)
