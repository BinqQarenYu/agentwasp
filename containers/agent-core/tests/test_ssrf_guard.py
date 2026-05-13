"""Unit tests for the centralized SSRF guard at src/utils/network_safety.py."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.network_safety import (
    BLOCKED_HOSTNAMES,
    BLOCKED_LITERAL_IPS,
    is_ssrf_target_sync,
    validate_url_for_request,
)


@pytest.mark.asyncio
async def test_blocks_aws_metadata_literal():
    r = await validate_url_for_request("http://169.254.169.254/latest/meta-data/")
    assert r is not None and ("metadata" in r.lower() or "private" in r.lower() or "169.254" in r)


@pytest.mark.asyncio
async def test_blocks_loopback_ipv4():
    r = await validate_url_for_request("http://127.0.0.1:8080/admin")
    assert r is not None


@pytest.mark.asyncio
async def test_blocks_loopback_ipv6():
    r = await validate_url_for_request("http://[::1]/")
    assert r is not None


@pytest.mark.asyncio
async def test_blocks_rfc1918_ipv4_10():
    r = await validate_url_for_request("http://10.0.0.5/admin")
    assert r is not None


@pytest.mark.asyncio
async def test_blocks_rfc1918_ipv4_192_168():
    r = await validate_url_for_request("http://192.168.1.1/")
    assert r is not None


@pytest.mark.asyncio
async def test_blocks_rfc1918_ipv4_172_16():
    r = await validate_url_for_request("http://172.16.0.1/")
    assert r is not None


@pytest.mark.asyncio
async def test_blocks_metadata_hostname():
    r = await validate_url_for_request("http://metadata.google.internal/")
    assert r is not None and "metadata.google.internal" in r


@pytest.mark.asyncio
async def test_blocks_localhost_hostname():
    r = await validate_url_for_request("http://localhost/")
    assert r is not None


@pytest.mark.asyncio
async def test_blocks_link_local():
    r = await validate_url_for_request("http://169.254.5.5/")
    assert r is not None


@pytest.mark.asyncio
async def test_blocks_unspecified():
    r = await validate_url_for_request("http://0.0.0.0/")
    assert r is not None


@pytest.mark.asyncio
async def test_blocks_non_http_scheme():
    r = await validate_url_for_request("file:///etc/passwd")
    assert r is not None and "scheme" in r.lower()


@pytest.mark.asyncio
async def test_blocks_missing_host():
    r = await validate_url_for_request("http:///nohost")
    assert r is not None


@pytest.mark.asyncio
async def test_blocks_alibaba_metadata():
    r = await validate_url_for_request("http://100.100.100.200/latest/meta-data/")
    assert r is not None


@pytest.mark.asyncio
async def test_blocks_docker_internal():
    r = await validate_url_for_request("http://host.docker.internal/")
    assert r is not None


@pytest.mark.network
@pytest.mark.asyncio
async def test_allows_real_public_domain():
    """google.com resolves to a public IP; guard should allow.

    Requires network. Skipped in CI environments or when DNS is unreachable
    so the test suite stays green offline.
    """
    if os.environ.get("CI") or os.environ.get("NO_NETWORK"):
        pytest.skip("Network tests disabled (CI / NO_NETWORK env set)")

    # Pre-flight DNS check — if we can't resolve google.com, skip rather than
    # fail because the network is the limitation, not the guard.
    import socket as _socket
    try:
        _socket.getaddrinfo("google.com", None)
    except Exception:
        pytest.skip("DNS unavailable; cannot exercise public-domain allowlist path")

    r = await validate_url_for_request("https://google.com/")
    assert r is None, f"Expected None (allow), got block reason: {r}"


@pytest.mark.asyncio
async def test_sync_helper_blocks_loopback():
    assert is_ssrf_target_sync("http://127.0.0.1/") is True


@pytest.mark.asyncio
async def test_sync_helper_blocks_metadata_name():
    assert is_ssrf_target_sync("http://metadata.google.internal/") is True


@pytest.mark.asyncio
async def test_sync_helper_allows_public_ip_literal():
    # 8.8.8.8 is public; sync helper doesn't do DNS but should pass IP literals
    assert is_ssrf_target_sync("http://8.8.8.8/") is False


# Sanity: the guard's allowlist/blocklist constants are non-empty.
def test_blocklists_non_empty():
    assert len(BLOCKED_HOSTNAMES) > 0
    assert len(BLOCKED_LITERAL_IPS) > 0
    assert "169.254.169.254" in BLOCKED_LITERAL_IPS
    assert "metadata.google.internal" in BLOCKED_HOSTNAMES
