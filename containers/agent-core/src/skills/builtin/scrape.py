"""Scrape skill — adaptive structured data extraction from any web page.

Uses multi-strategy extraction:
1. Semantic HTML (article, h2>a, etc.)
2. Adaptive DOM analysis — finds repeating container patterns dynamically
3. Headline link extraction (h1-h4 with <a>)
4. Deep link extraction with text scoring

Works on any site regardless of CSS class names.
"""

import re
from collections import Counter
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from ...utils.network_safety import validate_url_for_request
from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

MAX_OUTPUT = 12000
MAX_ARTICLES = 30

# Noise elements to remove
_NOISE_TAGS = {"script", "style", "noscript", "iframe", "svg", "img", "video", "audio", "source", "picture"}
_NOISE_ROLES = {"navigation", "banner", "contentinfo", "complementary"}

# Common date patterns
_DATE_RE = re.compile(
    r"\b(\d{1,2}[\s/.-]\w{3,}[\s/.-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}\s+de\s+\w+(?:\s+de\s+\d{4})?|"
    r"hace\s+\d+\s+\w+|\d+\s+(?:hours?|minutes?|horas?|minutos?)\s+ago)\b",
    re.IGNORECASE,
)

# Navigation / generic link text to skip
_NAV_TEXTS = {
    "home", "inicio", "menu", "menú", "search", "buscar", "más", "more",
    "ver más", "ver todo", "see more", "see all", "siguiente", "anterior",
    "next", "prev", "previous", "login", "register", "suscribirse",
    "iniciar sesión", "cerrar", "close", "share", "compartir",
}


def _clean(text: str) -> str:
    """Collapse whitespace and strip."""
    return re.sub(r"\s+", " ", text).strip()


def _is_article_url(href: str, base_url: str) -> bool:
    """Heuristic: does this URL look like an article (not nav/category)?"""
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    parsed = urlparse(urljoin(base_url, href))
    path = parsed.path.rstrip("/")
    # Articles typically have deeper paths or numeric IDs
    if not path or path == "/":
        return False
    segments = [s for s in path.split("/") if s]
    # At least 2 path segments or contains a number (article ID)
    if len(segments) >= 2:
        return True
    if any(c.isdigit() for c in path):
        return True
    # Long slug (likely article)
    if segments and len(segments[-1]) > 15:
        return True
    return False


def _remove_noise(soup: BeautifulSoup):
    """Remove noise elements in-place."""
    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()
    for el in soup.find_all(attrs={"role": _NOISE_ROLES}):
        el.decompose()
    # Remove elements with common ad/noise classes
    for sel in [".ad", ".ads", ".advertisement", ".sidebar", ".widget",
                ".social-share", ".cookie", ".popup", ".modal", ".banner-ad",
                "#comments", ".comments"]:
        for el in soup.select(sel):
            el.decompose()


def _get_signature(el: Tag) -> str:
    """Get a structural signature for a DOM element (tag + class pattern)."""
    tag = el.name
    classes = el.get("class", [])
    # Normalize classes: keep first 2, sort for consistency
    cls = ".".join(sorted(classes[:2])) if classes else ""
    return f"{tag}.{cls}" if cls else tag


def _extract_article_from_container(el: Tag, base_url: str) -> dict | None:
    """Try to extract an article from a container element."""
    # Find the best link (longest text, looks like article URL)
    best_link = None
    best_title = ""
    best_score = 0

    for a in el.find_all("a", href=True, limit=10):
        href = a["href"]
        if not _is_article_url(href, base_url):
            continue
        text = _clean(a.get_text())
        if not text or len(text) < 8:
            continue
        if text.lower() in _NAV_TEXTS:
            continue
        # Score: longer text = more likely headline
        score = len(text)
        # Bonus if inside a heading
        if a.find_parent(["h1", "h2", "h3", "h4"]):
            score += 100
        if a.parent and a.parent.name in ("h1", "h2", "h3", "h4"):
            score += 100
        if score > best_score:
            best_score = score
            best_link = a
            best_title = text

    if not best_link or not best_title:
        return None

    url = urljoin(base_url, best_link["href"])

    # Find description: first <p> in container that isn't the title
    desc = ""
    for p in el.find_all("p", limit=3):
        p_text = _clean(p.get_text())
        if p_text and p_text != best_title and len(p_text) > 15:
            desc = p_text[:200]
            break

    # Date
    date = ""
    time_el = el.find("time")
    if time_el:
        date = time_el.get("datetime", "") or _clean(time_el.get_text())
    if not date:
        m = _DATE_RE.search(el.get_text())
        if m:
            date = m.group(1)

    return {
        "title": best_title[:200],
        "url": url,
        "description": desc,
        "date": date,
        "category": "",
    }


def _extract_adaptive(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Adaptive extraction: find repeating DOM patterns that contain article links.

    Analyzes the page structure to find containers that:
    1. Repeat multiple times with the same tag+class signature
    2. Each contains at least one article-like link
    3. Have meaningful text content
    """
    articles = []
    seen_urls = set()

    # Find all elements that contain article-like links
    link_parents: list[tuple[Tag, str]] = []
    for a in soup.find_all("a", href=True, limit=200):
        if not _is_article_url(a["href"], base_url):
            continue
        text = _clean(a.get_text())
        if not text or len(text) < 10:
            continue
        # Walk up 1-3 levels to find the container
        for parent in [a.parent, a.parent.parent if a.parent else None,
                       a.parent.parent.parent if a.parent and a.parent.parent else None]:
            if parent and isinstance(parent, Tag) and parent.name not in ("html", "body", "[document]"):
                sig = _get_signature(parent)
                link_parents.append((parent, sig))

    # Count signatures — repeating patterns are likely article containers
    sig_counts = Counter(sig for _, sig in link_parents)
    # Keep signatures that appear 3+ times (likely article list)
    good_sigs = {sig for sig, count in sig_counts.items() if count >= 3}

    if not good_sigs:
        return []

    # Extract articles from containers with good signatures
    seen_elements: set[int] = set()
    for parent, sig in link_parents:
        if sig not in good_sigs:
            continue
        el_id = id(parent)
        if el_id in seen_elements:
            continue
        seen_elements.add(el_id)

        art = _extract_article_from_container(parent, base_url)
        if art and art["url"] not in seen_urls:
            seen_urls.add(art["url"])
            articles.append(art)
            if len(articles) >= MAX_ARTICLES:
                break

    return articles


def _extract_semantic(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Extract using semantic HTML elements (article, role=article, etc.)."""
    articles = []
    seen_urls = set()

    for selector in ["article", "[role='article']", ".article", ".story",
                     ".post", ".entry", ".news-item", ".noticia", ".nota"]:
        for el in soup.select(selector):
            if len(articles) >= MAX_ARTICLES:
                break
            art = _extract_article_from_container(el, base_url)
            if art and art["url"] not in seen_urls:
                seen_urls.add(art["url"])
                articles.append(art)

    return articles


def _extract_headlines(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Extract from heading elements (h1-h4) that contain links."""
    articles = []
    seen_urls = set()

    for tag in ["h2", "h3", "h1", "h4"]:
        for heading in soup.find_all(tag, limit=40):
            if len(articles) >= MAX_ARTICLES:
                break

            link = heading.find("a", href=True)
            if not link:
                parent_a = heading.find_parent("a", href=True)
                if parent_a:
                    link = parent_a
            if not link:
                continue

            title = _clean(heading.get_text())
            href = link["href"]
            if not title or len(title) < 5 or not _is_article_url(href, base_url):
                continue

            url = urljoin(base_url, href)
            if url in seen_urls:
                continue
            seen_urls.add(url)

            desc = ""
            next_sib = heading.find_next_sibling("p")
            if next_sib:
                desc = _clean(next_sib.get_text())[:200]
            if not desc and heading.parent:
                p = heading.parent.find("p")
                if p:
                    desc = _clean(p.get_text())[:200]

            articles.append({
                "title": title[:200], "url": url,
                "description": desc, "date": "", "category": "",
            })

    return articles


def _extract_deep_links(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Deep link extraction: all article-like links with scoring."""
    links = []
    seen = set()

    for a in soup.find_all("a", href=True, limit=150):
        href = a["href"]
        if not _is_article_url(href, base_url):
            continue

        url = urljoin(base_url, href)
        if url in seen:
            continue

        text = _clean(a.get_text())
        if not text or len(text) < 10 or text.lower() in _NAV_TEXTS:
            continue

        seen.add(url)
        links.append({
            "title": text[:200], "url": url,
            "description": "", "date": "", "category": "",
        })

    return links[:MAX_ARTICLES]


def _merge_unique(base: list[dict], additions: list[dict]) -> list[dict]:
    """Merge article lists, avoiding URL duplicates."""
    seen = {a["url"] for a in base if a["url"]}
    for art in additions:
        if art["url"] and art["url"] not in seen:
            base.append(art)
            seen.add(art["url"])
    return base


def _filter_by_keyword(articles: list[dict], keyword: str) -> list[dict]:
    """Filter articles matching keyword(s) in title, description, or URL."""
    if not keyword:
        return articles

    kw = keyword.lower()
    keywords = re.split(r"[,;]|\s+(?:y|and|o|or)\s+", kw)
    keywords = [k.strip() for k in keywords if k.strip()]

    # Also add common variants
    expanded = []
    for k in keywords:
        expanded.append(k)
        # EEUU / EE.UU. / Estados Unidos / USA
        if k in ("eeuu", "ee.uu.", "ee uu"):
            expanded.extend(["estados unidos", "eeuu", "ee.uu.", "usa", "united states", "eeuu"])
        elif k in ("usa", "united states"):
            expanded.extend(["eeuu", "ee.uu.", "estados unidos"])

    return [
        art for art in articles
        if any(k in f"{art['title']} {art['description']} {art['url']}".lower() for k in expanded)
    ]


def _format_articles(articles: list[dict], keyword: str = "", url: str = "") -> str:
    """Format articles into readable output."""
    if not articles:
        if keyword:
            return f"No articles found matching '{keyword}' on {url}."
        return f"No articles found on {url}."

    header = f"Found {len(articles)} articles"
    if keyword:
        header += f" matching '{keyword}'"
    header += f" on {url}:\n"

    lines = [header]
    for i, art in enumerate(articles, 1):
        line = f"{i}. {art['title']}"
        if art["url"]:
            line += f"\n   {art['url']}"
        if art["description"]:
            line += f"\n   {art['description']}"
        if art["date"]:
            line += f" [{art['date']}]"
        if art["category"]:
            line += f" [{art['category']}]"
        lines.append(line)

    output = "\n".join(lines)
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n... (truncated)"
    return output


class ScrapeSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="scrape",
            description=(
                "Extract structured articles, headlines, and links from any web page. "
                "Adaptive: auto-detects page structure regardless of CSS classes. "
                "Returns titles, URLs, descriptions. keyword param filters results. "
                "Use for news, blogs, search results, any site."
            ),
            params=[
                SkillParam(
                    name="url",
                    param_type=ParamType.STRING,
                    description="URL to scrape",
                ),
                SkillParam(
                    name="keyword",
                    param_type=ParamType.STRING,
                    description="Filter by keyword (e.g. 'EEUU', 'bitcoin'). Multiple: 'EEUU,Trump'",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="max_results",
                    param_type=ParamType.INTEGER,
                    description="Max articles to return (default 15)",
                    required=False,
                    default="15",
                ),
                SkillParam(
                    name="selector",
                    param_type=ParamType.STRING,
                    description="CSS selector to scope extraction (optional)",
                    required=False,
                    default="",
                ),
            ],
            category="web",
            timeout_seconds=20.0,
            cooldown_seconds=1.0,
        )

    async def execute(
        self,
        url: str,
        keyword: str = "",
        max_results: str = "15",
        selector: str = "",
        **kwargs,
    ) -> SkillResult:
        url = url.strip()
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        max_n = min(int(max_results), MAX_ARTICLES)

        # SSRF guard on initial URL.
        reason = await validate_url_for_request(url)
        if reason is not None:
            return SkillResult(
                skill_name="scrape", success=False, output="",
                error=f"Blocked: {reason}",
            )
        try:
            # follow_redirects=False — revalidate each redirect target.
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                _current = url
                for _ in range(6):
                    resp = await client.get(
                        _current,
                        headers={
                            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Accept-Language": "es,en;q=0.9",
                        },
                    )
                    if resp.is_redirect and resp.headers.get("location"):
                        _current = str(httpx.URL(_current).join(resp.headers["location"]))
                        reason = await validate_url_for_request(_current)
                        if reason is not None:
                            return SkillResult(
                                skill_name="scrape", success=False, output="",
                                error=f"Blocked redirect: {reason}",
                            )
                        continue
                    break
                resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")

            # Scope to selector if provided (before noise removal)
            if selector:
                target = soup.select_one(selector)
                if target:
                    soup = BeautifulSoup(str(target), "html.parser")

            _remove_noise(soup)

            # Multi-strategy extraction, merge results
            # 1. Semantic HTML (article tags, role=article)
            articles = _extract_semantic(soup, url)

            # 2. Adaptive DOM pattern analysis
            adaptive = _extract_adaptive(soup, url)
            articles = _merge_unique(articles, adaptive)

            # 3. Headline links (h1-h4 > a)
            if len(articles) < 5:
                headlines = _extract_headlines(soup, url)
                articles = _merge_unique(articles, headlines)

            # 4. Deep link extraction (last resort)
            if len(articles) < 3:
                deep = _extract_deep_links(soup, url)
                articles = _merge_unique(articles, deep)

            # Filter by keyword
            all_articles = articles[:]
            if keyword:
                filtered = _filter_by_keyword(articles, keyword)
                if filtered:
                    articles = filtered
                else:
                    # Keyword not found in extracted articles — try deep search
                    # Re-parse without noise removal to catch more links
                    soup2 = BeautifulSoup(resp.text, "html.parser")
                    deep2 = _extract_deep_links(soup2, url)
                    filtered2 = _filter_by_keyword(deep2, keyword)
                    if filtered2:
                        articles = filtered2
                    else:
                        # Return all articles with a note
                        articles = all_articles

            articles = articles[:max_n]
            output = _format_articles(articles, keyword, url)

            # If keyword was specified and no matches found, add helpful note
            if keyword and all_articles and not _filter_by_keyword(all_articles, keyword):
                output += f"\n\nNote: No exact matches for '{keyword}'. Showing all articles from the page."

            return SkillResult(
                skill_name="scrape",
                success=True,
                output=output,
            )

        except httpx.HTTPStatusError as e:
            return SkillResult(
                skill_name="scrape", success=False, output="",
                error=f"HTTP {e.response.status_code}: {url}",
            )
        except Exception as e:
            return SkillResult(
                skill_name="scrape", success=False, output="",
                error=f"Scrape failed: {e}",
            )
