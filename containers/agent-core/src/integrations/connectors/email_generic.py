"""Generic Email connector — IMAP read + SMTP send for any provider.

Unlike gmail_connector.py (Gmail-specific App Password auth), this connector
works with any email provider: Outlook, Yahoo, Fastmail, ProtonMail Bridge,
self-hosted servers, etc.

Secrets:
    address       — Email address (e.g. user@example.com)
    password      — Email password or app password
    imap_host     — IMAP server hostname (e.g. imap.example.com)
    smtp_host     — SMTP server hostname (e.g. smtp.example.com)
    imap_port     — IMAP port (default 993 for TLS, 143 for STARTTLS)
    smtp_port     — SMTP port (default 587 for STARTTLS, 465 for SSL)
    imap_ssl      — Use SSL for IMAP (default true; false for STARTTLS on port 143)
    smtp_ssl      — Use SSL for SMTP (default false = STARTTLS; true = SMTP over SSL port 465)

Actions:
    list_messages   — List recent inbox messages (headers only)         (LOW)
    read_message    — Read full content of a message by UID             (LOW)
    search          — Search mailbox by IMAP query                      (LOW)
    send_message    — Send an email                                     (HIGH)
    mark_read       — Mark a message as read                           (MEDIUM)
    move_message    — Move message to a folder                         (MEDIUM)
    delete_message  — Move message to Trash                            (HIGH)
    list_folders    — List all IMAP mailbox folders                    (LOW)
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

import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
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


class EmailGenericConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="email-generic", version="1.0.0", name="Email (Generic)", category="productivity",
            description=(
                "Read and send email via IMAP/SMTP — works with any email provider. "
                "Configure your own server settings: Outlook, Yahoo, Fastmail, self-hosted, etc."
            ),
            capabilities=["read_inbox", "search_email", "send_email", "manage_messages", "list_folders"],
            risk_level=RiskLevel.HIGH,
            required_secrets=["address", "password", "imap_host", "smtp_host"],
            config_schema={},
            rate_limits={
                "list_messages":  RateLimit(requests_per_minute=20),
                "read_message":   RateLimit(requests_per_minute=30),
                "search":         RateLimit(requests_per_minute=20),
                "send_message":   RateLimit(requests_per_minute=10, requests_per_hour=100),
                "mark_read":      RateLimit(requests_per_minute=30),
                "move_message":   RateLimit(requests_per_minute=20),
                "delete_message": RateLimit(requests_per_minute=10),
                "list_folders":   RateLimit(requests_per_minute=10),
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
                        ParamSpec("query", "string", "IMAP search query (e.g. FROM user@example.com SINCE 01-Jan-2024)", required=True),
                        ParamSpec("folder", "string", "Folder to search (default INBOX)", required=False),
                        ParamSpec("limit", "integer", "Max results (default 10)", required=False),
                    ]),
                ActionSpec(id="send_message", description="Send an email from the configured address",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[
                        ParamSpec("to", "string", "Recipient address(es), comma-separated", required=True),
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
                ActionSpec(id="move_message", description="Move a message to a different folder",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("uid", "string", "Message UID", required=True),
                        ParamSpec("destination", "string", "Destination folder name", required=True),
                        ParamSpec("folder", "string", "Source folder (default INBOX)", required=False),
                    ]),
                ActionSpec(id="delete_message", description="Move message to Trash folder",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[
                        ParamSpec("uid", "string", "Message UID", required=True),
                        ParamSpec("folder", "string", "Source folder (default INBOX)", required=False),
                        ParamSpec("trash_folder", "string", "Trash folder name (default: Trash)", required=False),
                    ]),
                ActionSpec(id="list_folders", description="List all IMAP mailbox folders/labels",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
            ],
            homepage="",
            docs_url="https://tools.ietf.org/html/rfc3501",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        address   = secrets.get("address", "")
        password  = secrets.get("password", "")
        imap_host = secrets.get("imap_host", "")
        smtp_host = secrets.get("smtp_host", "")
        if not address or not password or not imap_host:
            return self.err("address, password, and imap_host secrets are required")

        imap_port = int(secrets.get("imap_port") or 993)
        smtp_port = int(secrets.get("smtp_port") or 587)
        imap_ssl  = str(secrets.get("imap_ssl", "true")).lower() != "false"
        smtp_ssl  = str(secrets.get("smtp_ssl", "false")).lower() == "true"

        cfg = dict(
            address=address, password=password,
            imap_host=imap_host, imap_port=imap_port, imap_ssl=imap_ssl,
            smtp_host=smtp_host or imap_host, smtp_port=smtp_port, smtp_ssl=smtp_ssl,
        )

        if action == "list_messages":  return await self._imap_op(self._list_messages, params, cfg)
        if action == "read_message":   return await self._imap_op(self._read_message, params, cfg)
        if action == "search":         return await self._imap_op(self._search, params, cfg)
        if action == "mark_read":      return await self._imap_op(self._mark_read, params, cfg)
        if action == "move_message":   return await self._imap_op(self._move_message, params, cfg)
        if action == "delete_message": return await self._imap_op(self._delete_message, params, cfg)
        if action == "list_folders":   return await self._imap_op(self._list_folders, params, cfg)
        if action == "send_message":
            if not smtp_host:
                return self.err("smtp_host secret is required for send_message")
            return await self._send(params, cfg)
        return self.err(f"Unknown action: {action}")

    async def _imap_op(self, fn, params, cfg):
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, fn, params, cfg)
        except Exception as exc:
            logger.error("email_generic.imap_error", error=str(exc))
            return self.err(f"IMAP error: {exc}")

    def _connect_imap(self, cfg: dict) -> imaplib.IMAP4:
        ctx = ssl.create_default_context()
        if cfg["imap_ssl"]:
            m: imaplib.IMAP4 = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], ssl_context=ctx)
        else:
            m = imaplib.IMAP4(cfg["imap_host"], cfg["imap_port"])
            m.starttls(ssl_context=ctx)
        m.login(cfg["address"], cfg["password"])
        return m

    def _list_messages(self, params: dict, cfg: dict) -> dict:
        limit  = min(int(params.get("limit") or 10), 50)
        folder = params.get("folder") or "INBOX"
        unread = params.get("unread_only", False)
        m = self._connect_imap(cfg)
        try:
            m.select(folder, readonly=True)
            criteria = "UNSEEN" if unread else "ALL"
            _, data  = m.uid("search", None, criteria)
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

    def _read_message(self, params: dict, cfg: dict) -> dict:
        uid    = params.get("uid", "")
        folder = params.get("folder") or "INBOX"
        m = self._connect_imap(cfg)
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
                    if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
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

    def _search(self, params: dict, cfg: dict) -> dict:
        folder = params.get("folder") or "INBOX"
        limit  = min(int(params.get("limit") or 10), 50)
        query  = params.get("query", "ALL")
        m = self._connect_imap(cfg)
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
                            "uid":     uid,
                            "from":    _decode_header(msg.get("From", "")),
                            "subject": _decode_header(msg.get("Subject", "")),
                            "date":    msg.get("Date", ""),
                        })
            return self.ok({"messages": messages, "count": len(messages), "query": query})
        finally:
            m.logout()

    def _mark_read(self, params: dict, cfg: dict) -> dict:
        uid    = params.get("uid", "")
        folder = params.get("folder") or "INBOX"
        m = self._connect_imap(cfg)
        try:
            m.select(folder)
            m.uid("store", uid, "+FLAGS", "\\Seen")
            return self.ok({"uid": uid, "marked_read": True})
        finally:
            m.logout()

    def _move_message(self, params: dict, cfg: dict) -> dict:
        uid    = params.get("uid", "")
        dest   = params.get("destination", "")
        folder = params.get("folder") or "INBOX"
        m = self._connect_imap(cfg)
        try:
            m.select(folder)
            m.uid("copy", uid, dest)
            m.uid("store", uid, "+FLAGS", "\\Deleted")
            m.expunge()
            return self.ok({"uid": uid, "moved_to": dest})
        finally:
            m.logout()

    def _delete_message(self, params: dict, cfg: dict) -> dict:
        uid          = params.get("uid", "")
        folder       = params.get("folder") or "INBOX"
        trash_folder = params.get("trash_folder") or "Trash"
        m = self._connect_imap(cfg)
        try:
            m.select(folder)
            m.uid("copy", uid, trash_folder)
            m.uid("store", uid, "+FLAGS", "\\Deleted")
            m.expunge()
            return self.ok({"uid": uid, "deleted": True, "trash_folder": trash_folder})
        finally:
            m.logout()

    def _list_folders(self, params: dict, cfg: dict) -> dict:
        m = self._connect_imap(cfg)
        try:
            _, folder_list = m.list()
            folders = []
            for f in folder_list:
                if isinstance(f, bytes):
                    decoded = f.decode("utf-8", errors="replace")
                    # Format: (\HasNoChildren) "/" INBOX  →  extract last quoted/unquoted token
                    parts = decoded.split('"')
                    name  = parts[-1].strip() if parts else ""
                    if name:
                        folders.append(name)
            return self.ok({"folders": folders, "count": len(folders)})
        finally:
            m.logout()

    async def _send(self, params: dict, cfg: dict) -> dict:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._smtp_send, params, cfg)
        except Exception as exc:
            return self.err(f"SMTP error: {exc}")

    def _smtp_send(self, params: dict, cfg: dict) -> dict:
        msg = MIMEMultipart("alternative") if params.get("html_body") else MIMEText(params["body"], "plain", "utf-8")
        if params.get("html_body"):
            msg.attach(MIMEText(params["body"], "plain", "utf-8"))
            msg.attach(MIMEText(params["html_body"], "html", "utf-8"))
        msg["From"]    = cfg["address"]
        msg["To"]      = params["to"]
        msg["Subject"] = params.get("subject", "")
        if params.get("cc"):
            msg["Cc"] = params["cc"]

        ctx = ssl.create_default_context()
        if cfg["smtp_ssl"]:
            with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=ctx) as server:
                server.login(cfg["address"], cfg["password"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.login(cfg["address"], cfg["password"])
                server.send_message(msg)

        return self.ok({"sent": True, "to": params["to"], "subject": params.get("subject", "")})
