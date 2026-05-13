"""Browser Deep Scrape skill.

Extracts structured information from a webpage using JavaScript DOM manipulation:
  - Main title (og:title → h1 → document.title, priority order)
  - Content (article/main/body after removing noise elements)
  - Sections (h2, h3 headings with surrounding text)
  - Links (anchor href + text, filtered to meaningful links)

Noise removal (JavaScript-based, no external deps):
  - Removes: nav, header, footer, aside, [role=navigation], ad containers,
    cookie banners, sidebars, share widgets, social buttons, related articles
  - Cleans: scripts, styles, invisible elements, duplicate whitespace

Separation of responsibilities:
  - This skill handles ONLY content extraction. It never takes screenshots.
  - It reuses low-level browser.py infrastructure (sessions, driver management).
  - It does NOT call any other skill internally.

Output format (JSON-compatible plaintext):
  title: <page title>
  url: <page url>
  content: <main text, up to 8000 chars>
  sections:
    - <section heading>: <preview>
    ...
  links:
    - <text> | <href>
    ...
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

# Import shared browser infrastructure (low-level helpers only, NOT BrowserSkill)
from .browser import (
    _dismiss_overlays,
    _get_driver,
    _normalize_url,
    _wait_for_page,
)

logger = structlog.get_logger()

MAX_CONTENT_CHARS = 8000
MAX_SECTIONS = 20
MAX_LINKS = 50

# JavaScript that removes noise elements from the DOM before extraction
_CLEAN_DOM_JS = """
(function() {
    // ── Noise selectors to remove ──────────────────────────────────────────
    var NOISE_SELECTORS = [
        // Navigation
        'nav', 'header nav', '.nav', '.navbar', '.navigation', '[role="navigation"]',
        '[role="menubar"]', '#nav', '#navbar', '#navigation', '#main-nav', '#site-nav',
        // Header/Footer
        'footer', '.footer', '#footer', 'header.site-header', '#header.site-header',
        '.site-footer', '.page-footer',
        // Sidebar/Aside
        'aside', '.sidebar', '.widget-area', '.related-posts', '.related-articles',
        '#sidebar', '[role="complementary"]',
        // Ads
        '[class*="ad-"]', '[class*="-ad"]', '[class*="ads-"]', '[id*="ad-"]',
        '[id*="-ad"]', '[id*="ads-"]', '[class*="advertisement"]', '[class*="sponsor"]',
        '.dfp-ad', '.google-ad', '#google-ads', '.adsbygoogle',
        // Cookie banners
        '[class*="cookie"]', '[id*="cookie"]', '[class*="gdpr"]', '[id*="gdpr"]',
        '[class*="consent"]', '#onetrust-banner-sdk', '#cookiebanner',
        // Share/Social widgets
        '[class*="share"]', '[class*="social"]', '[class*="follow"]',
        '.sharethis', '.addthis', '.shareaholic',
        // Comments
        '#comments', '.comments', '.comment-section', '#disqus_thread',
        // Newsletter/Subscribe
        '[class*="newsletter"]', '[class*="subscribe"]', '[class*="signup"]',
        // Paywall / subscription notices
        '[class*="paywall"]', '[class*="subscription"]', '[class*="premium"]',
        // Breadcrumbs
        '.breadcrumb', '.breadcrumbs', '[aria-label="breadcrumb"]',
        // Misc overlays
        '[class*="popup"]', '[class*="modal"][class*="overlay"]',
        '[class*="sticky-bar"]', '.back-to-top', '.scroll-to-top',
    ];

    NOISE_SELECTORS.forEach(function(sel) {
        try {
            document.querySelectorAll(sel).forEach(function(el) {
                el.remove();
            });
        } catch(e) {}
    });

    // Remove scripts, styles, noscript, iframe, svg, canvas, video
    ['script', 'style', 'noscript', 'iframe', 'svg', 'canvas', 'video', 'audio'].forEach(function(tag) {
        document.querySelectorAll(tag).forEach(function(el) { el.remove(); });
    });

    // Remove hidden elements
    document.querySelectorAll('*').forEach(function(el) {
        try {
            var s = window.getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') {
                el.remove();
            }
        } catch(e) {}
    });
})();
"""

# JavaScript that extracts structured data from the cleaned DOM
_EXTRACT_DATA_JS = """
(function() {
    var data = {
        title: '',
        url: window.location.href,
        content: '',
        sections: [],
        links: []
    };

    // ── Title (priority: og:title > h1 > document.title) ─────────────────────
    var ogTitle = document.querySelector('meta[property="og:title"]');
    if (ogTitle && ogTitle.content) {
        data.title = ogTitle.content.trim();
    }
    if (!data.title) {
        var h1 = document.querySelector('h1');
        if (h1) data.title = h1.innerText.trim().slice(0, 200);
    }
    if (!data.title) {
        data.title = document.title.trim().slice(0, 200);
    }

    // ── Main content container ────────────────────────────────────────────────
    // Try semantic containers in priority order
    var contentEl = (
        document.querySelector('article') ||
        document.querySelector('main') ||
        document.querySelector('[role="main"]') ||
        document.querySelector('.article-body') ||
        document.querySelector('.post-content') ||
        document.querySelector('.entry-content') ||
        document.querySelector('.story-body') ||
        document.querySelector('#content') ||
        document.querySelector('.content') ||
        document.body
    );

    // ── Sections (h2, h3 with surrounding text) ───────────────────────────────
    var headings = contentEl.querySelectorAll('h2, h3');
    headings.forEach(function(h, idx) {
        if (idx >= 20) return;
        var text = h.innerText.trim();
        if (!text || text.length < 3) return;

        // Grab the text of up to 2 sibling paragraphs after the heading
        var preview = '';
        var sib = h.nextElementSibling;
        var count = 0;
        while (sib && count < 2) {
            if (['P', 'DIV', 'SPAN', 'LI'].indexOf(sib.tagName) !== -1) {
                var t = sib.innerText.trim();
                if (t.length > 20) {
                    preview += (preview ? ' ' : '') + t.slice(0, 200);
                    count++;
                }
            }
            sib = sib.nextElementSibling;
        }

        data.sections.push({
            heading: text.slice(0, 120),
            preview: preview.slice(0, 300)
        });
    });

    // ── Full content text ─────────────────────────────────────────────────────
    // Use innerText for clean rendering (respects display:none we removed)
    var rawText = contentEl.innerText || '';
    // Collapse multiple blank lines
    rawText = rawText.replace(/\\n{3,}/g, '\\n\\n').trim();
    data.content = rawText.slice(0, 8000);

    // ── Links (anchors with meaningful text and href) ─────────────────────────
    var anchors = contentEl.querySelectorAll('a[href]');
    var seen = new Set();
    anchors.forEach(function(a) {
        if (data.links.length >= 50) return;
        var href = a.href;
        var text = a.innerText.trim();
        // Skip empty, anchor-only, javascript: and mailto: links
        if (!href || !text || text.length < 2) return;
        if (href.startsWith('javascript:') || href.startsWith('mailto:')) return;
        if (href.startsWith('#')) return;
        // Deduplicate by href
        var norm = href.split('?')[0].split('#')[0];
        if (seen.has(norm)) return;
        seen.add(norm);
        data.links.push({ text: text.slice(0, 100), href: href.slice(0, 300) });
    });

    return JSON.stringify(data);
})();
"""


def _do_deep_scrape(url: str, session: str, chat_id: str, user_id: str) -> str:
    """Blocking implementation (runs in thread via asyncio.to_thread)."""
    url = _normalize_url(url)
    if not url:
        return "error: url is required"

    session = session or "scrape1"

    # ── Navigation ────────────────────────────────────────────────────────────
    try:
        driver = _get_driver(session)
        driver.get(url)
    except Exception as e:
        return f"error: navigation failed — {e}"

    _wait_for_page(driver)
    time.sleep(0.8)

    # ── Overlay dismissal ─────────────────────────────────────────────────────
    _dismiss_overlays(session)
    time.sleep(0.5)
    _dismiss_overlays(session)  # Second pass
    time.sleep(0.3)

    try:
        page_url = driver.current_url
    except Exception:
        page_url = url

    if page_url in ("data:,", "about:blank", ""):
        return f"error: browser blocked by anti-bot at {url}"

    # ── DOM cleaning ──────────────────────────────────────────────────────────
    try:
        driver.execute_script(_CLEAN_DOM_JS)
        time.sleep(0.2)
    except Exception as e:
        logger.warning("browser_deep_scrape.clean_failed", error=str(e)[:80])

    # ── Structured extraction ─────────────────────────────────────────────────
    try:
        raw = driver.execute_script(_EXTRACT_DATA_JS)
        data = json.loads(raw) if raw else {}
    except Exception as e:
        logger.warning("browser_deep_scrape.extract_failed", error=str(e)[:80])
        # Fallback: plain text extraction
        try:
            body = driver.find_element("tag name", "body")
            data = {
                "title": driver.title or "",
                "url": page_url,
                "content": (body.text or "")[:MAX_CONTENT_CHARS],
                "sections": [],
                "links": [],
            }
        except Exception as e2:
            return f"error: extraction failed — {e2}"

    if not data:
        return "error: no data extracted"

    # ── Format output ─────────────────────────────────────────────────────────
    lines = [
        f"title: {data.get('title', '')}",
        f"url: {data.get('url', page_url)}",
        f"content: {data.get('content', '')[:MAX_CONTENT_CHARS]}",
    ]

    sections = data.get("sections", [])[:MAX_SECTIONS]
    if sections:
        lines.append("sections:")
        for sec in sections:
            heading = sec.get("heading", "")
            preview = sec.get("preview", "")
            if preview:
                lines.append(f"  - {heading}: {preview}")
            else:
                lines.append(f"  - {heading}")

    links = data.get("links", [])[:MAX_LINKS]
    if links:
        lines.append("links:")
        for lnk in links:
            lines.append(f"  - {lnk.get('text', '')} | {lnk.get('href', '')}")

    logger.info(
        "browser_deep_scrape.complete",
        url=page_url,
        title=data.get("title", "")[:60],
        sections=len(sections),
        links=len(links),
        content_chars=len(data.get("content", "")),
    )
    return "\n".join(lines)


class BrowserDeepScrapeSkill(SkillBase):
    """Extract structured content (title, sections, links) from a webpage."""

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="browser_deep_scrape",
            description=(
                "Extract structured information from a webpage: title, main content, "
                "sections (headings with context), and links. "
                "Automatically removes nav bars, ads, cookie banners, and footer noise. "
                "Use when the user asks to extract/scrape/analyze the content of a page "
                "(e.g. 'extract info from', 'analyze content of', 'scrape', 'extract the article from'). "
                "Returns: title, content text, sections list, links list."
            ),
            params=[
                SkillParam(
                    name="url",
                    param_type=ParamType.STRING,
                    description="URL of the page to scrape",
                ),
                SkillParam(
                    name="session",
                    param_type=ParamType.STRING,
                    required=False,
                    default="scrape1",
                    description="Browser session name (persists cookies). Default: 'scrape1'",
                ),
            ],
            category="web",
            timeout_seconds=60.0,
            capability_level="monitored",
        )

    async def execute(
        self,
        url: str = "",
        session: str = "scrape1",
        **kwargs,
    ) -> SkillResult:
        chat_id = kwargs.get("chat_id", "")
        user_id = kwargs.get("user_id", "")

        if not url:
            return SkillResult(
                skill_name="browser_deep_scrape",
                success=False,
                output="",
                error="url is required",
            )

        try:
            result = await asyncio.to_thread(
                _do_deep_scrape,
                url, session, chat_id, user_id,
            )
            success = not result.startswith("error:")
            return SkillResult(
                skill_name="browser_deep_scrape",
                success=success,
                output=result if success else "",
                error=result if not success else "",
            )
        except Exception as e:
            logger.exception("browser_deep_scrape.error", url=url)
            return SkillResult(
                skill_name="browser_deep_scrape",
                success=False,
                output="",
                error=str(e),
            )
