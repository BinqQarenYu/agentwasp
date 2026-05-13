"""Centralized SSRF guard helpers.

Use these from EVERY outbound HTTP call site (httpx, urllib, browser, etc.).
The guard resolves DNS, validates ALL returned A/AAAA records, blocks cloud
metadata hosts by name, and provides a manual redirect-following client that
re-validates every Location: header (so an attacker-controlled redirect to a
private IP cannot bypass the initial check).

Usage:

    from ..utils.network_safety import validate_url_for_request, safe_get

    reason = await validate_url_for_request(url)
    if reason:
        return SkillResult(success=False, error=f"Blocked: {reason}")

    # OR for the common GET case:
    async with safe_get(url, timeout=15) as resp:
        ...
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import urllib.parse
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx

# Cloud metadata + intentionally internal hosts. Block by name, NOT just IP,
# so a hostname like `metadata.google.internal` is rejected even if DNS
# resolution returns something deceptively public.
BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.gcp.internal",
    "computemetadata.internal",
    "metadata",                    # short alias used inside some clouds
    "instance-data",               # AWS legacy
    "instance-data.ec2.internal",
    "kubernetes.default.svc",
    "kubernetes.default",
    "host.docker.internal",
    "gateway.docker.internal",
    # Localhost aliases (covered by IP check too, but defense-in-depth)
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
})

# Hard-coded IPs that should always be blocked, regardless of how DNS resolved.
BLOCKED_LITERAL_IPS = frozenset({
    "169.254.169.254",        # AWS/Azure IMDS
    "fd00:ec2::254",          # AWS IMDSv6
    "100.100.100.200",        # Alibaba metadata
})


def _is_private_or_special_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Catch every CIDR a public service has no business reaching."""
    if str(addr) in BLOCKED_LITERAL_IPS:
        return True
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _classify_host_literal(host: str) -> str | None:
    """If host is an IP literal, return a block reason or None."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None
    if _is_private_or_special_ip(addr):
        return f"private/loopback/metadata IP literal ({host})"
    return None


async def _resolve_all(host: str) -> list[str]:
    """Resolve a hostname to ALL A/AAAA records. Empty list on failure."""
    loop = asyncio.get_event_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except Exception:
        return []
    ips: set[str] = set()
    for family, _, _, _, sockaddr in infos:
        if family in (socket.AF_INET, socket.AF_INET6) and sockaddr:
            ips.add(sockaddr[0])
    return sorted(ips)


async def validate_url_for_request(url: str) -> str | None:
    """Return a block reason string, or None if the URL is safe to fetch.

    Validates:
      - URL parses and uses http/https
      - hostname not in BLOCKED_HOSTNAMES
      - if hostname is an IP literal, it must be public
      - if hostname is a name, EVERY resolved A/AAAA record must be public
        (this is the DNS-rebinding protection)
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return "malformed URL"

    if parsed.scheme not in ("http", "https"):
        return f"scheme {parsed.scheme!r} not allowed (only http/https)"

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return "missing hostname"

    if host in BLOCKED_HOSTNAMES:
        return f"blocked hostname ({host})"

    # IP literal? validate directly.
    literal_reason = _classify_host_literal(host)
    if literal_reason is not None:
        return literal_reason

    # Hostname → resolve and check every IP.
    if not host.replace(".", "").replace(":", "").isdigit():  # not a bare IPv4 numeric
        ips = await _resolve_all(host)
        if not ips:
            # DNS failure — let httpx surface the error rather than block prematurely.
            return None
        for ip in ips:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if _is_private_or_special_ip(addr):
                return f"hostname {host!r} resolves to private/loopback/metadata address ({ip})"

    return None


def is_ssrf_target_sync(url: str) -> bool:
    """Synchronous best-effort check (no DNS). Use only when you can't await.

    NOTE: This does NOT protect against DNS rebinding. For full safety prefer
    the async ``validate_url_for_request``.
    """
    try:
        host = (urllib.parse.urlparse(url).hostname or "").strip().lower()
    except Exception:
        return True
    if not host:
        return True
    if host in BLOCKED_HOSTNAMES:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return _is_private_or_special_ip(addr)


@asynccontextmanager
async def safe_get(
    url: str,
    *,
    timeout: float = 15.0,
    headers: dict | None = None,
    max_redirects: int = 5,
    user_agent: str = "WASP-Agent/1.0",
) -> AsyncIterator[httpx.Response]:
    """GET with SSRF guard on the URL AND every redirect target.

    Yields the final ``httpx.Response``. Raises ``PermissionError`` if any
    URL in the chain is blocked, ``httpx.HTTPError`` on transport failure.
    Caller is responsible for resp.raise_for_status() if it cares about 4xx/5xx.
    """
    final_headers = {"User-Agent": user_agent}
    if headers:
        final_headers.update(headers)

    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for hop in range(max_redirects + 1):
            reason = await validate_url_for_request(current)
            if reason is not None:
                raise PermissionError(f"SSRF guard blocked URL: {reason}")
            resp = await client.get(current, headers=final_headers)
            if resp.is_redirect and resp.headers.get("location"):
                if hop >= max_redirects:
                    raise httpx.TooManyRedirects(
                        f"Exceeded {max_redirects} redirects",
                        request=resp.request,
                    )
                # Resolve Location relative to current URL
                current = str(httpx.URL(current).join(resp.headers["location"]))
                continue
            yield resp
            return
