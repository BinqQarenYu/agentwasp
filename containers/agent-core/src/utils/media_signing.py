"""Signed media URL helpers for /chat/media endpoint.

Format: /chat/media/<filename>?exp=<unix_ts>&sig=<hmac_sha256>

The signature covers "path|exp" so modifying either the path or the
expiry timestamp invalidates the signature.
"""
from __future__ import annotations

import hashlib
import hmac
import time


def sign_media_url(path: str, expires: int, secret: str) -> str:
    """Return HMAC-SHA256 hex digest of 'path|expires'."""
    payload = f"{path}|{expires}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def generate_signed_media_url(path: str, secret: str, ttl_seconds: int = 300) -> str:
    """Return a signed, time-limited URL for the given media path.

    Example:
        /chat/media/foo.png → /chat/media/foo.png?exp=1712345678&sig=abc...
    """
    expires = int(time.time()) + ttl_seconds
    sig = sign_media_url(path, expires, secret)
    return f"{path}?exp={expires}&sig={sig}"


def verify_media_url(path: str, exp_str: str | None, sig: str | None, secret: str) -> tuple[bool, str]:
    """Validate a signed media URL.

    Returns (is_valid: bool, reason: str).
    """
    if not exp_str or not sig:
        return False, "missing exp or sig"

    try:
        exp = int(exp_str)
    except (ValueError, TypeError):
        return False, "invalid exp"

    if exp < int(time.time()):
        return False, "expired"

    expected = sign_media_url(path, exp, secret)
    if not hmac.compare_digest(sig, expected):
        return False, "invalid signature"

    return True, "ok"
