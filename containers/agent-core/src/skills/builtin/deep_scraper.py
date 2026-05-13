"""DeepScraper Skill — Playwright-based deep web scraping via containerized Crawlee.

Specialises in two scenarios:
  - YouTube transcripts: intercepts timedtext API to extract full captions
  - JS-heavy pages: full networkidle rendering via Playwright/Chromium

Executes the pre-built `clawd-crawlee` Docker image and parses its JSON stdout.
The Docker socket must be mounted in the agent container (standard config).

Output (plaintext):
  status: SUCCESS | PARTIAL | ERROR
  type:   TRANSCRIPT | DESCRIPTION | GENERIC
  [video_id: <id>]          -- YouTube only
  content:
  <extracted text>
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from urllib.parse import urlparse

import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()

_SKILL_NAME = "deep_scraper"
_DOCKER_IMAGE = "clawd-crawlee"
_TIMEOUT_S = 90  # generous: YouTube ~30s, complex pages up to 60s


def _is_safe_url(url: str) -> bool:
    """Return True if the URL resolves to a public (non-internal) IP address.

    Blocks loopback, private (RFC 1918), link-local, and reserved ranges.
    Fails closed: if DNS resolution fails for any reason, returns False.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname  # strips port, brackets for IPv6
        if not hostname:
            return False

        # getaddrinfo returns all A/AAAA records — check every resolved IP
        results = socket.getaddrinfo(hostname, None)
        if not results:
            return False

        for _family, _type, _proto, _canonname, sockaddr in results:
            raw_ip = sockaddr[0]  # first element is always the IP string
            try:
                ip = ipaddress.ip_address(raw_ip)
            except ValueError:
                return False  # unparseable — fail closed

            if (
                ip.is_loopback
                or ip.is_private
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_unspecified
                or ip.is_multicast
            ):
                return False

        return True

    except Exception:
        return False  # DNS failure, parse error, etc. — fail closed


class DeepScraperSkill(SkillBase):
    """Playwright-powered deep scraper for YouTube transcripts and JS-heavy pages."""

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name=_SKILL_NAME,
            description=(
                "Deep web scraper using a containerized Playwright/Chromium browser. "
                "Use for: (1) YouTube URLs — extracts the full video transcript via "
                "network interception; (2) JS-heavy or anti-bot pages that fetch_url "
                "and browser cannot render properly. "
                "Returns the full extracted text (up to 15 000 chars for transcripts, "
                "10 000 for generic pages). "
                "WHEN TO USE: user shares a YouTube link and wants transcript/summary; "
                "page content is dynamic and simpler skills return empty/blocked results."
            ),
            params=[
                SkillParam(
                    name="url",
                    param_type=ParamType.STRING,
                    description="URL to scrape (YouTube or any JS-rendered page)",
                ),
            ],
            category="web",
            timeout_seconds=100.0,
            capability_level="monitored",
        )

    async def execute(self, url: str = "", **kwargs) -> SkillResult:
        url = url.strip()
        if not url:
            return SkillResult(
                skill_name=_SKILL_NAME,
                success=False,
                output="",
                error="url is required",
            )

        # Basic scheme guard — block non-http(s)
        if not url.startswith(("http://", "https://")):
            return SkillResult(
                skill_name=_SKILL_NAME,
                success=False,
                output="",
                error="Only http/https URLs are supported",
            )

        # SSRF guard — resolve hostname, block non-public IPs (fail closed)
        safe = await asyncio.to_thread(_is_safe_url, url)
        if not safe:
            logger.warning("deep_scraper.ssrf_blocked", url=url[:80])
            return SkillResult(
                skill_name=_SKILL_NAME,
                success=False,
                output="",
                error="This URL cannot be accessed due to network safety restrictions.",
            )

        logger.info("deep_scraper.start", url=url[:80])

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "run", "--rm", "--shm-size=1gb", _DOCKER_IMAGE, url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return SkillResult(
                    skill_name=_SKILL_NAME,
                    success=False,
                    output="",
                    error=f"deep_scraper timed out after {_TIMEOUT_S}s",
                )
        except FileNotFoundError:
            return SkillResult(
                skill_name=_SKILL_NAME,
                success=False,
                output="",
                error="docker not found — cannot run deep_scraper",
            )
        except Exception as exc:
            logger.exception("deep_scraper.launch_error", url=url[:80])
            return SkillResult(
                skill_name=_SKILL_NAME,
                success=False,
                output="",
                error=str(exc),
            )

        raw = stdout.decode("utf-8", errors="replace").strip()
        if not raw:
            err_msg = stderr.decode("utf-8", errors="replace").strip()[:200]
            return SkillResult(
                skill_name=_SKILL_NAME,
                success=False,
                output="",
                error=f"no output from container: {err_msg}",
            )

        # Parse JSON output from container
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Container printed something non-JSON — return raw text as-is
            logger.warning("deep_scraper.non_json_output", url=url[:80])
            return SkillResult(
                skill_name=_SKILL_NAME,
                success=True,
                output=raw[:10000],
            )

        status = data.get("status", "UNKNOWN")
        content_type = data.get("type", "GENERIC")
        content = data.get("data", "")
        video_id = data.get("videoId", "")

        if status == "ERROR" or not content:
            return SkillResult(
                skill_name=_SKILL_NAME,
                success=False,
                output="",
                error=f"scraper returned {status}: {content[:200]}",
            )

        lines = [
            f"status: {status}",
            f"type: {content_type}",
        ]
        if video_id:
            lines.append(f"video_id: {video_id}")
        lines.append("content:")
        lines.append(content)

        output = "\n".join(lines)
        logger.info(
            "deep_scraper.done",
            url=url[:80],
            status=status,
            content_type=content_type,
            chars=len(content),
        )

        return SkillResult(
            skill_name=_SKILL_NAME,
            success=True,
            output=output,
        )
