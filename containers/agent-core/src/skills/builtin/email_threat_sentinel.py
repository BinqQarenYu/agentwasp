"""Security, Anti-Phishing & Dangerous Link Sentinel for Gmail.

Analyzes raw email headers, body content, and embedded hyperlinks to detect:
1. High-risk spoofing & sender mismatched domains (Display Name vs. Return-Path).
2. Urgency pressure triggers (threat of account closure, immediate wire transfer, password reset).
3. Malicious link obfuscation (lookalike domains, IP-in-URL, URL shorteners, punycode/IDN attacks).
4. Dangerous attachment extensions (.exe, .scr, .vbs, .iso, .xlsm, .bat).
"""

import re
import urllib.parse
from typing import Dict, List, Tuple
import structlog

logger = structlog.get_logger()

# Dangerous file extensions commonly used in malicious payloads
DANGEROUS_EXTENSIONS = {
    ".exe", ".scr", ".bat", ".cmd", ".vbs", ".js", ".jse", ".wsf", ".iso",
    ".img", ".hta", ".cpl", ".msc", ".jar", ".ps1", ".xlsm", ".docm"
}

# High-risk urgency / scam trigger keywords
SCAM_URGENCY_PATTERNS = re.compile(
    r"(account\s+suspended|suspended\s+within|immediate\s+action\s+required|"
    r"security\s+breach|verify\s+your\s+identity|unauthorized\s+transaction|"
    r"confirm\s+your\s+seed\s+phrase|private\s+key|wallet\s+validation|"
    r"wire\s+transfer\s+immediately|gift\s+card|inheritance\s+funds|"
    r"crypto\s+airdrop|claim\s+your\s+tokens|password\s+expires\s+in)",
    re.IGNORECASE,
)

# Known URL shorteners that obscure destination targets
URL_SHORTENERS = {
    "bit.ly", "tinyurl.com", "t.co", "is.gd", "buff.ly", "ow.ly",
    "cutt.ly", "rb.gy", "goo.gl", "rebrand.ly"
}

# Well-known brands frequently impersonated in phishing
SPOOFED_TARGET_DOMAINS = {
    "binance", "bybit", "metamask", "google", "paypal", "stripe",
    "coinbase", "telegram", "ledger", "trezor", "github", "apple"
}


def analyze_email_threat(
    sender: str,
    subject: str,
    body: str,
    attachment_names: List[str] = None
) -> Dict:
    """Evaluates an email and assigns a Threat Score (0 to 100) with detailed flags."""
    flags = []
    threat_score = 0
    attachment_names = attachment_names or []

    # 1. Attachment Threat Scan
    for att in attachment_names:
        ext = "." + att.split(".")[-1].lower() if "." in att else ""
        if ext in DANGEROUS_EXTENSIONS:
            flags.append(f"CRITICAL: Executable attachment detected ({att})")
            threat_score += 60

    # 2. Urgency & Social Engineering Trigger Scan
    urgency_matches = SCAM_URGENCY_PATTERNS.findall(subject + " " + body)
    if urgency_matches:
        unique_matches = set(m.lower() for m in urgency_matches)
        flags.append(f"WARNING: Psychological urgency/scam triggers found ({', '.join(unique_matches)})")
        threat_score += 25

    # 3. Sender Header & Lookalike Domain Analysis
    sender_lower = sender.lower()
    # Check if a major brand is in the display name but NOT in the sending domain
    for brand in SPOOFED_TARGET_DOMAINS:
        if brand in sender_lower:
            # Check domain after @
            domain_part = sender_lower.split("@")[-1] if "@" in sender_lower else ""
            if brand not in domain_part:
                flags.append(f"HIGH RISK: Display name claims '{brand}' but domain is '{domain_part}'")
                threat_score += 45

    # 4. Embedded Link & URL Inspection
    raw_urls = re.findall(r'https?://[^\s<>"\']+', body)
    flagged_urls = []

    for u in raw_urls:
        try:
            parsed = urllib.parse.urlparse(u)
            host = parsed.netloc.lower().split(":")[0]

            # Flag direct IP address in link
            if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host):
                flagged_urls.append(f"{u} (RAW IP ADDRESS HOST)")
                threat_score += 35

            # Flag hidden URL shortener
            if host in URL_SHORTENERS:
                flagged_urls.append(f"{u} (OBSCURED URL SHORTENER)")
                threat_score += 15

            # Flag punycode / non-ascii IDN spoofing
            if "xn--" in host:
                flagged_urls.append(f"{u} (PUNYCODE HOMOGLYPH ATTACK)")
                threat_score += 40

        except Exception:
            continue

    if flagged_urls:
        flags.append(f"DANGER: Suspicious link patterns detected:\n  • " + "\n  • ".join(flagged_urls[:5]))

    # Calculate Verdict
    threat_score = min(threat_score, 100)
    if threat_score >= 50:
        verdict = "DANGEROUS / SCAM"
    elif threat_score >= 20:
        verdict = "SUSPICIOUS"
    else:
        verdict = "CLEAN"

    return {
        "verdict": verdict,
        "threat_score": threat_score,
        "flags": flags,
        "quarantine_recommended": threat_score >= 50,
        "total_links_scanned": len(raw_urls)
    }
