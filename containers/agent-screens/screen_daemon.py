"""Isolated Per-Bot Virtual Screen Daemon (`agent-screens` container).

Manages on-demand Linux virtual displays (`Xvfb` + `dbus-daemon` + `openbox`) with
persistent per-bot Chrome profiles and live browser streaming (`voxl` preferred,
automatic `x11vnc` + `websockify`/noVNC fallback).

Security & resource guarantees:
- Strict slot cap (`MAX_SCREENS`, default 2) with LRU eviction of idle screens.
- Automatic idle reaping (`SCREEN_IDLE_MINUTES`, default 15).
- Zero access to host secrets, Redis, Postgres, or Agent WASP Vault.
- Authenticated internal HTTP API (`X-Screens-Token` when `SCREENS_INTERNAL_TOKEN` is set).
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def safe_bot_id(bot_id: str) -> str:
    cleaned = _SAFE_ID_RE.sub("_", (bot_id or "").strip()).strip("._")
    return cleaned or "default-bot"


def browser_session_name(bot_id: str) -> str:
    return f"wasp-{safe_bot_id(bot_id)}"


@dataclass
class ScreenSlot:
    bot_id: str
    slot: int
    display_num: int
    port: int
    token: str
    backend: str  # "voxl" | "novnc" | "headless-xvfb"
    profile_dir: Path
    bus_address: str
    last_used: float = field(default_factory=time.monotonic)
    busy: bool = False
    procs: list[Any] = field(default_factory=list, repr=False)

    @property
    def display(self) -> str:
        return f":{self.display_num}"

    def info_dict(self, public_base: str = "") -> dict[str, Any]:
        base = public_base.rstrip("/")
        if self.backend == "voxl":
            view_path = f"/screen/{self.port}/?token={self.token}"
        elif self.backend == "novnc":
            view_path = f"/screen/{self.port}/vnc.html?autoconnect=true&password={self.token}"
        else:
            view_path = ""
        return {
            "bot_id": self.bot_id,
            "slot": self.slot,
            "display": self.display,
            "bus": self.bus_address,
            "port": self.port,
            "backend": self.backend,
            "profile_dir": str(self.profile_dir),
            "browser_session": browser_session_name(self.bot_id),
            "view_url": f"{base}{view_path}" if view_path else "",
            "busy": self.busy,
            "idle_seconds": round(max(0.0, time.monotonic() - self.last_used), 1),
        }


ProcessStarter = Callable[[list[str], dict[str, str]], Any]


def _default_spawn(cmd: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
    merged = os.environ.copy()
    merged.update(env)
    return subprocess.Popen(
        cmd,
        env=merged,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class ScreenManager:
    """Manages bounded per-bot Xvfb + live viewer slots with LRU eviction and idle reaping."""

    def __init__(
        self,
        *,
        profiles_root: Path | None = None,
        run_dir: Path | None = None,
        max_screens: int = 2,
        screen_base: int = 100,
        screen_size: str = "1280x800",
        idle_minutes: float = 15.0,
        public_base: str = "",
        spawner: ProcessStarter | None = None,
        which_fn: Callable[[str], str | None] | None = None,
        wait_for_display: bool = True,
    ) -> None:
        self.profiles_root = Path(profiles_root or os.environ.get("PROFILES_DIR", "/data/profiles"))
        self.run_dir = Path(run_dir or os.environ.get("SCREEN_RUN_DIR", "/tmp/wasp-screens"))
        self.max_screens = max(1, int(max_screens))
        self.screen_base = int(screen_base)
        self.screen_size = screen_size
        self.idle_seconds = max(5.0, float(idle_minutes) * 60.0)
        self.public_base = public_base
        self._spawn = spawner or _default_spawn
        self._which = which_fn or shutil.which
        self._wait_for_display = wait_for_display

        self._lock = threading.Lock()
        self._slots: dict[str, ScreenSlot] = {}

    def _is_alive(self, slot: ScreenSlot) -> bool:
        if not slot.procs:
            return True
        for p in slot.procs:
            poll = getattr(p, "poll", None)
            if callable(poll) and poll() is not None:
                return False
        return True

    def _stop_slot(self, slot: ScreenSlot) -> None:
        if self._which("agent-browser"):
            try:
                subprocess.run(
                    ["agent-browser", "--session", browser_session_name(slot.bot_id), "close"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                    check=False,
                )
            except Exception:
                pass
        for p in reversed(slot.procs):
            try:
                terminate = getattr(p, "terminate", None)
                if callable(terminate):
                    terminate()
            except Exception:
                pass
        for p in reversed(slot.procs):
            try:
                wait = getattr(p, "wait", None)
                if callable(wait):
                    wait(timeout=2)
            except Exception:
                try:
                    kill = getattr(p, "kill", None)
                    if callable(kill):
                        kill()
                except Exception:
                    pass

    def _start_slot(self, bot_id: str, slot_num: int) -> ScreenSlot:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        profile_dir = self.profiles_root / safe_bot_id(bot_id) / "browser"
        profile_dir.mkdir(parents=True, exist_ok=True)

        display_num = self.screen_base + slot_num
        display = f":{display_num}"
        port = 6800 + display_num  # 6901, 6902, ...
        token = secrets.token_hex(12)
        bus_path = self.run_dir / f"bus-{display_num}"
        bus_address = f"unix:path={bus_path}"

        procs: list[Any] = []
        if self._which("Xvfb"):
            sock = Path(f"/tmp/.X11-unix/X{display_num}")
            for leftover in (Path(f"/tmp/.X{display_num}-lock"), sock):
                try:
                    leftover.unlink(missing_ok=True)
                except Exception:
                    pass
            procs.append(
                self._spawn(
                    ["Xvfb", display, "-screen", "0", f"{self.screen_size}x24", "-nolisten", "tcp"],
                    {},
                )
            )
            if self._wait_for_display:
                for _ in range(50):
                    if sock.exists():
                        break
                    time.sleep(0.1)

        if self._which("dbus-daemon"):
            try:
                bus_path.unlink(missing_ok=True)
            except Exception:
                pass
            procs.append(
                self._spawn(["dbus-daemon", "--session", "--nofork", f"--address={bus_address}"], {})
            )

        if self._which("openbox"):
            procs.append(
                self._spawn(["openbox"], {"DISPLAY": display, "DBUS_SESSION_BUS_ADDRESS": bus_address})
            )

        backend = "headless-xvfb"
        if self._which("voxl"):
            backend = "voxl"
            procs.append(
                self._spawn(
                    [
                        "voxl",
                        "--addr",
                        "0.0.0.0",
                        "--port",
                        str(port),
                        "--allowed-hosts",
                        "*",
                        "--audio-enabled",
                        "false",
                        "--file-transfers",
                        "none",
                        "--enable-clipboard",
                        "true",
                    ],
                    {"DISPLAY": display, "VOXL_PASSWORD": token},
                )
            )
        elif self._which("x11vnc") and self._which("websockify"):
            backend = "novnc"
            vnc_port = 5900 + slot_num
            procs.append(
                self._spawn(
                    [
                        "x11vnc",
                        "-display",
                        display,
                        "-rfbport",
                        str(vnc_port),
                        "-passwd",
                        token,
                        "-forever",
                        "-shared",
                        "-localhost",
                    ],
                    {"DISPLAY": display},
                )
            )
            web_dir = "/usr/share/novnc" if Path("/usr/share/novnc").exists() else "/tmp"
            procs.append(
                self._spawn(
                    ["websockify", "--web", web_dir, f"0.0.0.0:{port}", f"127.0.0.1:{vnc_port}"],
                    {},
                )
            )

        return ScreenSlot(
            bot_id=bot_id,
            slot=slot_num,
            display_num=display_num,
            port=port,
            token=token,
            backend=backend,
            profile_dir=profile_dir,
            bus_address=bus_address,
            procs=procs,
        )

    def ensure(self, bot_id: str, busy: bool | None = None) -> dict[str, Any]:
        with self._lock:
            existing = self._slots.get(bot_id)
            if existing is not None:
                if self._is_alive(existing):
                    existing.last_used = time.monotonic()
                    if busy is not None:
                        existing.busy = busy
                    return existing.info_dict(self.public_base)
                self._slots.pop(bot_id, None)
                self._stop_slot(existing)

            if len(self._slots) >= self.max_screens:
                idle_candidates = [s for s in self._slots.values() if not s.busy]
                if not idle_candidates:
                    raise RuntimeError(
                        f"All {self.max_screens} screen slots are currently busy; retry after a bot finishes."
                    )
                victim = min(idle_candidates, key=lambda s: s.last_used)
                self._slots.pop(victim.bot_id, None)
                self._stop_slot(victim)

            used_slots = {s.slot for s in self._slots.values()}
            free_slot = next((n for n in range(1, self.max_screens + 1) if n not in used_slots), None)
            if free_slot is None:
                raise RuntimeError("No screen slot available.")

            created = self._start_slot(bot_id, free_slot)
            if busy is not None:
                created.busy = busy
            self._slots[bot_id] = created
            return created.info_dict(self.public_base)

    def set_busy(self, bot_id: str, busy: bool) -> bool:
        with self._lock:
            slot = self._slots.get(bot_id)
            if not slot:
                return False
            slot.busy = busy
            slot.last_used = time.monotonic()
            return True

    def close(self, bot_id: str) -> bool:
        with self._lock:
            slot = self._slots.pop(bot_id, None)
        if slot is None:
            return False
        self._stop_slot(slot)
        return True

    def reap_idle(self, now: float | None = None) -> list[str]:
        check_now = now if now is not None else time.monotonic()
        reaped: list[str] = []
        to_stop: list[ScreenSlot] = []
        with self._lock:
            for bid, slot in list(self._slots.items()):
                if not slot.busy and (check_now - slot.last_used) > self.idle_seconds:
                    to_stop.append(self._slots.pop(bid))
                    reaped.append(bid)
        for slot in to_stop:
            self._stop_slot(slot)
        return reaped

    def list_screens(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.info_dict(self.public_base) for s in self._slots.values()]

    def health(self) -> dict[str, Any]:
        with self._lock:
            active = len(self._slots)
        return {
            "ok": True,
            "active_screens": active,
            "max_screens": self.max_screens,
            "screen_size": self.screen_size,
            "has_xvfb": bool(self._which("Xvfb")),
            "has_voxl": bool(self._which("voxl")),
            "has_novnc": bool(self._which("x11vnc") and self._which("websockify")),
            "has_agent_browser": bool(self._which("agent-browser")),
        }


def make_http_server(
    manager: ScreenManager,
    host: str = "0.0.0.0",
    port: int = 6900,
    internal_token: str = "",
) -> ThreadingHTTPServer:
    """Build a lightweight stdlib HTTP server wrapping `ScreenManager`."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, code: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _check_auth(self) -> bool:
            if not internal_token:
                return True
            provided = self.headers.get("X-Screens-Token", "")
            if hmac.compare_digest(provided, internal_token):
                return True
            self._send_json(401, {"ok": False, "error": "Unauthorized"})
            return False

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                return {}

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(200, manager.health())
                return
            if not self._check_auth():
                return
            if self.path == "/screens":
                self._send_json(200, {"ok": True, "screens": manager.list_screens(), **manager.health()})
                return
            self._send_json(404, {"ok": False, "error": "Not found"})

        def do_POST(self) -> None:
            if not self._check_auth():
                return
            if self.path == "/screens/reap":
                reaped = manager.reap_idle()
                self._send_json(200, {"ok": True, "reaped": reaped})
                return
            m = re.match(r"^/screens/([^/]+)/ensure$", self.path)
            if m:
                bot_id = m.group(1)
                body = self._read_body()
                busy = body.get("busy")
                try:
                    info = manager.ensure(bot_id, busy=busy if isinstance(busy, bool) else None)
                    self._send_json(200, {"ok": True, "screen": info})
                except RuntimeError as exc:
                    self._send_json(503, {"ok": False, "error": str(exc)})
                return
            self._send_json(404, {"ok": False, "error": "Not found"})

        def do_DELETE(self) -> None:
            if not self._check_auth():
                return
            m = re.match(r"^/screens/([^/]+)$", self.path)
            if m:
                closed = manager.close(m.group(1))
                self._send_json(200 if closed else 404, {"ok": closed, "bot_id": m.group(1)})
                return
            self._send_json(404, {"ok": False, "error": "Not found"})

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    mgr = ScreenManager(
        max_screens=int(os.environ.get("MAX_SCREENS", "2")),
        screen_base=int(os.environ.get("SCREEN_BASE", "100")),
        screen_size=os.environ.get("SCREEN_SIZE", "1280x800"),
        idle_minutes=float(os.environ.get("SCREEN_IDLE_MINUTES", "15")),
        public_base=os.environ.get("SCREEN_PUBLIC_BASE", ""),
    )

    def _reaper_loop() -> None:
        while True:
            time.sleep(60)
            mgr.reap_idle()

    threading.Thread(target=_reaper_loop, daemon=True).start()
    server = make_http_server(
        mgr,
        host=os.environ.get("SCREENS_HOST", "0.0.0.0"),
        port=int(os.environ.get("SCREENS_PORT", "6900")),
        internal_token=os.environ.get("SCREENS_INTERNAL_TOKEN", ""),
    )
    print(f"agent-screens listening on {server.server_address[0]}:{server.server_address[1]} (max={mgr.max_screens})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
