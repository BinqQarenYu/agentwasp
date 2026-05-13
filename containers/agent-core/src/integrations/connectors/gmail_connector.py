"""Gmail integration connector — IMAP read + SMTP send (App Password auth).

Uses App Password authentication (same mechanism as the existing gmail skill).
This connector adds policy-gating, circuit-breaker, vault-encrypted secrets,
and integration-level metrics to Gmail operations.

Secrets:
    address      — Gmail address (e.g. user@example.com)
    app_password — Gmail App Password (16-char, spaces optional)

Actions:
    list_messages   — List recent inbox messages (headers only)         (LOW)
    read_message    — Read full content of a message by UID             (LOW)
    search          — Search mailbox by IMAP query                      (LOW)
    send_message    — Send an email                                     (HIGH)
    mark_read       — Mark a message as read                           (MEDIUM)
    move_message    — Move message to a folder/label                   (MEDIUM)
    delete_message  — Permanently delete a message                     (HIGH)
"""
from __future__ import annotations

import asyncio
import email
import email.header
import imaplib
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_IMAP_HOST = "imap.gmail.com"
_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587
_IMAP_PORT = 993
_MAX_BODY = 8000


def _decode_header(value: str | bytes) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    parts = []
    for decoded, charset in email.header.decode_header(value):
        if isinstance(decoded, bytes):
            decoded = decoded.decode(charset or "utf-8", errors="replace")
        parts.append(str(decoded))
    return " ".join(parts)


class GmailConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="gmail-connector", version="1.0.0", name="Gmail", category="productivity",
            description="Read and send Gmail via IMAP/SMTP with App Password auth. Policy-gated and audited.",
            capabilities=["read_inbox", "search_email", "send_email", "manage_messages"],
            risk_level=RiskLevel.HIGH,
            required_secrets=["address", "app_password"],
            config_schema={},
            rate_limits={
                "list_messages":  RateLimit(requests_per_minute=20),
                "read_message":   RateLimit(requests_per_minute=30),
                "search":         RateLimit(requests_per_minute=20),
                "send_message":   RateLimit(requests_per_minute=10, requests_per_hour=100),
                "mark_read":      RateLimit(requests_per_minute=30),
                "move_message":   RateLimit(requests_per_minute=20),
                "delete_message": RateLimit(requests_per_minute=10),
            },
            actions=[
                ActionSpec(id="list_messages", description="List recent inbox messages (subject, sender, date only)",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("limit", "integer", "Max messages (default 10)", required=False),
                        ParamSpec("folder", "string", "Mailbox folder (default INBOX)", required=False),
                        ParamSpec("unread_only", "boolean", "Only unread messages (default false)", required=False),
                    ]),
                ActionSpec(id="read_message", description="Read full content of a message by UID",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("uid", "string", "Message UID from list_messages", required=True),
                        ParamSpec("folder", "string", "Mailbox folder (default INBOX)", required=False),
                    ]),
                ActionSpec(id="search", description="Search mailbox using IMAP query syntax",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("query", "string", "IMAP search query (e.g. FROM user@example.com, SUBJECT invoice)", required=True),
                        ParamSpec("folder", "string", "Folder to search (default INBOX)", required=False),
                        ParamSpec("limit", "integer", "Max results (default 10)", required=False),
                    ]),
                ActionSpec(id="send_message", description="Send an email from the configured Gmail address",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[
                        ParamSpec("to", "string", "Recipient email address(es), comma-separated", required=True),
                        ParamSpec("subject", "string", "Email subject", required=True),
                        ParamSpec("body", "string", "Email body (plain text)", required=True),
                        ParamSpec("cc", "string", "CC addresses, comma-separated", required=False),
                        ParamSpec("html_body", "string", "Optional HTML body", required=False),
                    ]),
                ActionSpec(id="mark_read", description="Mark a message as read",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("uid", "string", "Message UID", required=True),
                        ParamSpec("folder", "string", "Folder (default INBOX)", required=False),
                    ]),
                ActionSpec(id="move_message", description="Move message to a different folder/label",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("uid", "string", "Message UID", required=True),
                        ParamSpec("destination", "string", "Destination folder name", required=True),
                        ParamSpec("folder", "string", "Source folder (default INBOX)", required=False),
                    ]),
                ActionSpec(id="delete_message", description="Permanently delete a message (move to Trash)",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[
                        ParamSpec("uid", "string", "Message UID", required=True),
                        ParamSpec("folder", "string", "Source folder (default INBOX)", required=False),
                    ]),
            ],
            homepage="https://mail.google.com",
            docs_url="https://support.google.com/accounts/answer/185833",  # App Passwords
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        address  = secrets.get("address", "")
        password = (secrets.get("app_password") or "").replace(" ", "")
        if not address or not password:
            return self.err("address and app_password are required")

        if action == "list_messages":  return await self._imap_op(self._list_messages, params, address, password)
        if action == "read_message":   return await self._imap_op(self._read_message, params, address, password)
        if action == "search":         return await self._imap_op(self._search, params, address, password)
        if action == "mark_read":      return await self._imap_op(self._mark_read, params, address, password)
        if action == "move_message":   return await self._imap_op(self._move_message, params, address, password)
        if action == "delete_message": return await self._imap_op(self._delete_message, params, address, password)
        if action == "send_message":   return await self._send(params, address, password)
        return self.err(f"Unknown action: {action}")

    # ------------------------------------------------------------------
    # IMAP operations (run in thread pool — imaplib is blocking)
    # ------------------------------------------------------------------

    async def _imap_op(self, fn, params, address, password):
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, fn, params, address, password)
        except Exception as exc:
            logger.error("gmail_connector.imap_error", error=str(exc))
            return self.err(f"IMAP error: {exc}")

    def _connect_imap(self, address: str, password: str) -> imaplib.IMAP4_SSL:
        ctx = ssl.create_default_context()
        m = imaplib.IMAP4_SSL(_IMAP_HOST, _IMAP_PORT, ssl_context=ctx)
        m.login(address, password)
        return m

    def _list_messages(self, params: dict, address: str, password: str) -> dict:
        limit  = min(int(params.get("limit") or 10), 50)
        folder = params.get("folder") or "INBOX"
        unread = params.get("unread_only", False)
        m = self._connect_imap(address, password)
        try:
            m.select(folder, readonly=True)
            criteria = "UNSEEN" if unread else "ALL"
            _, data = m.uid("search", None, criteria)
            uids = (data[0].decode().split() if data[0] else [])[-limit:][::-1]
            messages = []
            for uid in uids:
                _, msg_data = m.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if msg_data and msg_data[0]:
                    raw = msg_data[0][1]
                    if isinstance(raw, bytes):
                        msg = email.message_from_bytes(raw)
                        messages.append({
                            "uid":     uid,
                            "from":    _decode_header(msg.get("From", "")),
                            "subject": _decode_header(msg.get("Subject", "")),
                            "date":    msg.get("Date", ""),
                        })
            return self.ok({"messages": messages, "count": len(messages), "folder": folder})
        finally:
            m.logout()

    def _read_message(self, params: dict, address: str, password: str) -> dict:
        uid    = params.get("uid", "")
        folder = params.get("folder") or "INBOX"
        m = self._connect_imap(address, password)
        try:
            m.select(folder, readonly=True)
            _, data = m.uid("fetch", uid, "(RFC822)")
            if not data or not data[0]:
                return self.err(f"Message UID {uid} not found")
            raw = data[0][1]
            msg = email.message_from_bytes(raw if isinstance(raw, bytes) else raw.encode())
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ct = part.get_content_type()
                    if ct == "text/plain" and not part.get("Content-Disposition"):
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="replace")
                            break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")
            return self.ok({
                "uid":     uid,
                "from":    _decode_header(msg.get("From", "")),
                "to":      _decode_header(msg.get("To", "")),
                "subject": _decode_header(msg.get("Subject", "")),
                "date":    msg.get("Date", ""),
                "body":    body[:_MAX_BODY],
            })
        finally:
            m.logout()

    def _search(self, params: dict, address: str, password: str) -> dict:
        folder = params.get("folder") or "INBOX"
        limit  = min(int(params.get("limit") or 10), 50)
        query  = params.get("query", "ALL")
        m = self._connect_imap(address, password)
        try:
            m.select(folder, readonly=True)
            _, data = m.uid("search", None, query)
            uids = (data[0].decode().split() if data[0] else [])[-limit:][::-1]
            messages = []
            for uid in uids:
                _, msg_data = m.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if msg_data and msg_data[0]:
                    raw = msg_data[0][1]
                    if isinstance(raw, bytes):
                        msg = email.message_from_bytes(raw)
                        messages.append({
                            "uid": uid,
                            "from": _decode_header(msg.get("From", "")),
                            "subject": _decode_header(msg.get("Subject", "")),
                            "date": msg.get("Date", ""),
                        })
            return self.ok({"messages": messages, "count": len(messages), "query": query})
        finally:
            m.logout()

    def _mark_read(self, params: dict, address: str, password: str) -> dict:
        uid    = params.get("uid", "")
        folder = params.get("folder") or "INBOX"
        m = self._connect_imap(address, password)
        try:
            m.select(folder)
            m.uid("store", uid, "+FLAGS", "\\Seen")
            return self.ok({"uid": uid, "marked_read": True})
        finally:
            m.logout()

    def _move_message(self, params: dict, address: str, password: str) -> dict:
        uid    = params.get("uid", "")
        dest   = params.get("destination", "")
        folder = params.get("folder") or "INBOX"
        m = self._connect_imap(address, password)
        try:
            m.select(folder)
            m.uid("copy", uid, dest)
            m.uid("store", uid, "+FLAGS", "\\Deleted")
            m.expunge()
            return self.ok({"uid": uid, "moved_to": dest})
        finally:
            m.logout()

    def _delete_message(self, params: dict, address: str, password: str) -> dict:
        uid    = params.get("uid", "")
        folder = params.get("folder") or "INBOX"
        m = self._connect_imap(address, password)
        try:
            m.select(folder)
            m.uid("copy", uid, "[Gmail]/Trash")
            m.uid("store", uid, "+FLAGS", "\\Deleted")
            m.expunge()
            return self.ok({"uid": uid, "deleted": True})
        finally:
            m.logout()

    # ------------------------------------------------------------------
    # SMTP send
    # ------------------------------------------------------------------

    async def _send(self, params: dict, address: str, password: str) -> dict:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._smtp_send, params, address, password)
        except Exception as exc:
            return self.err(f"SMTP error: {exc}")

    def _smtp_send(self, params: dict, address: str, password: str) -> dict:
        msg = MIMEMultipart("alternative") if params.get("html_body") else MIMEText(params["body"], "plain", "utf-8")
        if params.get("html_body"):
            msg.attach(MIMEText(params["body"], "plain", "utf-8"))
            msg.attach(MIMEText(params["html_body"], "html", "utf-8"))
        msg["From"]    = address
        msg["To"]      = params["to"]
        msg["Subject"] = params.get("subject", "")
        if params.get("cc"): msg["Cc"] = params["cc"]
        ctx = ssl.create_default_context()
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.login(address, password)
            server.send_message(msg)
        return self.ok({"sent": True, "to": params["to"], "subject": params.get("subject", "")})
