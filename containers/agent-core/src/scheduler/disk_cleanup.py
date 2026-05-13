"""Disk cleanup jobs — prevent unbounded growth of browser sessions and screenshots.

Two jobs:
  BrowserSessionCleanupJob  — runs weekly, deletes Chromium profile dirs not accessed in 30 days
  ScreenshotCleanupJob      — runs daily, deletes screenshot files older than 7 days

Both respect a configurable max-size safety cap that triggers emergency cleanup when breached.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import structlog

logger = structlog.get_logger()

BROWSER_SESSIONS_DIR = Path("/data/browser_sessions")
SCREENSHOTS_DIR = Path("/data/screenshots")
BACKUPS_DIR = Path("/data/backups")

# Safety caps — emergency cleanup triggers when total size exceeds these thresholds
BROWSER_SESSIONS_MAX_GB: float = 20.0
SCREENSHOTS_MAX_GB: float = 2.0

# Backup rotation
BACKUPS_KEEP: int = 30

# Redis stream trim caps (events grew unbounded in audit — see release-prep notes)
_STREAM_TRIM_CAPS: dict[str, int] = {
    "events:outgoing": 1000,
    "events:incoming": 500,
}


def _rotate_backups(backups_dir: Path = BACKUPS_DIR, keep: int = BACKUPS_KEEP) -> tuple[int, float]:
    """Delete oldest *.tar.gz backup archives, keeping the newest ``keep``.

    Returns (deleted_count, freed_mb). Best-effort; logs warnings but never
    raises into the caller.
    """
    if not backups_dir.is_dir():
        return (0, 0.0)
    try:
        archives = sorted(
            (p for p in backups_dir.iterdir() if p.is_file() and p.name.endswith(".tar.gz")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return (0, 0.0)
    if len(archives) <= keep:
        return (0, 0.0)
    deleted = 0
    freed_mb = 0.0
    for old in archives[keep:]:
        try:
            sz = old.stat().st_size
            old.unlink()
            deleted += 1
            freed_mb += sz / (1024 * 1024)
            logger.info("backup_rotation.deleted", file=old.name, size_mb=round(sz / (1024 * 1024), 1))
        except Exception as exc:
            logger.warning("backup_rotation.error", file=old.name, error=str(exc)[:80])
    return (deleted, freed_mb)


async def _trim_redis_streams(redis_url: str, caps: dict[str, int] = _STREAM_TRIM_CAPS) -> dict[str, int]:
    """XTRIM each event stream to its MAXLEN cap. Returns {stream: trimmed_count}.

    Best-effort; failures are logged but never raised.
    """
    if not redis_url:
        return {}
    trimmed: dict[str, int] = {}
    try:
        import redis.asyncio as aioredis  # type: ignore
    except Exception:
        return {}
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
    except Exception as exc:
        logger.warning("stream_trim.connect_failed", error=str(exc)[:80])
        return {}
    try:
        for stream, maxlen in caps.items():
            try:
                # approximate=True uses ~ for cheap radix-tree-bound trim.
                count = await r.xtrim(stream, maxlen=maxlen, approximate=True)
                trimmed[stream] = int(count or 0)
                logger.info("stream_trim.done", stream=stream, maxlen=maxlen, trimmed=trimmed[stream])
            except Exception as exc:
                logger.warning("stream_trim.error", stream=stream, error=str(exc)[:80])
    finally:
        try:
            await r.aclose()
        except Exception:
            pass
    return trimmed


def _dir_size_gb(path: Path) -> float:
    """Return total size of a directory tree in GB."""
    try:
        total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        return total / (1024 ** 3)
    except Exception:
        return 0.0


def _file_size_gb(path: Path) -> float:
    """Return total size of files in a flat directory in GB."""
    try:
        total = sum(
            p.stat().st_size for p in path.iterdir() if p.is_file()
        )
        return total / (1024 ** 3)
    except Exception:
        return 0.0


class BrowserSessionCleanupJob:
    """Weekly cleanup of stale Chromium profile directories.

    Deletes session directories whose last modification time is older than
    max_age_days. Respects a maximum total-size cap: if exceeded, even recently
    used sessions are pruned (oldest first) until under cap.
    """

    def __init__(
        self,
        sessions_dir: Path = BROWSER_SESSIONS_DIR,
        max_age_days: int = 30,
        max_size_gb: float = BROWSER_SESSIONS_MAX_GB,
    ):
        self.sessions_dir = sessions_dir
        self.max_age_days = max_age_days
        self.max_size_gb = max_size_gb

    async def __call__(self) -> str:
        if not self.sessions_dir.exists():
            return "browser_session_cleanup: sessions dir not found"

        cutoff = time.time() - (self.max_age_days * 86400)
        deleted = 0
        freed_mb = 0.0
        errors = 0

        # --- Phase 1: age-based cleanup ---
        entries = sorted(
            (e for e in self.sessions_dir.iterdir() if e.is_dir()),
            key=lambda e: e.stat().st_mtime,
        )
        for entry in entries:
            try:
                mtime = entry.stat().st_mtime
                if mtime < cutoff:
                    size_mb = _dir_size_gb(entry) * 1024
                    shutil.rmtree(entry, ignore_errors=True)
                    if not entry.exists():
                        deleted += 1
                        freed_mb += size_mb
                        logger.info(
                            "browser_session_cleanup.deleted",
                            session=entry.name,
                            age_days=int((time.time() - mtime) / 86400),
                            size_mb=round(size_mb, 1),
                        )
            except Exception as exc:
                errors += 1
                logger.warning("browser_session_cleanup.error", session=entry.name, error=str(exc)[:80])

        # --- Phase 2: safety-cap emergency cleanup ---
        current_gb = _dir_size_gb(self.sessions_dir)
        if current_gb > self.max_size_gb:
            logger.warning(
                "browser_session_cleanup.cap_exceeded",
                current_gb=round(current_gb, 2),
                cap_gb=self.max_size_gb,
            )
            # Prune oldest sessions until under cap
            remaining = sorted(
                (e for e in self.sessions_dir.iterdir() if e.is_dir()),
                key=lambda e: e.stat().st_mtime,
            )
            for entry in remaining:
                if _dir_size_gb(self.sessions_dir) <= self.max_size_gb * 0.8:
                    break
                try:
                    size_mb = _dir_size_gb(entry) * 1024
                    shutil.rmtree(entry, ignore_errors=True)
                    if not entry.exists():
                        deleted += 1
                        freed_mb += size_mb
                        logger.info(
                            "browser_session_cleanup.emergency_deleted",
                            session=entry.name,
                            size_mb=round(size_mb, 1),
                        )
                except Exception as exc:
                    errors += 1
                    logger.warning("browser_session_cleanup.emergency_error", error=str(exc)[:80])

        final_gb = _dir_size_gb(self.sessions_dir)
        result = (
            f"browser_session_cleanup: deleted={deleted} freed={freed_mb:.0f}MB "
            f"remaining={final_gb:.2f}GB errors={errors}"
        )
        logger.info("browser_session_cleanup.done", deleted=deleted, freed_mb=round(freed_mb), final_gb=round(final_gb, 2))
        return result


class ScreenshotCleanupJob:
    """Daily cleanup of old screenshot files.

    Deletes screenshot PNG/JPG files in SCREENSHOTS_DIR older than max_age_days.
    Triggers emergency cleanup (keep only newest max_keep files) when size cap breached.
    """

    def __init__(
        self,
        screenshots_dir: Path = SCREENSHOTS_DIR,
        max_age_days: int = 7,
        max_size_gb: float = SCREENSHOTS_MAX_GB,
        max_keep: int = 500,
        redis_url: str = "",
        backups_dir: Path = BACKUPS_DIR,
        backups_keep: int = BACKUPS_KEEP,
    ):
        self.screenshots_dir = screenshots_dir
        self.max_age_days = max_age_days
        self.max_size_gb = max_size_gb
        self.max_keep = max_keep
        # Daily piggyback chores
        self.redis_url = redis_url
        self.backups_dir = backups_dir
        self.backups_keep = backups_keep

    async def __call__(self) -> str:
        # Always run the piggyback chores (backup rotation + Redis trim) even
        # if the screenshots dir is missing — they're cheap and independent.
        rot_deleted, rot_freed_mb = _rotate_backups(self.backups_dir, self.backups_keep)
        stream_trim = await _trim_redis_streams(self.redis_url)
        chore_summary = ""
        if rot_deleted:
            chore_summary += f" backups_pruned={rot_deleted}(-{rot_freed_mb:.0f}MB)"
        if stream_trim:
            chore_summary += " streams=" + ",".join(f"{k}:{v}" for k, v in stream_trim.items())

        if not self.screenshots_dir.exists():
            return f"screenshot_cleanup: dir not found{chore_summary}"

        cutoff = time.time() - (self.max_age_days * 86400)
        deleted = 0
        freed_mb = 0.0
        errors = 0

        # --- Phase 1: age-based cleanup ---
        files = [
            p for p in self.screenshots_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ]
        for f in files:
            try:
                if f.stat().st_mtime < cutoff:
                    size_bytes = f.stat().st_size
                    f.unlink()
                    deleted += 1
                    freed_mb += size_bytes / (1024 * 1024)
            except Exception as exc:
                errors += 1
                logger.warning("screenshot_cleanup.error", file=f.name, error=str(exc)[:80])

        # --- Phase 2: safety-cap emergency cleanup ---
        current_gb = _file_size_gb(self.screenshots_dir)
        if current_gb > self.max_size_gb:
            logger.warning(
                "screenshot_cleanup.cap_exceeded",
                current_gb=round(current_gb, 2),
                cap_gb=self.max_size_gb,
            )
            remaining = sorted(
                (p for p in self.screenshots_dir.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
            while len(remaining) > self.max_keep and remaining:
                f = remaining.pop(0)
                try:
                    size_bytes = f.stat().st_size
                    f.unlink()
                    deleted += 1
                    freed_mb += size_bytes / (1024 * 1024)
                except Exception as exc:
                    errors += 1
                    logger.warning("screenshot_cleanup.emergency_error", error=str(exc)[:80])

        final_count = sum(1 for p in self.screenshots_dir.iterdir() if p.is_file())
        result = (
            f"screenshot_cleanup: deleted={deleted} freed={freed_mb:.0f}MB "
            f"remaining={final_count} errors={errors}{chore_summary}"
        )
        logger.info(
            "screenshot_cleanup.done",
            deleted=deleted,
            freed_mb=round(freed_mb),
            remaining=final_count,
            backups_pruned=rot_deleted,
            streams_trimmed=stream_trim,
        )
        return result


class DiskMonitorJob:
    """Checks disk usage every 30 minutes and sends Telegram alerts at thresholds.

    Soft threshold (80%): warning notification, rate-limited to once per 24h.
    Hard threshold (90%): critical notification, rate-limited to once per 6h.
    Uses /data partition if available, falls back to /.
    """

    SOFT_THRESHOLD = 80.0
    HARD_THRESHOLD = 90.0
    _KEY_SOFT = "disk:warned_80"
    _KEY_HARD = "disk:warned_90"
    _SOFT_TTL = 86400   # 24h
    _HARD_TTL = 21600   # 6h

    def __init__(self, redis_url: str = "", bus=None, notify_chat_id: str = "") -> None:
        self.redis_url = redis_url
        self.bus = bus
        self.notify_chat_id = notify_chat_id

    async def __call__(self) -> str:
        try:
            return await self._run()
        except Exception:
            logger.exception("disk_monitor.error")
            return "disk_monitor: error"

    async def _run(self) -> str:
        try:
            usage = shutil.disk_usage("/data")
        except Exception:
            usage = shutil.disk_usage("/")
        used_pct = (usage.used / usage.total) * 100
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        logger.info("disk_monitor.check",
                    used_pct=round(used_pct, 1),
                    free_gb=round(free_gb, 1),
                    total_gb=round(total_gb, 1))
        if used_pct >= self.HARD_THRESHOLD:
            await self._maybe_alert(
                self._KEY_HARD, self._HARD_TTL,
                f"DISK CRITICAL: {used_pct:.1f}% used ({free_gb:.1f}GB free / {total_gb:.0f}GB). Immediate cleanup required.",
            )
        elif used_pct >= self.SOFT_THRESHOLD:
            await self._maybe_alert(
                self._KEY_SOFT, self._SOFT_TTL,
                f"Disk warning: {used_pct:.1f}% used ({free_gb:.1f}GB free / {total_gb:.0f}GB). Consider cleanup.",
            )

        # Proactive browser-sessions cleanup: when the sessions dir crosses
        # 12GB (60% of the 20GB hard cap), trigger an immediate evict pass on
        # entries unused >7d.  This rides on the existing 30-min disk_monitor
        # tick so no new job is added — just earlier reaction.
        try:
            sessions_gb = _dir_size_gb(BROWSER_SESSIONS_DIR)
            if sessions_gb >= 12.0:
                logger.warning(
                    "disk_monitor.browser_sessions_high",
                    size_gb=round(sessions_gb, 1),
                )
                await self._evict_old_browser_sessions(max_age_days=7)
        except Exception:
            pass

        return f"disk_monitor: {used_pct:.1f}% used, {free_gb:.1f}GB free"

    async def _evict_old_browser_sessions(self, max_age_days: int = 7) -> None:
        """Delete Chromium profile directories not modified within max_age_days.

        Reuses the same eviction shape as BrowserSessionCleanupJob but at a
        tighter age threshold (7d vs 30d default) so we react before the
        weekly cleanup tick.  Best-effort.
        """
        if not BROWSER_SESSIONS_DIR.is_dir():
            return
        cutoff = time.time() - (max_age_days * 86400)
        evicted = 0
        for entry in BROWSER_SESSIONS_DIR.iterdir():
            if not entry.is_dir():
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    evicted += 1
            except Exception:
                continue
        if evicted:
            logger.info(
                "disk_monitor.browser_sessions_evicted",
                evicted=evicted,
                cutoff_days=max_age_days,
            )

    async def _maybe_alert(self, redis_key: str, ttl: int, message: str) -> None:
        if not self.bus or not self.notify_chat_id:
            return
        if self.redis_url:
            try:
                import redis.asyncio as aioredis
                _r = aioredis.from_url(self.redis_url, decode_responses=True)
                try:
                    if await _r.get(redis_key):
                        return
                    await _r.setex(redis_key, ttl, "1")
                finally:
                    await _r.aclose()
            except Exception:
                pass
        try:
            from ..utils.safe_notify import safe_notify
            await safe_notify(
                self.bus,
                str(self.notify_chat_id),
                message,
                source="disk_cleanup",
            )
            logger.warning("disk_monitor.alert_sent", message=message[:80])
        except Exception:
            pass
