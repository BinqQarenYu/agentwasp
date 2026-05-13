"""Cloudflare challenge detector — engine-agnostic.

Both nodriver and Selenium handlers call ``is_cloudflare_challenge()`` after
a page load. When the detector fires, the caller returns
``[CLOUDFLARE_BLOCKED: <domain>]`` immediately. Upstream routing treats that
marker as terminal — no fallback to the other engine, no retries, no further
LLM rounds on the same URL.

Why a single shared module
--------------------------
We want identical semantics across engines so the LLM never sees a Cloudflare
page on Selenium that nodriver would have flagged (or vice versa). Keeping the
detector pure-text-input also makes it trivially testable.
"""
from __future__ import annotations

from urllib.parse import urlparse

# Title patterns Cloudflare uses for the JS challenge / managed challenge.
# Match is prefix-based on lowercased title to absorb minor wording drift.
_TITLE_PREFIXES = (
    "just a moment",
    "attention required! | cloudflare",
    "access denied | ",
    "please wait...",
    "ddos-guard",
    "checking your browser",
)

# Body-text fingerprints. These appear in the rendered HTML/innerText of every
# CF challenge variant we have observed in production.
_BODY_MARKERS = (
    "/cdn-cgi/challenge-platform/",
    "_cf_chl_opt",
    "cf-challenge",
    "cf-mitigated",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "performance & security by cloudflare",
    "ray id:",
    "needs to review the security of your connection",
    "verify you are human by completing the action below",
)


def is_cloudflare_challenge(*, title: str = "", body_text: str = "", url: str = "") -> bool:
    """Return True if the page looks like a Cloudflare challenge / WAF block.

    All three inputs are optional; pass whatever the engine has cheap access
    to. The check is OR-of-signals so a single match is enough.
    """
    t = (title or "").strip().lower()
    if t:
        for prefix in _TITLE_PREFIXES:
            if t.startswith(prefix):
                return True

    b = (body_text or "")[:6000].lower()
    if b:
        # Need at least one strong marker — avoid false positives on pages
        # that happen to mention Cloudflare in passing (e.g. blog posts).
        for marker in _BODY_MARKERS:
            if marker in b:
                # "ray id:" alone is too weak; require it alongside a CF
                # branding token to count.
                if marker == "ray id:":
                    if "cloudflare" in b:
                        return True
                    continue
                return True
    return False


def domain_of(url: str) -> str:
    """Extract bare hostname from URL for the BLOCKED tag and telemetry key."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""
    return host.lower().lstrip("www.") or "unknown"


def blocked_response(url: str, *, engine: str = "") -> str:
    """Standardised marker that upstream routing parses to halt retries."""
    domain = domain_of(url)
    suffix = f" (engine={engine})" if engine else ""
    return (
        f"[CLOUDFLARE_BLOCKED: {domain}]{suffix}\n"
        f"This site is protected by Cloudflare and the VPS IP is being challenged. "
        f"Browser cannot proceed. Suggest: official API, direct courier/source site, "
        f"or admit the limit to the user. Do NOT retry the same URL."
    )
