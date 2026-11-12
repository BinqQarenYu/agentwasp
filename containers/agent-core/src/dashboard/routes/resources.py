"""Resource Management dashboard route — CPU, RAM, and SSD/Disk."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import psutil
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()
logger = structlog.get_logger()

# Redis keys for persisted thresholds
_THRESHOLDS_KEY = "resources:thresholds"

# Default alert thresholds (percent)
_DEFAULTS: dict[str, dict[str, int]] = {
    "cpu":  {"warn": 70, "crit": 90},
    "ram":  {"warn": 75, "crit": 90},
    "disk": {"warn": 80, "crit": 90},
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_bytes(n: int, unit: str = "auto") -> str:
    """Human-readable byte size."""
    if unit == "GB" or (unit == "auto" and n >= 1024 ** 3):
        return f"{n / (1024**3):.2f} GB"
    if unit == "MB" or (unit == "auto" and n >= 1024 ** 2):
        return f"{n / (1024**2):.1f} MB"
    return f"{n / 1024:.1f} KB"


def _collect_full() -> dict[str, Any]:
    """Collect rich psutil resource metrics (runs in thread)."""
    # ── CPU ───────────────────────────────────────────────────────────────────
    cpu_pct_all = psutil.cpu_percent(interval=0.5, percpu=True)
    cpu_pct     = round(sum(cpu_pct_all) / len(cpu_pct_all), 1) if cpu_pct_all else 0.0
    cpu_times   = psutil.cpu_times_percent(interval=None)
    cpu_freq    = psutil.cpu_freq()
    cpu_load    = psutil.getloadavg() if hasattr(psutil, "getloadavg") else (0, 0, 0)
    cores_phys  = psutil.cpu_count(logical=False) or 1
    cores_logic = psutil.cpu_count(logical=True)  or 1

    cpu: dict[str, Any] = {
        "percent":       cpu_pct,
        "per_core":      [round(p, 1) for p in cpu_pct_all],
        "cores_physical": cores_phys,
        "cores_logical":  cores_logic,
        "user_pct":      round(cpu_times.user, 1) if cpu_times else 0,
        "system_pct":    round(cpu_times.system, 1) if cpu_times else 0,
        "idle_pct":      round(cpu_times.idle, 1) if cpu_times else 0,
        "freq_mhz":      round(cpu_freq.current) if cpu_freq else None,
        "freq_max_mhz":  round(cpu_freq.max) if cpu_freq else None,
        "load_1m":       round(cpu_load[0], 2),
        "load_5m":       round(cpu_load[1], 2),
        "load_15m":      round(cpu_load[2], 2),
    }

    # ── RAM ───────────────────────────────────────────────────────────────────
    mem  = psutil.virtual_memory()
    swap = psutil.swap_memory()

    ram: dict[str, Any] = {
        "percent":      mem.percent,
        "used_mb":      round(mem.used   / (1024 ** 2)),
        "total_mb":     round(mem.total  / (1024 ** 2)),
        "free_mb":      round(mem.free   / (1024 ** 2)),
        "available_mb": round(mem.available / (1024 ** 2)),
        "cached_mb":    round(getattr(mem, "cached", 0) / (1024 ** 2)),
        "buffers_mb":   round(getattr(mem, "buffers", 0) / (1024 ** 2)),
        "used_gb":      round(mem.used   / (1024 ** 3), 2),
        "total_gb":     round(mem.total  / (1024 ** 3), 2),
        "swap_percent": swap.percent,
        "swap_used_mb": round(swap.used  / (1024 ** 2)),
        "swap_total_mb":round(swap.total / (1024 ** 2)),
    }

    # ── Disk / SSD ────────────────────────────────────────────────────────────
    # Collect all mounted partitions (skip non-physical on Linux)
    partitions = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            partitions.append({
                "device":     part.device,
                "mountpoint": part.mountpoint,
                "fstype":     part.fstype,
                "opts":       part.opts,
                "total_gb":   round(usage.total / (1024 ** 3), 2),
                "used_gb":    round(usage.used  / (1024 ** 3), 2),
                "free_gb":    round(usage.free  / (1024 ** 3), 2),
                "percent":    usage.percent,
            })
        except (PermissionError, OSError):
            pass

    # Aggregate across all partitions for the "primary" disk view
    total_disk = sum(p["total_gb"] for p in partitions)
    used_disk  = sum(p["used_gb"]  for p in partitions)
    pct_disk   = round(used_disk / total_disk * 100, 1) if total_disk else 0.0

    # Disk I/O counters (best-effort)
    disk_io: dict[str, Any] = {}
    try:
        io = psutil.disk_io_counters()
        if io:
            disk_io = {
                "read_mb":   round(io.read_bytes  / (1024 ** 2), 1),
                "write_mb":  round(io.write_bytes / (1024 ** 2), 1),
                "read_count":  io.read_count,
                "write_count": io.write_count,
            }
    except Exception:
        pass

    disk: dict[str, Any] = {
        "percent":    pct_disk,
        "used_gb":    round(used_disk,  2),
        "total_gb":   round(total_disk, 2),
        "free_gb":    round(total_disk - used_disk, 2),
        "partitions": partitions,
        "io":         disk_io,
    }

    # ── Top processes (by CPU + MEM) ──────────────────────────────────────────
    top_cpu: list[dict] = []
    top_mem: list[dict] = []
    try:
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
            try:
                info = p.info
                procs.append({
                    "pid":     info["pid"],
                    "name":    (info["name"] or "")[:30],
                    "cpu":     round(info["cpu_percent"] or 0, 1),
                    "mem":     round(info["memory_percent"] or 0, 2),
                    "status":  info["status"] or "",
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        top_cpu = sorted(procs, key=lambda x: x["cpu"], reverse=True)[:8]
        top_mem = sorted(procs, key=lambda x: x["mem"], reverse=True)[:8]
    except Exception:
        pass

    return {
        "cpu":     cpu,
        "ram":     ram,
        "disk":    disk,
        "top_cpu": top_cpu,
        "top_mem": top_mem,
    }


async def _load_thresholds(redis_url: str) -> dict:
    """Load custom thresholds from Redis; fall back to defaults."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2)
        try:
            raw = await r.get(_THRESHOLDS_KEY)
            if raw:
                stored = json.loads(raw)
                # Merge with defaults so missing keys are filled in
                result = {}
                for k, dv in _DEFAULTS.items():
                    result[k] = {**dv, **stored.get(k, {})}
                return result
        finally:
            await r.aclose()
    except Exception:
        pass
    return {k: dict(v) for k, v in _DEFAULTS.items()}


async def _save_thresholds(redis_url: str, thresholds: dict) -> bool:
    """Persist thresholds to Redis."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2)
        try:
            await r.set(_THRESHOLDS_KEY, json.dumps(thresholds))
            return True
        finally:
            await r.aclose()
    except Exception as exc:
        logger.warning("resources.save_thresholds_failed", error=str(exc)[:120])
        return False


# ── Page route ────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def resources_page(request: Request):
    """Full resource management page (SSR snapshot + live JS polling)."""
    metrics: dict = {}
    thresholds: dict = {}
    err: str | None = None

    try:
        metrics, thresholds = await asyncio.gather(
            asyncio.to_thread(_collect_full),
            _load_thresholds(request.app.state.redis_url),
        )
    except Exception as exc:
        logger.exception("resources_page.error")
        err = str(exc)[:200]

    return request.app.state.templates.TemplateResponse(
        request,
        "resources.html",
        {
            "metrics":    metrics,
            "thresholds": thresholds,
            "error":      err,
        },
    )


# ── JSON live-snapshot ─────────────────────────────────────────────────────────

@router.get("/api/snapshot")
async def resources_snapshot(request: Request):
    """Live JSON snapshot of CPU / RAM / Disk — polled by the UI every 5 s."""
    try:
        metrics, thresholds = await asyncio.gather(
            asyncio.to_thread(_collect_full),
            _load_thresholds(request.app.state.redis_url),
        )
        return JSONResponse({"ok": True, "metrics": metrics, "thresholds": thresholds})
    except Exception as exc:
        logger.exception("resources_snapshot.error")
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=500)


# ── Threshold management ───────────────────────────────────────────────────────

@router.get("/api/thresholds")
async def get_thresholds(request: Request):
    """Return the current alert thresholds."""
    thresholds = await _load_thresholds(request.app.state.redis_url)
    return JSONResponse({"ok": True, "thresholds": thresholds, "defaults": _DEFAULTS})


@router.post("/api/thresholds")
async def set_thresholds(request: Request):
    """Update alert thresholds. Body: {cpu:{warn,crit}, ram:{warn,crit}, disk:{warn,crit}}."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    current = await _load_thresholds(request.app.state.redis_url)

    for resource in ("cpu", "ram", "disk"):
        if resource in body:
            patch = body[resource]
            for key in ("warn", "crit"):
                if key in patch:
                    val = int(patch[key])
                    if not (0 <= val <= 100):
                        return JSONResponse(
                            {"ok": False, "error": f"{resource}.{key} must be 0-100"},
                            status_code=422,
                        )
                    current[resource][key] = val

    # Validate warn < crit
    for resource in ("cpu", "ram", "disk"):
        t = current[resource]
        if t["warn"] >= t["crit"]:
            return JSONResponse(
                {"ok": False, "error": f"{resource}: warn ({t['warn']}) must be < crit ({t['crit']})"},
                status_code=422,
            )

    saved = await _save_thresholds(request.app.state.redis_url, current)
    if not saved:
        return JSONResponse({"ok": False, "error": "Failed to persist thresholds"}, status_code=500)

    logger.info("resources.thresholds_updated", thresholds=current)
    return JSONResponse({"ok": True, "thresholds": current})


@router.post("/api/thresholds/reset")
async def reset_thresholds(request: Request):
    """Reset all thresholds to system defaults."""
    saved = await _save_thresholds(request.app.state.redis_url, {k: dict(v) for k, v in _DEFAULTS.items()})
    return JSONResponse({"ok": saved, "thresholds": {k: dict(v) for k, v in _DEFAULTS.items()}})
