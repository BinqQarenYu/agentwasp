"""Gmail skill — read, send, search, and delete emails via IMAP/SMTP with App Password."""

import asyncio
import email
import email.header
import email.utils
import imaplib
import mimetypes
import os
import smtplib
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

import redis.asyncio as aioredis
import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()

MAX_OUTPUT_CHARS = 8000
IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
REDIS_GMAIL_KEY = "gmail:credentials"


def _decode_header(raw: str) -> str:
    """Decode an email header value (handles encoded-word syntax)."""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def _get_text_body(msg: email.message.Message) -> str:
    """Extract plain-text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: try text/html
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    import re
                    text = re.sub(r"<[^>]+>", "", html)
                    text = re.sub(r"\s+", " ", text).strip()
                    return text
        return "(no text body)"
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        return "(empty)"


VAULT_INTEGRATION_ID = "gmail-connector"


class GmailSkill(SkillBase):
    def __init__(self, redis_url: str, address: str = "", app_password: str = "", vault=None, policy=None):
        self._redis_url = redis_url
        self._address = address
        self._password = app_password
        self._vault = vault    # SecretVault — encrypted integration credentials
        self._policy = policy  # PolicyEngine — enable/disable gate

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="gmail",
            description=(
                "Operate Gmail: configure credentials, read inbox, read a specific email, "
                "send emails, search emails, and delete emails."
            ),
            params=[
                SkillParam(
                    name="action",
                    param_type=ParamType.STRING,
                    description="Action: configure, inbox, read, send, delete, search",
                ),
                SkillParam(
                    name="count",
                    param_type=ParamType.STRING,
                    description="Number of emails to list (for inbox/search, default 10)",
                    required=False,
                    default="10",
                ),
                SkillParam(
                    name="email_id",
                    param_type=ParamType.STRING,
                    description="Email ID to read or delete",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="to",
                    param_type=ParamType.STRING,
                    description="Recipient email address (for send)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="subject",
                    param_type=ParamType.STRING,
                    description="Email subject (for send)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="body",
                    param_type=ParamType.STRING,
                    description=(
                        "Email body text (for send). "
                        "When sending a report: put the COMPLETE report text here — "
                        "all sections, all data points, everything the recipient needs to read. "
                        "Do NOT write a one-liner like 'see attachments'. "
                        "The body is what the email recipient reads; attachments are supplementary visuals."
                    ),
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="query",
                    param_type=ParamType.STRING,
                    description='IMAP search query (for search). Examples: FROM user@example.com, SUBJECT factura, UNSEEN',
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="attachments",
                    param_type=ParamType.STRING,
                    description="Comma-separated file paths to attach (for send). ALWAYS pass screenshot paths here, never embed them in the body. E.g. /data/screenshots/screenshot_123.png,/data/screenshots/screenshot_124.png",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="address",
                    param_type=ParamType.STRING,
                    description="Gmail address (for configure action)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="password",
                    param_type=ParamType.STRING,
                    description="Gmail App Password (for configure action)",
                    required=False,
                    default="",
                ),
            ],
            category="communication",
            timeout_seconds=30.0,
        )

    async def _load_credentials(self):
        """Load credentials — vault (integration system) first, legacy Redis fallback."""
        if self._address and self._password:
            return
        # Try vault first (single source of truth)
        if self._vault:
            try:
                secrets = await self._vault.get_all(VAULT_INTEGRATION_ID)
                if secrets.get("address") and secrets.get("app_password"):
                    self._address = secrets["address"]
                    self._password = secrets["app_password"]
                    return
            except Exception:
                pass
        # Fall back to legacy plain-text Redis key
        try:
            r = aioredis.from_url(self._redis_url, decode_responses=True)
            try:
                creds = await r.hgetall(REDIS_GMAIL_KEY)
                if creds.get("address") and creds.get("password"):
                    self._address = creds["address"]
                    self._password = creds["password"]
            finally:
                await r.aclose()
        except Exception:
            pass

    async def execute(
        self,
        action: str,
        count: str = "10",
        email_id: str = "",
        to: str = "",
        subject: str = "",
        body: str = "",
        query: str = "",
        address: str = "",
        password: str = "",
        attachments: str = "",
        **kwargs,
    ) -> SkillResult:
        action = action.lower().strip()

        # Configure doesn't need existing credentials or policy gate
        if action == "configure":
            return await self._configure(address=address, password=password)

        # Policy gate — check integration is enabled before any operation
        if self._policy and not self._policy.is_enabled(VAULT_INTEGRATION_ID):
            return SkillResult(
                skill_name="gmail",
                success=False,
                output="",
                error="Gmail integration is disabled. Enable it from the Integrations panel or run gmail(action=\"configure\", ...) to set up.",
            )

        # For all other actions, load credentials
        await self._load_credentials()

        if not self._address or not self._password:
            return SkillResult(
                skill_name="gmail",
                success=False,
                output="",
                error="Gmail no configurado. Usa gmail(action=\"configure\", address=\"user@example.com\", password=\"tu-app-password\") para configurar.",
            )

        # send_check: auto-detected send request — credentials verified, instruct LLM to call send
        if action == "send_check":
            # Use the locked recipient if propagated from auto_detect (parameter immutability)
            _locked_to = kwargs.get("_locked_to", "")
            _to_instruction = (
                f"Use to='{_locked_to}' exactly — do NOT change this address."
                if _locked_to
                else "Use the recipient from the user's message exactly as written."
            )
            return SkillResult(
                skill_name="gmail",
                success=True,
                output=(
                    f"Gmail credentials loaded. {_to_instruction} "
                    "Call gmail(action='send', to='...', subject='...', body='...') NOW. "
                    "Do NOT confirm to the user until gmail(action='send') returns success."
                ),
                error="",
            )

        dispatch = {
            "inbox": self._inbox,
            "read": self._read,
            "send": self._send,
            "delete": self._delete,
            "search": self._search,
        }
        handler = dispatch.get(action)
        if not handler:
            return SkillResult(
                skill_name="gmail",
                success=False,
                output="",
                error=f"Unknown action: {action}. Use: configure, inbox, read, send, delete, search",
            )

        try:
            return await handler(
                count=count, email_id=email_id, to=to,
                subject=subject, body=body, query=query,
                attachments=attachments,
                **kwargs,
            )
        except Exception as e:
            return SkillResult(
                skill_name="gmail", success=False, output="", error=str(e),
            )

    # ── Configure ──

    async def _configure(self, address: str = "", password: str = "") -> SkillResult:
        if not address or not password:
            return SkillResult(
                skill_name="gmail",
                success=False,
                output="",
                error="Both 'address' and 'password' are required for configure action.",
            )

        # Test connection before saving
        try:
            def _test():
                conn = imaplib.IMAP4_SSL(IMAP_HOST)
                conn.login(address, password)
                conn.select("INBOX", readonly=True)
                conn.logout()
                return True
            await asyncio.to_thread(_test)
        except Exception as e:
            return SkillResult(
                skill_name="gmail",
                success=False,
                output="",
                error=f"Login failed: {e}. Check address and app password.",
            )

        # Save to vault (integration system — encrypted, single source of truth)
        vault_saved = False
        if self._vault:
            try:
                await self._vault.set(VAULT_INTEGRATION_ID, "address", address)
                await self._vault.set(VAULT_INTEGRATION_ID, "app_password", password)
                vault_saved = True
            except Exception as e:
                return SkillResult(
                    skill_name="gmail",
                    success=False,
                    output="",
                    error=f"Could not save credentials to vault: {e}",
                )

        # Enable integration policy so dashboard shows ENABLED
        if self._policy:
            try:
                await self._policy.enable(VAULT_INTEGRATION_ID)
            except Exception:
                pass

        # Also keep legacy Redis key in sync (for backward compatibility)
        try:
            r = aioredis.from_url(self._redis_url, decode_responses=True)
            try:
                await r.hset(REDIS_GMAIL_KEY, mapping={
                    "address": address,
                    "password": password,
                })
            finally:
                await r.aclose()
        except Exception:
            pass  # Non-fatal if vault succeeded

        # Update in-memory
        self._address = address
        self._password = password

        storage_note = " (saved to integrations vault)" if vault_saved else ""
        return SkillResult(
            skill_name="gmail",
            success=True,
            output=f"Gmail configured for {address}. Connection verified{storage_note}.",
            error="",
        )

    # ── IMAP helpers ──

    def _imap_connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(IMAP_HOST)
        conn.login(self._address, self._password)
        return conn

    def _fetch_headers(self, conn: imaplib.IMAP4_SSL, msg_ids: list[bytes], max_count: int) -> str:
        """Fetch headers for a list of message IDs and return formatted table."""
        ids_to_fetch = msg_ids[-max_count:] if len(msg_ids) > max_count else msg_ids
        ids_to_fetch = list(reversed(ids_to_fetch))  # newest first

        lines = ["ID | From | Subject | Date"]
        lines.append("---|------|---------|-----")
        for mid in ids_to_fetch:
            status, data = conn.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if status != "OK" or not data or not data[0]:
                continue
            raw = data[0][1] if isinstance(data[0], tuple) else data[0]
            if isinstance(raw, bytes):
                msg = email.message_from_bytes(raw)
            else:
                continue
            from_addr = _decode_header(msg.get("From", ""))
            subj = _decode_header(msg.get("Subject", "(sin asunto)"))
            date_raw = msg.get("Date", "")
            parsed = email.utils.parsedate_to_datetime(date_raw) if date_raw else None
            date_str = parsed.strftime("%Y-%m-%d %H:%M") if parsed else date_raw[:16]
            if len(from_addr) > 30:
                from_addr = from_addr[:27] + "..."
            if len(subj) > 50:
                subj = subj[:47] + "..."
            lines.append(f"{mid.decode()} | {from_addr} | {subj} | {date_str}")

        return "\n".join(lines)

    # ── Actions ──

    async def _inbox(self, count: str = "10", **kw) -> SkillResult:
        max_count = min(int(count), 50) if count.isdigit() else 10

        def _do():
            conn = self._imap_connect()
            try:
                conn.select("INBOX", readonly=True)
                status, data = conn.search(None, "ALL")
                if status != "OK":
                    return "No se pudo acceder al inbox."
                msg_ids = data[0].split() if data[0] else []
                if not msg_ids:
                    return "Inbox vacío. No hay correos."
                total = len(msg_ids)
                result = self._fetch_headers(conn, msg_ids, max_count)
                return f"Total: {total} correos. Mostrando últimos {min(max_count, total)}:\n\n{result}"
            finally:
                conn.logout()

        output = await asyncio.to_thread(_do)
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
        return SkillResult(skill_name="gmail", success=True, output=output, error="")

    async def _read(self, email_id: str = "", **kw) -> SkillResult:
        if not email_id:
            return SkillResult(
                skill_name="gmail", success=False, output="",
                error="email_id is required for read action",
            )

        def _do():
            conn = self._imap_connect()
            try:
                conn.select("INBOX", readonly=True)
                status, data = conn.fetch(email_id.encode(), "(RFC822)")
                if status != "OK" or not data or not data[0]:
                    return None, f"Email ID {email_id} not found."
                raw = data[0][1] if isinstance(data[0], tuple) else data[0]
                msg = email.message_from_bytes(raw)
                from_addr = _decode_header(msg.get("From", ""))
                to_addr = _decode_header(msg.get("To", ""))
                subj = _decode_header(msg.get("Subject", ""))
                date_str = msg.get("Date", "")
                body_text = _get_text_body(msg)
                output = (
                    f"From: {from_addr}\n"
                    f"To: {to_addr}\n"
                    f"Subject: {subj}\n"
                    f"Date: {date_str}\n"
                    f"\n{body_text}"
                )
                return output, None
            finally:
                conn.logout()

        output, err = await asyncio.to_thread(_do)
        if err:
            return SkillResult(skill_name="gmail", success=False, output="", error=err)
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
        return SkillResult(skill_name="gmail", success=True, output=output, error="")

    async def _send(self, to: str = "", subject: str = "", body: str = "", attachments: str = "", **kw) -> SkillResult:
        if not to:
            return SkillResult(
                skill_name="gmail", success=False, output="",
                error="'to' is required for send action",
            )

        # ── Recipient allowlist (defense-in-depth vs prompt injection) ────────
        # If GMAIL_RECIPIENT_ALLOWLIST is set, refuse to send to anything not
        # explicitly permitted. Entries may be a full email (alice@example.com) or a
        # domain prefixed with @ (@company.com).  Empty = no restriction.
        import os as _os
        _allowlist_raw = _os.environ.get("GMAIL_RECIPIENT_ALLOWLIST", "").strip()
        if _allowlist_raw:
            _allowed = [e.strip().lower() for e in _allowlist_raw.split(",") if e.strip()]
            _recipients = [r.strip().lower() for r in to.split(",") if r.strip()]
            for _rcpt in _recipients:
                _ok = False
                for _entry in _allowed:
                    if _entry.startswith("@"):
                        if _rcpt.endswith(_entry):
                            _ok = True
                            break
                    elif _rcpt == _entry:
                        _ok = True
                        break
                if not _ok:
                    logger.warning(
                        "gmail.send_blocked_allowlist",
                        rejected=_rcpt, allowlist_size=len(_allowed),
                    )
                    return SkillResult(
                        skill_name="gmail", success=False, output="",
                        error=(
                            f"Recipient '{_rcpt}' is not in GMAIL_RECIPIENT_ALLOWLIST. "
                            f"Ask the operator to add it before retrying."
                        ),
                    )

        # ── Last-resort placeholder content guard ────────────────────────
        # If the caller (LLM, fast-path, action_commitment retry) supplied
        # placeholder/empty subject and body, refuse the send. The agent
        # must ask the user what to send instead of dispatching a junk email.
        # Mirrors `_is_placeholder_subject` / `_is_placeholder_body` from
        # events.handlers, but local to keep this skill self-contained.
        _PLACEHOLDER_SUBJ = {
            "", "subject", "subject here", "your subject", "your subject here",
            "(no subject)", "no subject", "untitled", "asunto", "sin asunto",
            "tu asunto", "tema", "asunto del correo",
            "todo", "tbd", "placeholder", "test", "prueba", "hola", "hello",
            "saludo", "saludos",
        }
        _PLACEHOLDER_BODY = {
            "", "body", "body here", "your body", "your body here",
            "test", "prueba", "hello", "hola", "saludo", "saludos",
            "todo", "tbd", "placeholder", "tu mensaje", "your message here",
        }
        import re as _re
        _placeholder_like = _re.compile(
            r"^\s*(?:your\s+\w+|tu\s+\w+|\[[^\]]*\]|<[^>]*>|"
            r"\([^\)]*here\)|\.\.\.+|saludo[s]?|hello|hi|hola)\s*[.,;:!?]?\s*$",
            _re.IGNORECASE,
        )

        def _is_placeholder(value: str, lookup: set, min_len: int) -> bool:
            if not value:
                return True
            v = value.strip().lower()
            if len(v) < min_len:
                return True
            if v in lookup:
                return True
            if _placeholder_like.match(v):
                return True
            return False

        _bad_subj = _is_placeholder(subject, _PLACEHOLDER_SUBJ, min_len=3)
        _bad_body = _is_placeholder(body, _PLACEHOLDER_BODY, min_len=10)
        if _bad_subj and _bad_body:
            logger.warning(
                "gmail.send_blocked_placeholder",
                to=to, subject_preview=subject[:40], body_len=len(body or ""),
            )
            return SkillResult(
                skill_name="gmail", success=False, output="",
                error=(
                    "Placeholder content detected (empty / 'Your Subject' / 'Saludos'). "
                    "Ask the user what to send before retrying — never invent content."
                ),
            )

        # ── Idempotency gate: one send per execution_id ───────────────────────
        # Prevents duplicate SMTP delivery on retries or double LLM skill calls.
        _exec_id = kw.get("_execution_id", "")
        if _exec_id and self._redis_url:
            _idem_key = f"gmail:sent:{_exec_id}"
            try:
                import redis.asyncio as _aioredis
                _r = _aioredis.from_url(self._redis_url, decode_responses=True)
                try:
                    _cached = await _r.get(_idem_key)
                    if _cached:
                        logger.info("gmail.idempotent_skip", execution_id=_exec_id, to=to)
                        return SkillResult(
                            skill_name="gmail", success=True,
                            output=_cached,
                            error="",
                        )
                finally:
                    await _r.aclose()
            except Exception:
                pass  # Redis unavailable — proceed with send (never block on dedup failure)
        if not subject:
            subject = "(sin asunto)"

        # Auto-extract any image paths embedded in the body as markdown ![...](path)
        # and move them to attachments so the body stays clean text
        import re as _re
        body = body or ""
        # Unescape literal \n sequences the LLM sometimes outputs as text
        body = body.replace('\\n', '\n')
        inline_paths = _re.findall(r'!\[.*?\]\((/data/[^\)]+)\)', body)
        # Strip all markdown image syntax from the body
        body = _re.sub(r'!\[.*?\]\([^\)]+\)', '', body)
        # Remove leftover empty numbered list lines (e.g. "1. \n2. \n")
        body = _re.sub(r'(?m)^\d+\.\s*$', '', body)
        # Collapse 3+ blank lines → 2, trim edges
        body = _re.sub(r'\n{3,}', '\n\n', body).strip()

        # ── Placeholder guard: reject body with unfilled template values ──────
        # Catches $X, Y%, [text], N/A that indicate incomplete data passed to gmail.
        _BODY_PLACEHOLDER_RE = _re.compile(
            r'\$[A-Z]\b'           # $X — dollar + single uppercase
            r'|\b[A-Z]%'           # Y% — uppercase + percent
            r'|\[[^\]]{3,80}\]'    # [text in brackets] (min 3 chars to skip emoticons)
            r'|\bN/A\b',
            _re.IGNORECASE,
        )
        _ph_matches = _BODY_PLACEHOLDER_RE.findall(body)
        if _ph_matches:
            return SkillResult(
                skill_name="gmail",
                success=False,
                output="",
                error=(
                    f"SEND BLOCKED: email body contains {len(_ph_matches)} placeholder value(s): "
                    f"{_ph_matches[:4]}. "
                    "Fetch real data, call render_report to build the body, then retry gmail."
                ),
            )

        # Merge explicit attachments + inline-extracted paths
        explicit_paths = [p.strip() for p in attachments.split(",") if p.strip()] if attachments else []
        all_candidate_paths = explicit_paths + inline_paths

        # Validate all paths exist and are safe (no path traversal outside /data)
        valid_paths: list[str] = []
        seen: set[str] = set()
        for p in all_candidate_paths:
            real = os.path.realpath(p)
            if real not in seen and real.startswith("/data/") and os.path.isfile(real):
                valid_paths.append(real)
                seen.add(real)

        # If the caller explicitly specified attachments but none are valid files,
        # fail hard rather than silently sending without them. This prevents the
        # agent from claiming "adjunté el informe" when no file was actually attached.
        if explicit_paths and not valid_paths:
            missing_names = [os.path.basename(p) for p in explicit_paths[:3]]
            return SkillResult(
                skill_name="gmail",
                success=False,
                output="",
                error=(
                    f"Attachment file(s) not found: {', '.join(missing_names)}. "
                    "Cannot send email without the required attachments. "
                    "Retry after the attachment is generated."
                ),
            )

        def _do():
            if valid_paths:
                msg = MIMEMultipart()
                msg.attach(MIMEText(body or "", "plain", "utf-8"))
            else:
                msg = MIMEText(body or "", "plain", "utf-8")

            msg["From"] = self._address
            msg["To"] = to
            msg["Subject"] = subject

            for path in valid_paths:
                mime_type, _ = mimetypes.guess_type(path)
                main_type, sub_type = (mime_type or "application/octet-stream").split("/", 1)
                with open(path, "rb") as f:
                    part = MIMEBase(main_type, sub_type)
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=os.path.basename(path),
                )
                msg.attach(part)

            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                server.login(self._address, self._password)
                server.send_message(msg)

            if valid_paths:
                names = ", ".join(os.path.basename(p) for p in valid_paths)
                return f"Correo enviado a {to} con asunto \"{subject}\" y {len(valid_paths)} adjunto(s): {names}"
            return f"Correo enviado a {to} con asunto \"{subject}\""

        output = await asyncio.to_thread(_do)

        # Cache successful send result for idempotency (TTL = 1h)
        if _exec_id and self._redis_url:
            try:
                import redis.asyncio as _aioredis
                _r = _aioredis.from_url(self._redis_url, decode_responses=True)
                try:
                    await _r.setex(_idem_key, 3600, output)
                finally:
                    await _r.aclose()
            except Exception:
                pass  # Non-fatal — idempotency best-effort

        return SkillResult(skill_name="gmail", success=True, output=output, error="")

    async def _delete(self, email_id: str = "", **kw) -> SkillResult:
        if not email_id:
            return SkillResult(
                skill_name="gmail", success=False, output="",
                error="email_id is required for delete action",
            )

        def _do():
            conn = self._imap_connect()
            try:
                conn.select("INBOX")
                status, _ = conn.store(email_id.encode(), "+FLAGS", "\\Deleted")
                if status != "OK":
                    return None, f"Could not delete email ID {email_id}"
                conn.expunge()
                return f"Correo ID {email_id} eliminado.", None
            finally:
                conn.logout()

        output, err = await asyncio.to_thread(_do)
        if err:
            return SkillResult(skill_name="gmail", success=False, output="", error=err)
        return SkillResult(skill_name="gmail", success=True, output=output, error="")

    async def _search(self, query: str = "", count: str = "10", **kw) -> SkillResult:
        if not query:
            return SkillResult(
                skill_name="gmail", success=False, output="",
                error="'query' is required for search action. Examples: FROM user@example.com, SUBJECT factura, UNSEEN",
            )
        max_count = min(int(count), 50) if count.isdigit() else 10

        def _do():
            conn = self._imap_connect()
            try:
                conn.select("INBOX", readonly=True)
                q = query.strip()
                if not any(q.upper().startswith(k) for k in (
                    "FROM", "TO", "SUBJECT", "BODY", "UNSEEN", "SEEN",
                    "SINCE", "BEFORE", "ON", "ALL", "FLAGGED", "NEW", "OLD",
                )):
                    q = f'SUBJECT "{q}"'
                status, data = conn.search(None, q)
                if status != "OK":
                    return f"Search failed for query: {q}"
                msg_ids = data[0].split() if data[0] else []
                if not msg_ids:
                    return f"No emails found for: {q}"
                total = len(msg_ids)
                result = self._fetch_headers(conn, msg_ids, max_count)
                return f"Found {total} emails for: {q}\n\n{result}"
            finally:
                conn.logout()

        output = await asyncio.to_thread(_do)
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
        return SkillResult(skill_name="gmail", success=True, output=output, error="")
