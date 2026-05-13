import re

import httpx
from bs4 import BeautifulSoup

from ...utils.network_safety import validate_url_for_request
from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

import structlog
logger = structlog.get_logger()

MAX_OUTPUT = 12000


class FetchUrlSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="fetch_url",
            description="Fetch a URL and extract readable text content (HTML cleaned). Use for browsing websites, reading articles, checking pages.",
            params=[
                SkillParam(name="url", param_type=ParamType.STRING, description="URL to fetch"),
                SkillParam(
                    name="max_chars",
                    param_type=ParamType.INTEGER,
                    description="Max characters to return (default 6000)",
                    required=False,
                    default="6000",
                ),
                SkillParam(
                    name="selector",
                    param_type=ParamType.STRING,
                    description="CSS selector to extract specific element (e.g. 'article', '.content', '#main')",
                    required=False,
                    default="",
                ),
            ],
            category="web",
            timeout_seconds=20.0,
            cooldown_seconds=1.0,
        )

    async def execute(self, url: str, max_chars: str = "6000", selector: str = "", **kwargs) -> SkillResult:
        # Normalize URL
        url = url.strip()
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url

        # SSRF protection — block private/internal/metadata targets, including
        # hostnames that DNS-resolve to those (anti-rebinding).
        _ssrf_reason = await validate_url_for_request(url)
        if _ssrf_reason is not None:
            logger.warning("fetch_url.ssrf_blocked", url=url[:120], reason=_ssrf_reason)
            return SkillResult(skill_name="fetch_url", success=False, output="",
                               error=f"Blocked: {_ssrf_reason}")

        max_c = min(int(max_chars), MAX_OUTPUT)
        try:
            # follow_redirects=False — revalidate each Location target.
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
                        _r = await validate_url_for_request(_current)
                        if _r is not None:
                            logger.warning("fetch_url.ssrf_redirect_blocked", url=_current[:120], reason=_r)
                            return SkillResult(skill_name="fetch_url", success=False, output="",
                                               error=f"Blocked redirect: {_r}")
                        continue
                    break
                resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")

            # Non-HTML: return raw text (JSON, plain text, etc.)
            if "html" not in content_type and "xml" not in content_type:
                text = resp.text[:max_c]
                return SkillResult(
                    skill_name="fetch_url",
                    success=True,
                    output=f"Content from {url} ({content_type}):\n---\n{text}",
                )

            soup = BeautifulSoup(resp.text, "html.parser")

            # Extract title
            title = soup.title.string.strip() if soup.title and soup.title.string else ""

            # If CSS selector provided, extract that element
            if selector:
                target = soup.select_one(selector)
                if target:
                    soup = BeautifulSoup(str(target), "html.parser")

            # Remove noise elements
            for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"]):
                tag.decompose()

            # Extract links for context
            links = []
            for a in soup.find_all("a", href=True, limit=20):
                href = a["href"]
                link_text = a.get_text(strip=True)[:60]
                if link_text and href.startswith(("http", "/")):
                    links.append(f"  [{link_text}]({href})")

            text = soup.get_text(separator="\n", strip=True)
            # Collapse multiple blank lines
            text = re.sub(r"\n{3,}", "\n\n", text)
            lines = [line for line in text.splitlines() if line.strip()]
            text = "\n".join(lines)[:max_c]

            # If very little text extracted, site likely requires JavaScript
            if len(text.strip()) < 50:
                return SkillResult(
                    skill_name="fetch_url",
                    success=False,
                    output="",
                    error=f"Page returned almost no text content — likely requires JavaScript. Use browser(action=\"navigate\", url=\"{url}\") instead.",
                )

            output = f"Content from {url}:\n"
            if title:
                output += f"Title: {title}\n"
            output += f"---\n{text}"
            if links:
                output += f"\n---\nLinks found:\n" + "\n".join(links[:15])

            return SkillResult(
                skill_name="fetch_url",
                success=True,
                output=output,
            )
        except httpx.HTTPStatusError as e:
            return SkillResult(skill_name="fetch_url", success=False, output="", error=f"HTTP {e.response.status_code}: {url}. Try using browser(action=\"navigate\", url=\"{url}\") instead.")
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            return SkillResult(skill_name="fetch_url", success=False, output="", error=f"Timeout/connection error: {url}. This site may need JavaScript. Use browser(action=\"navigate\", url=\"{url}\") instead.")
        except Exception as e:
            return SkillResult(skill_name="fetch_url", success=False, output="", error=f"{e}. Try browser(action=\"navigate\", url=\"{url}\") as fallback.")
