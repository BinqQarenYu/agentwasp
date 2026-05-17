"""Dashboard routes — Direct agent chat with rich media support."""

import asyncio
import base64
import json
import mimetypes
import re
import uuid
from pathlib import Path

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from ...config import settings
from ...utils.media_signing import generate_signed_media_url, verify_media_url

logger = structlog.get_logger()
router = APIRouter()

UPLOAD_DIR = Path("/data/chat-uploads")
try:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass  # Directory may be created later or already exist

# Internal path prefix → public chat-media URL
# Matches bare paths AND paths inside markdown ![]() or []() syntax
_DATA_PATH_RE = re.compile(r"/data/(?:chat-uploads|shared|screenshots?)/([^\s\)\]\"']+)")

# Also match the new /data/screenshots/ path (non-plural form was already in screenshots?)
# screenshots? already covers both "screenshot" and "screenshots"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
AUDIO_EXTS = {".mp3", ".ogg", ".wav", ".m4a", ".aac", ".flac"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}


def _media_type(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


def _templates(request: Request):
    return request.app.state.templates


def _rewrite_paths(text: str) -> str:
    """Replace /data/... file paths in agent response with /chat/media/... URLs.

    Handles three cases:
      1. Bare path: /data/shared/foo.png → ![foo.png](/chat/media/foo.png)
      2. Already in markdown image: ![alt](/data/shared/foo.png) → ![alt](/chat/media/foo.png)
      3. Already in markdown link: [text](/data/shared/foo.png) → [text](/chat/media/foo.png)
    """
    def replacer(m: re.Match) -> str:
        full = m.group(0)   # the full /data/... match
        fname = m.group(1)
        unsigned = f"/chat/media/{fname}"
        url = generate_signed_media_url(unsigned, settings.media_signing_secret)
        new_path = url

        # Check if this match is the href inside []() or ![]() — just replace the path
        start = m.start()
        if start > 0 and text[start - 1] == '(':
            return new_path

        # Bare path — promote to media embed
        ext = Path(fname).suffix.lower()
        if ext in IMAGE_EXTS:
            return f"![{fname}]({url})"
        if ext in AUDIO_EXTS:
            return f"[🔊 {fname}]({url})"
        if ext in VIDEO_EXTS:
            return f"[🎬 {fname}]({url})"
        return f"[📎 {fname}]({url})"

    return _DATA_PATH_RE.sub(replacer, text)


async def _save_attachment(att: dict) -> tuple[str | None, str | None]:
    """Save a base64 attachment to disk. Returns (saved_path, public_url)."""
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        name = att.get("name", "file")
        data_b64 = att.get("data", "")
        # Strip data URI prefix if present
        if "," in data_b64:
            data_b64 = data_b64.split(",", 1)[1]
        raw = base64.b64decode(data_b64)
        ext = Path(name).suffix or ".bin"
        filename = f"{uuid.uuid4().hex}{ext}"
        dest = UPLOAD_DIR / filename
        dest.write_bytes(raw)
        unsigned = f"/chat/media/{filename}"
        signed = generate_signed_media_url(unsigned, settings.media_signing_secret)
        return str(dest), signed
    except Exception as e:
        logger.warning("chat.attachment_save_failed", error=str(e))
        return None, None


@router.get("", response_class=HTMLResponse)
async def chat_page(request: Request):
    return _templates(request).TemplateResponse(request, "chat.html", {})


@router.get("/media/{file_path:path}")
async def serve_media(request: Request, file_path: str):
    """Serve agent-generated or user-uploaded media files.

    Requires a valid signed URL (?exp=...&sig=...) unless running in debug/
    transition mode (media_signing_debug=True in settings), which allows
    unsigned requests so existing bookmarks and pre-signed links keep working.
    """
    # Sanitize: no path traversal — only allow basename
    safe = Path(file_path).name
    if not safe or safe in (".", ".."):
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    # Signature validation
    exp = request.query_params.get("exp")
    sig = request.query_params.get("sig")

    if sig:
        # Signed request — validate it strictly
        canonical_path = f"/chat/media/{safe}"
        valid, reason = verify_media_url(canonical_path, exp, sig, settings.media_signing_secret)
        if not valid:
            logger.warning("media.signature_rejected", path=safe, reason=reason)
            return JSONResponse({"error": f"Access denied: {reason}"}, status_code=403)
    else:
        # Unsigned request — allow only in debug/transition mode
        if not settings.media_signing_debug:
            logger.warning("media.unsigned_rejected", path=safe)
            return JSONResponse({"error": "Access denied: unsigned request"}, status_code=403)
        # Debug mode: allow legacy unsigned access (transition period)

    # Search common data directories
    for base in [UPLOAD_DIR, Path("/data/screenshots"), Path("/data/shared"), Path("/data/screenshot")]:
        candidate = base / safe
        if candidate.exists() and candidate.is_file():
            return FileResponse(str(candidate), media_type=_media_type(safe))
    return JSONResponse({"error": "Not found"}, status_code=404)


@router.post("/send")
async def chat_send(request: Request):
    handler = getattr(request.app.state, "handler", None)
    if not handler:
        return JSONResponse({"ok": False, "error": "Agent not available"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "Message cannot be empty"}, status_code=400)
    if len(text) > 4000:
        return JSONResponse({"ok": False, "error": "Message too long (max 4000 chars)"}, status_code=400)

    try:
        response = await handler.chat_direct(text)
        response = _rewrite_paths(response)
        context: dict = {}
        try:
            mm = getattr(request.app.state, "model_manager", None)
            if mm:
                context["model"] = getattr(mm, "active_model", None)
                context["provider"] = getattr(mm, "active_provider", None)
        except Exception:
            pass
        return JSONResponse({"ok": True, "response": response, "context": context})
    except Exception as e:
        logger.exception("dashboard_chat.error")
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)


@router.post("/stream")
async def chat_stream(request: Request):
    """SSE endpoint — streams live thinking/skill progress while chat_direct runs."""
    handler = getattr(request.app.state, "handler", None)
    if not handler:
        return JSONResponse({"ok": False, "error": "Agent not available"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    text = (body.get("text") or "").strip()
    attachments = body.get("attachments") or []
    execution_mode = body.get("execution_mode", "fast")

    if not text and not attachments:
        return JSONResponse({"ok": False, "error": "Message cannot be empty"}, status_code=400)
    if len(text) > 4000:
        return JSONResponse({"ok": False, "error": "Message too long (max 4000 chars)"}, status_code=400)

    # Process attachments
    image_path: str | None = None
    attachment_notes: list[str] = []
    public_urls: list[dict] = []
    mm = getattr(request.app.state, "model_manager", None)

    for att in attachments:
        att_type = att.get("type", "")
        att_name = att.get("name", "file")
        saved_path, pub_url = await _save_attachment(att)
        if not saved_path:
            continue
        ext = Path(att_name).suffix.lower()
        public_urls.append({"name": att_name, "url": pub_url, "type": att_type})
        if ext in IMAGE_EXTS and image_path is None:
            image_path = saved_path  # first image → vision
            attachment_notes.append(f"[IMAGE ATTACHED: {att_name}] — You CAN see this image. Describe exactly what you see in it, then answer the user's question.")
        elif ext in AUDIO_EXTS:
            # Try to transcribe audio with Whisper
            transcription = ""
            if mm and hasattr(mm, "transcribe_audio"):
                try:
                    transcription = await mm.transcribe_audio(saved_path) or ""
                except Exception as _e:
                    logger.warning("chat.audio_transcription_failed", error=str(_e))
            if transcription:
                attachment_notes.append(f"[Audio transcribed — {att_name}]: {transcription}")
            else:
                attachment_notes.append(f"[Audio attached: {att_name}]({pub_url})")
        elif ext in VIDEO_EXTS:
            # Extract first frame for vision analysis
            frame_path = None
            if image_path is None:
                try:
                    import subprocess, tempfile
                    frame_file = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False,
                                                             dir=str(UPLOAD_DIR))
                    frame_file.close()
                    result = await asyncio.to_thread(
                        subprocess.run,
                        ["ffmpeg", "-y", "-i", saved_path, "-vf", "select=eq(n\\,0)",
                         "-frames:v", "1", "-q:v", "2", frame_file.name],
                        capture_output=True, timeout=30,
                    )
                    if result.returncode == 0 and Path(frame_file.name).stat().st_size > 0:
                        frame_path = frame_file.name
                        image_path = frame_path
                        attachment_notes.append(f"[VIDEO FRAME — {att_name}] — You CAN see this video frame. Describe what you see and answer the user's question.")
                    else:
                        attachment_notes.append(f"[Video attached: {att_name}]({pub_url})")
                except Exception as _e:
                    logger.warning("chat.video_frame_failed", error=str(_e))
                    attachment_notes.append(f"[Video attached: {att_name}]({pub_url})")
            else:
                attachment_notes.append(f"[Video attached: {att_name}]({pub_url})")
        else:
            # Try to extract text from plain text / PDF / doc files
            try:
                content = Path(saved_path).read_bytes()
                if att_type.startswith("text/") or ext in {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml"}:
                    attachment_notes.append(f"[File: {att_name}]\n{content.decode('utf-8', errors='replace')[:3000]}")
                else:
                    attachment_notes.append(f"[Document attached: {att_name}]({pub_url})")
            except Exception:
                attachment_notes.append(f"[Document attached: {att_name}]({pub_url})")

    # Build final text passed to agent
    full_text = text
    if attachment_notes:
        full_text = text + "\n\n" + "\n\n".join(attachment_notes) if text else "\n\n".join(attachment_notes)
    if not full_text and image_path:
        full_text = "Describe everything you see in this image in detail."

    # ── Request correlation ID ────────────────────────────────────────────────
    # Accept from client or generate here. Propagated into every SSE event
    # and persisted to audit_log so traces can be bound exactly.
    request_id: str = (body.get("request_id") or "").strip()
    if not request_id:
        request_id = "req_" + uuid.uuid4().hex[:16]

    queue: asyncio.Queue = asyncio.Queue()

    # Wrap progress_callback to inject request_id into every intermediate event.
    def _enriched_cb(event: dict) -> None:
        if isinstance(event, dict):
            event["request_id"] = request_id
        queue.put_nowait(event)

    from datetime import datetime, timezone as _tz
    _req_start = datetime.now(_tz.utc)

    async def run_chat():
        try:
            response = await handler.chat_direct(
                full_text,
                progress_callback=_enriched_cb,
                image_path=image_path,
                execution_mode=execution_mode,
            )
            response = _rewrite_paths(response)
            context: dict = {}
            try:
                mm = getattr(request.app.state, "model_manager", None)
                if mm:
                    context["model"] = getattr(mm, "active_model", None)
                    context["provider"] = getattr(mm, "active_provider", None)
            except Exception:
                pass
            queue.put_nowait({
                "type": "done", "ok": True, "response": response,
                "context": context, "media": public_urls,
                "request_id": request_id,
            })

            # ── Tag audit_log entry with request_id ───────────────────────────
            # The most recent dashboard.chat/dashboard.message entry was just
            # written by store_episodic() — retroactively add request_id to its
            # metadata_json so the grounding endpoint can do exact correlation.
            try:
                from ...db.models import AuditLog
                from ...db.session import async_session
                from sqlalchemy import select, desc as _desc, update as _update
                async with async_session() as _s:
                    _row = await _s.execute(
                        select(AuditLog.id, AuditLog.metadata_json)
                        .where(AuditLog.timestamp >= _req_start)
                        .where(AuditLog.source == "dashboard")
                        .order_by(_desc(AuditLog.timestamp))
                        .limit(1)
                    )
                    _entry = _row.first()
                    if _entry:
                        _eid, _meta = _entry
                        _meta = dict(_meta) if isinstance(_meta, dict) else {}
                        _meta["request_id"] = request_id
                        await _s.execute(
                            _update(AuditLog)
                            .where(AuditLog.id == _eid)
                            .values(metadata_json=_meta)
                        )
                        await _s.commit()
            except Exception:
                pass  # fail-safe: audit tag is best-effort

        except Exception as e:
            logger.exception("dashboard_chat_stream.error")
            queue.put_nowait({"type": "error", "ok": False, "error": str(e).splitlines()[0][:120],
                              "request_id": request_id})

    asyncio.create_task(run_chat())

    async def generator():
        yield 'data: {"type":"start"}\n\n'
        deadline = asyncio.get_event_loop().time() + 300  # 5-minute hard cap
        while True:
            if asyncio.get_event_loop().time() > deadline:
                yield 'data: {"type":"error","ok":false,"error":"Timeout"}\n\n'
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=5.0)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/media-sign")
async def sign_media_path(request: Request, path: str = ""):
    """Return a signed URL for a bare /chat/media/... path.

    Used by the JS fallback in chat.html to sign paths that were not
    rewritten server-side. Requires an authenticated session (auth
    middleware ensures this since /chat/media-sign is not in UNPROTECTED).
    """
    if not path or not path.startswith("/chat/media/"):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    # Sanitize: only allow the basename portion
    safe = Path(path.removeprefix("/chat/media/")).name
    if not safe or safe in (".", ".."):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    canonical = f"/chat/media/{safe}"
    signed = generate_signed_media_url(canonical, settings.media_signing_secret)
    return JSONResponse({"ok": True, "url": signed})


@router.post("/classify")
async def classify_strategy(request: Request):
    """Decision Layer diagnostic — classify a message without executing it."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text required"}, status_code=400)

    try:
        from ...decision_layer import decide_execution_strategy, explain_strategy
        explanation = explain_strategy(text)
        return JSONResponse({"ok": True, **explanation})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)


@router.get("/api/last-grounding")
async def last_grounding(request: Request, request_id: str = ""):
    """Return grounding validation result from the most recent dashboard message.

    With request_id: exact lookup via metadata_json filter (precise correlation).
    Without request_id: fallback to most recent source='dashboard' within 5 min.
    Fail-open → returns null result.
    """
    from datetime import datetime, timedelta, timezone
    try:
        from ...db.models import AuditLog
        from ...db.session import async_session
        from sqlalchemy import select, desc as _desc

        async with async_session() as session:
            if request_id:
                # ── Exact correlation via request_id in metadata_json ──────────
                try:
                    row = await session.execute(
                        select(AuditLog.metadata_json, AuditLog.timestamp, AuditLog.latency_ms, AuditLog.error)
                        .where(AuditLog.source == "dashboard")
                        .where(AuditLog.metadata_json["request_id"].astext == request_id)
                        .order_by(_desc(AuditLog.timestamp))
                        .limit(1)
                    )
                    entry = row.first()
                except Exception:
                    entry = None  # JSONB operator not available — fall through

                if not entry:
                    # Fallback: proximity with short window (30s)
                    cutoff = datetime.now(timezone.utc) - timedelta(seconds=30)
                    row = await session.execute(
                        select(AuditLog.metadata_json, AuditLog.timestamp, AuditLog.latency_ms, AuditLog.error)
                        .where(AuditLog.source == "dashboard")
                        .where(AuditLog.timestamp >= cutoff)
                        .order_by(_desc(AuditLog.timestamp))
                        .limit(1)
                    )
                    entry = row.first()
            else:
                # ── Proximity fallback (original behavior) ─────────────────────
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
                row = await session.execute(
                    select(AuditLog.metadata_json, AuditLog.timestamp, AuditLog.latency_ms, AuditLog.error)
                    .where(AuditLog.source == "dashboard")
                    .where(AuditLog.timestamp >= cutoff)
                    .order_by(_desc(AuditLog.timestamp))
                    .limit(1)
                )
                entry = row.first()

            if not entry:
                return JSONResponse({"ok": True, "grounding": None})

            meta, ts, latency_ms, error = entry
            grounding = None
            if meta and isinstance(meta, dict):
                grounding = meta.get("grounding")
            elif meta and isinstance(meta, str):
                import json as _json
                try:
                    parsed = _json.loads(meta)
                    grounding = parsed.get("grounding")
                except Exception:
                    pass

            return JSONResponse({
                "ok": True,
                "grounding": grounding,
                "ts": ts.strftime("%H:%M:%S") if ts else None,
                "latency_ms": latency_ms,
                "error": str(error)[:80] if error else None,
            })
    except Exception:
        return JSONResponse({"ok": True, "grounding": None})
