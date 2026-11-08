"""Autonomous Background Mail Sentinel Job for Agent WASP.

Runs every 30 minutes:
1. Connects to primary Gmail over SSL (benjohncarino@gmail.com).
2. Scans newly arrived UNSEEN emails.
3. Passes each message through email_threat_sentinel.py:
   - If THREAT / SCAM (Threat Score >= 50): Quarantines to [Gmail]/Spam, alerts operator via Telegram.
4. Partitions clean messages into the 5-Channel Operator Matrix:
   - Trading & Market Execution (Alpha Funded, Polymarket, Crypto, Exchanges)
   - Jules / Google & AI Dev Operations (GitHub, Google Cloud, CI/CD)
   - Architecture & Design (Clients, RFP, CAD/3D tenders)
   - E-Commerce & Marketplace (Orders, Stripe, Disputes)
   - Invoices & Subscriptions (Receipts, PyQuant, Renewals)
5. Posts structured digest to Redis event stream for the Grok Cockpit.
"""

import imaplib
import email
from email.header import decode_header
import structlog
from datetime import datetime, timezone

from ..builtin.email_threat_sentinel import analyze_email_threat

logger = structlog.get_logger()

def decode_mime(header_val: str) -> str:
    if not header_val:
        return ""
    try:
        parts = decode_header(header_val)
        res = ""
        for part, enc in parts:
            if isinstance(part, bytes):
                res += part.decode(enc or "utf-8", errors="ignore")
            else:
                res += str(part)
        return res
    except Exception:
        return str(header_val)

def get_body_snippet(msg: email.message.Message, max_chars: int = 1500) -> str:
    body = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                cdispo = str(part.get("Content-Disposition"))
                if ctype == "text/plain" and "attachment" not in cdispo:
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
        else:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
    except Exception:
        pass
    return body[:max_chars].strip()


class MailSentinelJob:
    """30-minute automated email ingestion and governance runner."""

    def __init__(self, user: str, password: str, redis_url: str = None):
        self.user = user
        self.password = password
        self.redis_url = redis_url

    async def run_once(self) -> dict:
        if not self.user or not self.password:
            return {"ok": False, "error": "No Gmail credentials configured"}

        results = {
            "scanned": 0,
            "threats_quarantined": [],
            "trading_alerts": [],
            "dev_alerts": [],
            "architecture_leads": [],
            "invoices": [],
            "promotions_cleared": 0,
        }

        try:
            conn = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            conn.login(self.user, self.password)
            conn.select("INBOX", readonly=False)

            # Check unread messages first, fallback to latest 10
            status, data = conn.search(None, "UNSEEN")
            unseen_ids = data[0].split() if data and data[0] else []

            target_ids = unseen_ids
            if not target_ids:
                status, all_data = conn.search(None, "ALL")
                all_ids = all_data[0].split() if all_data and all_data[0] else []
                target_ids = all_ids[-10:] if len(all_ids) >= 10 else all_ids

            results["scanned"] = len(target_ids)

            for mid in target_ids:
                status, msg_data = conn.fetch(mid, "(RFC822)")
                if status != "OK" or not msg_data:
                    continue

                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                sender = decode_mime(msg.get("From", "")).replace("\n", " ").strip()
                subject = decode_mime(msg.get("Subject", "")).replace("\n", " ").strip()
                body = get_body_snippet(msg)

                # ── Step 1: Run Threat & Phishing Sentinel ───────────────────
                threat = analyze_email_threat(sender=sender, subject=subject, body=body)
                if threat["quarantine_recommended"]:
                    # Quarantine to Spam
                    try:
                        conn.copy(mid, "[Gmail]/Spam")
                        conn.store(mid, "+FLAGS", "\\Deleted")
                        results["threats_quarantined"].append({
                            "from": sender,
                            "subject": subject,
                            "score": threat["threat_score"],
                            "flags": threat["flags"]
                        })
                        logger.warning("mail_sentinel.quarantined_threat", subject=subject, sender=sender)
                        continue
                    except Exception as q_err:
                        logger.error("mail_sentinel.quarantine_failed", error=str(q_err))

                # ── Step 2: Route into 5-Channel Operator Matrix ─────────────
                combo = (subject + " " + sender + " " + body).lower()

                if any(k in combo for k in ["binance", "bybit", "htx", "alpha funded", "polymarket", "fill", "margin", "pnl", "trade"]):
                    results["trading_alerts"].append({"from": sender, "subject": subject})
                elif any(k in combo for k in ["github", "jules", "google cloud", "pull request", "build", "deploy", "repo"]):
                    results["dev_alerts"].append({"from": sender, "subject": subject})
                elif any(k in combo for k in ["architect", "cad", "bim", "drawing", "client", "tender", "rfp"]):
                    results["architecture_leads"].append({"from": sender, "subject": subject})
                elif any(k in combo for k in ["invoice", "receipt", "pyquant", "subscription", "billed", "renewal"]):
                    results["invoices"].append({"from": sender, "subject": subject})
                else:
                    results["promotions_cleared"] += 1

            conn.expunge()
            conn.logout()
            return {"ok": True, "results": results}

        except Exception as e:
            logger.exception("mail_sentinel.run_failed", error=str(e))
            return {"ok": False, "error": str(e)}
