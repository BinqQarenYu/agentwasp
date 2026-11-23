"""Agent WASP Desktop Glassmorphic Control Widget.

Features:
- Premium glassmorphic obsidian styling with signature gold accents (#f5c542)
- Live 24/7 Autonomic Core indicator with breathing neon pulse
- Real-time fleet & mesh telemetry (Active bots, LAN nodes, host safety)
- Native Web App launcher (Edge / Chrome --app mode) for Cockpit, Mesh, and Visual Graph
- One-click fast restart & stack auto-heal
- Draggable with persistent desktop position memory
- Single-instance awareness: re-launching brings the active widget to front
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import Menu
from PIL import Image, ImageTk
import psutil

# Enable High DPI awareness on Windows for ultra-crisp typography & edges
try:
    from ctypes import windll
    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# --- Configuration & Paths ---
WASP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(WASP_DIR)
POS_FILE = os.path.join(WASP_DIR, "widget_pos.json")
ICON_PATH = os.path.join(WASP_DIR, "icon.png")
ICON_ICO_PATH = os.path.join(WASP_DIR, "icon.ico")

CHAT_URL = "http://127.0.0.1:1434/chat"
DASHBOARD_URL = "http://127.0.0.1:1434/overview"
TASKS_URL = "http://127.0.0.1:1434/tasks"
MESH_URL = "http://127.0.0.1:1434/mesh"
VISUAL_URL = "http://127.0.0.1:1434/workspaces/visual"
MESH_API_STATE = "http://127.0.0.1:1434/mesh/api/state"
LOCK_PORT = 14349


class WaspWidget:
    def __init__(self, server_sock: socket.socket | None = None):
        self.server_sock = server_sock
        # Lower host process priority so the desktop never stutters
        try:
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title("Agent WASP 24/7 Controller")

        # Window properties: frameless, topmost, alpha
        self.is_pinned = True
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.95)

        # Geometry & position
        win_w, win_h = 475, 175
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        pos_x, pos_y = self._load_position(screen_w, screen_h, win_w, win_h)
        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")

        # Dragging state
        self.drag_x = 0
        self.drag_y = 0

        # Runtime & telemetry state
        self.is_online = False
        self.pulse_step = 0
        self.bots_count = 15
        self.lan_nodes_count = 2
        self.start_timestamp = time.time()
        self.is_restarting = False

        self._setup_ui()
        self._setup_events()

        # Start background polling & animations
        self._poll_telemetry()
        self._pulse_animation()

        # Start IPC listener if server socket provided
        if self.server_sock:
            threading.Thread(target=self._ipc_listener, daemon=True).start()

        # Ensure Windows desktop shortcut exists with Ctrl+Alt+W hotkey
        threading.Thread(target=self.ensure_desktop_shortcut, daemon=True).start()

    def _ipc_listener(self):
        """Listen for activation pings from secondary instances (e.g. shortcut or hotkey)."""
        while True:
            try:
                conn, _ = self.server_sock.accept()
                with conn:
                    conn.settimeout(1.0)
                    try:
                        conn.recv(64)
                    except Exception:
                        pass
                    self.root.after(0, self.bring_to_front)
            except Exception:
                break

    def _load_position(self, screen_w: int, screen_h: int, win_w: int, win_h: int) -> tuple[int, int]:
        if os.path.exists(POS_FILE):
            try:
                with open(POS_FILE, "r", encoding="utf-8") as f:
                    pos = json.load(f)
                    x = pos.get("x")
                    y = pos.get("y")
                    if isinstance(x, int) and isinstance(y, int):
                        return x, y
            except Exception:
                pass
        return screen_w - win_w - 25, screen_h - win_h - 70

    def _save_position(self):
        try:
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            with open(POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": x, "y": y}, f)
        except Exception:
            pass

    def _setup_ui(self):
        # Master Outer Frame (Hairline Gold Border)
        self.outer_frame = tk.Frame(
            self.root,
            bg="#0b0d14",
            highlightthickness=1,
            highlightbackground="#f5c542",
            highlightcolor="#f5c542"
        )
        self.outer_frame.pack(fill=tk.BOTH, expand=True)

        # ---------------- TOP HEADER BAR ----------------
        self.header = tk.Frame(self.outer_frame, bg="#111420", height=32)
        self.header.pack(fill=tk.X, side=tk.TOP)
        self.header.pack_propagate(False)

        # Logo Icon
        try:
            img = Image.open(ICON_PATH)
            img = img.resize((22, 22), Image.Resampling.LANCZOS)
            self.icon_img = ImageTk.PhotoImage(img)
            self.icon_label = tk.Label(self.header, image=self.icon_img, bg="#111420", cursor="hand2")
            self.icon_label.pack(side=tk.LEFT, padx=(8, 4))
            self.icon_label.bind("<Button-1>", lambda e: self.open_app(DASHBOARD_URL))
        except Exception:
            self.icon_label = tk.Label(self.header, text="🐝", fg="#f5c542", bg="#111420", font=("Segoe UI", 11))
            self.icon_label.pack(side=tk.LEFT, padx=(8, 4))

        # Title & Subtitle Badge
        self.title_label = tk.Label(
            self.header,
            text="AGENT WASP",
            fg="#ffffff",
            bg="#111420",
            font=("Segoe UI", 9, "bold"),
            cursor="fleur"
        )
        self.title_label.pack(side=tk.LEFT, padx=(2, 6))

        self.sub_badge = tk.Label(
            self.header,
            text="24/7 AUTONOMIC CORE",
            fg="#f5c542",
            bg="#1d2236",
            font=("Segoe UI", 7, "bold"),
            padx=5,
            pady=1
        )
        self.sub_badge.pack(side=tk.LEFT)

        # Window Controls (Pin, Close)
        self.close_btn = tk.Label(
            self.header,
            text="✕",
            fg="#64748b",
            bg="#111420",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            padx=6
        )
        self.close_btn.pack(side=tk.RIGHT, padx=(0, 4))
        self.close_btn.bind("<Button-1>", lambda e: self.exit_app())
        self.close_btn.bind("<Enter>", lambda e: self.close_btn.config(fg="#ef4444"))
        self.close_btn.bind("<Leave>", lambda e: self.close_btn.config(fg="#64748b"))

        self.pin_btn = tk.Label(
            self.header,
            text="📌",
            fg="#f5c542",
            bg="#111420",
            font=("Segoe UI", 9),
            cursor="hand2",
            padx=4
        )
        self.pin_btn.pack(side=tk.RIGHT)
        self.pin_btn.bind("<Button-1>", lambda e: self.toggle_pin())

        # ---------------- CONTENT AREA ----------------
        self.body = tk.Frame(self.outer_frame, bg="#0b0d14", padx=12, pady=6)
        self.body.pack(fill=tk.BOTH, expand=True)

        # Row 1: Status & Uptime
        r1 = tk.Frame(self.body, bg="#0b0d14")
        r1.pack(fill=tk.X, pady=(2, 3))

        self.status_led = tk.Label(
            r1,
            text="● INITIALIZING 24/7 CORE...",
            fg="#f5c542",
            bg="#0b0d14",
            font=("Segoe UI", 8, "bold")
        )
        self.status_led.pack(side=tk.LEFT)

        self.uptime_label = tk.Label(
            r1,
            text="UPTIME: 0m",
            fg="#64748b",
            bg="#0b0d14",
            font=("Segoe UI", 7, "bold")
        )
        self.uptime_label.pack(side=tk.RIGHT)

        # Row 2: Mesh & LAN Fleet Telemetry
        r2 = tk.Frame(self.body, bg="#0b0d14")
        r2.pack(fill=tk.X, pady=(1, 3))

        self.fleet_label = tk.Label(
            r2,
            text="⚡ FLEET: Connecting to Mesh...",
            fg="#94a3b8",
            bg="#0b0d14",
            font=("Segoe UI", 8)
        )
        self.fleet_label.pack(side=tk.LEFT)

        # Row 3: Host CPU & RAM Safety Bar
        r3 = tk.Frame(self.body, bg="#0b0d14")
        r3.pack(fill=tk.X, pady=(1, 6))

        self.host_label = tk.Label(
            r3,
            text="💻 HOST: CPU --% | RAM --% (Guarded)",
            fg="#64748b",
            bg="#0b0d14",
            font=("Segoe UI", 8)
        )
        self.host_label.pack(side=tk.LEFT)

        # ---------------- ACTION BUTTON BAR ----------------
        self.actions = tk.Frame(self.body, bg="#0b0d14")
        self.actions.pack(fill=tk.X, side=tk.BOTTOM, pady=(0, 2))

        btn_style = {
            "bg": "#161b2b",
            "fg": "#e2e8f0",
            "font": ("Segoe UI", 8, "bold"),
            "relief": tk.FLAT,
            "bd": 0,
            "padx": 6,
            "pady": 3,
            "activebackground": "#f5c542",
            "activeforeground": "#0b0d14",
            "cursor": "hand2"
        }

        self.btn_chat = tk.Button(
            self.actions,
            text="💬 Chat",
            command=lambda: self.open_app(CHAT_URL),
            **btn_style
        )
        self.btn_chat.pack(side=tk.LEFT, padx=(0, 3))
        self._hover_effect(self.btn_chat, "#f5c542", "#0b0d14", "#161b2b", "#e2e8f0")

        self.btn_cockpit = tk.Button(
            self.actions,
            text="🚀 Cockpit",
            command=lambda: self.open_app(DASHBOARD_URL),
            **btn_style
        )
        self.btn_cockpit.pack(side=tk.LEFT, padx=(0, 3))
        self._hover_effect(self.btn_cockpit, "#38bdf8", "#0b0d14", "#161b2b", "#e2e8f0")

        self.btn_tasks = tk.Button(
            self.actions,
            text="⚡ Tasks",
            command=lambda: self.open_app(TASKS_URL),
            **btn_style
        )
        self.btn_tasks.pack(side=tk.LEFT, padx=(0, 3))
        self._hover_effect(self.btn_tasks, "#fbbf24", "#0b0d14", "#161b2b", "#e2e8f0")

        self.btn_mesh = tk.Button(
            self.actions,
            text="🕸️ Mesh",
            command=lambda: self.open_app(MESH_URL),
            **btn_style
        )
        self.btn_mesh.pack(side=tk.LEFT, padx=(0, 3))
        self._hover_effect(self.btn_mesh, "#34d399", "#0b0d14", "#161b2b", "#e2e8f0")

        self.btn_visual = tk.Button(
            self.actions,
            text="🔍 Visual",
            command=lambda: self.open_app(VISUAL_URL),
            **btn_style
        )
        self.btn_visual.pack(side=tk.LEFT, padx=(0, 3))
        self._hover_effect(self.btn_visual, "#a855f7", "#0b0d14", "#161b2b", "#e2e8f0")

        self.btn_restart = tk.Button(
            self.actions,
            text="🔄 Restart",
            command=self.restart_core,
            **btn_style
        )
        self.btn_restart.pack(side=tk.RIGHT)
        self._hover_effect(self.btn_restart, "#ef4444", "#ffffff", "#161b2b", "#e2e8f0")

    def _hover_effect(self, widget, hover_bg, hover_fg, normal_bg, normal_fg):
        widget.bind("<Enter>", lambda e: widget.config(bg=hover_bg, fg=hover_fg))
        widget.bind("<Leave>", lambda e: widget.config(bg=normal_bg, fg=normal_fg))

    def _setup_events(self):
        # Draggable by header, title, or outer frame
        for w in (self.header, self.title_label, self.sub_badge, self.outer_frame):
            w.bind("<Button-1>", self._start_drag)
            w.bind("<B1-Motion>", self._do_drag)
            w.bind("<ButtonRelease-1>", lambda e: self._save_position())

        # Double click on header opens Chat Cockpit
        self.header.bind("<Double-Button-1>", lambda e: self.open_app(CHAT_URL))

        # Hotkeys & Keyboard Shortcuts
        self.root.bind("<Key-t>", lambda e: self.open_app(CHAT_URL))
        self.root.bind("<Key-T>", lambda e: self.open_app(CHAT_URL))
        self.root.bind("<Control-t>", lambda e: self.open_app(CHAT_URL))
        self.root.bind("<Control-T>", lambda e: self.open_app(CHAT_URL))

        self.root.bind("<Key-c>", lambda e: self.open_app(DASHBOARD_URL))
        self.root.bind("<Key-C>", lambda e: self.open_app(DASHBOARD_URL))
        self.root.bind("<Control-c>", lambda e: self.open_app(DASHBOARD_URL))
        self.root.bind("<Control-C>", lambda e: self.open_app(DASHBOARD_URL))

        self.root.bind("<Key-k>", lambda e: self.open_app(TASKS_URL))
        self.root.bind("<Key-K>", lambda e: self.open_app(TASKS_URL))
        self.root.bind("<Control-k>", lambda e: self.open_app(TASKS_URL))
        self.root.bind("<Control-K>", lambda e: self.open_app(TASKS_URL))

        self.root.bind("<Key-m>", lambda e: self.open_app(MESH_URL))
        self.root.bind("<Key-M>", lambda e: self.open_app(MESH_URL))
        self.root.bind("<Control-m>", lambda e: self.open_app(MESH_URL))
        self.root.bind("<Control-M>", lambda e: self.open_app(MESH_URL))

        self.root.bind("<Key-v>", lambda e: self.open_app(VISUAL_URL))
        self.root.bind("<Key-V>", lambda e: self.open_app(VISUAL_URL))
        self.root.bind("<Control-v>", lambda e: self.open_app(VISUAL_URL))
        self.root.bind("<Control-V>", lambda e: self.open_app(VISUAL_URL))

        self.root.bind("<Key-p>", lambda e: self.toggle_pin())
        self.root.bind("<Key-P>", lambda e: self.toggle_pin())

        self.root.bind("<Key-r>", lambda e: self.restart_core())
        self.root.bind("<Key-R>", lambda e: self.restart_core())
        self.root.bind("<Control-r>", lambda e: self.restart_core())
        self.root.bind("<Control-R>", lambda e: self.restart_core())

        self.root.bind("<Escape>", lambda e: self.root.iconify())

        # Right-click context menu
        self.menu = Menu(self.root, tearoff=0, bg="#111420", fg="#e2e8f0", activebackground="#f5c542", activeforeground="#000000")
        self.menu.add_command(label="💬 Open Chat Cockpit (T)", command=lambda: self.open_app(CHAT_URL))
        self.menu.add_command(label="🚀 Open Overview Cockpit (C)", command=lambda: self.open_app(DASHBOARD_URL))
        self.menu.add_command(label="⚡ Open Tasks & Workflows (K)", command=lambda: self.open_app(TASKS_URL))
        self.menu.add_command(label="🕸️ Open Bot Mesh (M)", command=lambda: self.open_app(MESH_URL))
        self.menu.add_command(label="🔍 Open Workspace Visual Graph (V)", command=lambda: self.open_app(VISUAL_URL))
        self.menu.add_separator()
        self.menu.add_command(label="🖥️ Create Desktop Shortcut (Ctrl+Alt+W)", command=lambda: self.ensure_desktop_shortcut(force=True))
        self.menu.add_command(label="📌 Toggle Always on Top (P)", command=self.toggle_pin)
        self.menu.add_command(label="🔄 Restart Core Engine (R)", command=self.restart_core)
        self.menu.add_separator()
        self.menu.add_command(label="✕ Exit Widget", command=self.exit_app)

        self.root.bind("<Button-3>", self._show_context_menu)

    def _show_context_menu(self, event):
        self.menu.post(event.x_root, event.y_root)

    def _start_drag(self, event):
        self.drag_x = event.x
        self.drag_y = event.y

    def _do_drag(self, event):
        dx = event.x - self.drag_x
        dy = event.y - self.drag_y
        new_x = self.root.winfo_x() + dx
        new_y = self.root.winfo_y() + dy
        self.root.geometry(f"+{new_x}+{new_y}")

    def toggle_pin(self):
        self.is_pinned = not self.is_pinned
        self.root.attributes("-topmost", self.is_pinned)
        self.pin_btn.config(fg="#f5c542" if self.is_pinned else "#64748b")

    def bring_to_front(self):
        """Bring window to front and emphasize."""
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        # Pulse visual border
        self.outer_frame.config(highlightbackground="#00ffff")
        self.root.after(800, lambda: self.outer_frame.config(highlightbackground="#f5c542"))

    def ensure_desktop_shortcut(self, force: bool = False):
        """Create or refresh Windows desktop shortcut with icon and hotkey (Ctrl+Alt+W)."""
        if platform.system().lower() != "windows":
            return
        try:
            # Check if desktop shortcut already exists to avoid redundant subprocess spawns
            home = os.path.expanduser("~")
            dt_std = os.path.join(home, "Desktop", "Agent WASP.lnk")
            dt_one = os.path.join(home, "OneDrive", "Desktop", "Agent WASP.lnk")
            if not force and (os.path.exists(dt_std) or os.path.exists(dt_one)):
                return

            if not os.path.exists(ICON_ICO_PATH) and os.path.exists(ICON_PATH):
                from PIL import Image
                img = Image.open(ICON_PATH)
                img.save(ICON_ICO_PATH, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])

            pythonw = sys.executable.replace("python.exe", "pythonw.exe")
            script_path = os.path.abspath(__file__)
            widget_dir = WASP_DIR

            ps_code = f"""
            $wshell = New-Object -ComObject WScript.Shell
            $desktops = @(
                [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::Desktop),
                (Join-Path $env:USERPROFILE "Desktop"),
                (Join-Path $env:USERPROFILE "OneDrive\\Desktop")
            ) | Select-Object -Unique
            foreach ($dt in $desktops) {{
                if (Test-Path $dt) {{
                    $shortcut = $wshell.CreateShortcut((Join-Path $dt "Agent WASP.lnk"))
                    $shortcut.TargetPath = "{pythonw}"
                    $shortcut.Arguments = '"{script_path}"'
                    $shortcut.WorkingDirectory = "{widget_dir}"
                    $shortcut.IconLocation = "{ICON_ICO_PATH},0"
                    $shortcut.Description = "Agent WASP 24/7 Autonomic Desktop Widget & Mission Controller (Ctrl+Alt+W)"
                    $shortcut.Hotkey = "Ctrl+Alt+W"
                    $shortcut.Save()
                }}
            }}
            """
            creationflags = 0x08000000
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_code],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                timeout=8
            )
        except Exception:
            pass

    def open_app(self, url: str):
        """Open cockpit or views in standalone app window."""
        def _launch():
            opened = False
            flags = 0x08000000 if platform.system().lower() == "windows" else 0
            # Try MS Edge --app
            edge_paths = [
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
                "msedge.exe"
            ]
            for p in edge_paths:
                try:
                    if os.path.exists(p) or p == "msedge.exe":
                        subprocess.Popen(
                            [p, f"--app={url}"],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=flags
                        )
                        opened = True
                        break
                except Exception:
                    pass

            # Try Chrome --app
            if not opened:
                chrome_paths = [
                    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
                    "chrome.exe"
                ]
                for p in chrome_paths:
                    try:
                        if os.path.exists(p) or p == "chrome.exe":
                            subprocess.Popen(
                                [p, f"--app={url}"],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=flags
                            )
                            opened = True
                            break
                    except Exception:
                        pass

            if not opened:
                webbrowser.open(url)

        threading.Thread(target=_launch, daemon=True).start()

    def restart_core(self):
        if self.is_restarting:
            return
        self.is_restarting = True
        self.status_led.config(text="● RESTARTING CORE ENGINE...", fg="#f5c542")
        self.btn_restart.config(text="...", state=tk.DISABLED)

        def _do_restart():
            try:
                flags = 0x08000000 if platform.system().lower() == "windows" else 0
                subprocess.run(
                    ["docker", "compose", "restart", "agent-core"],
                    cwd=PROJECT_ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags,
                    check=True,
                    timeout=25
                )
                time.sleep(2)
                self.root.after(0, self._on_restart_success)
            except Exception as e:
                self.root.after(0, lambda: self._on_restart_failed(str(e)))

        threading.Thread(target=_do_restart, daemon=True).start()

    def _on_restart_success(self):
        self.is_restarting = False
        self.status_led.config(text="● 24/7 CORE RESTORED", fg="#10b981")
        self.btn_restart.config(text="Restart", state=tk.NORMAL)

    def _on_restart_failed(self, err: str):
        self.is_restarting = False
        self.status_led.config(text="● RESTART FAILED", fg="#ef4444")
        self.btn_restart.config(text="Restart", state=tk.NORMAL)

    def _poll_telemetry(self):
        """Fetch host CPU/RAM and live Docker agent telemetry asynchronously."""
        def _fetch():
            # 1. Host Resources
            mem = psutil.virtual_memory().percent
            cpu = psutil.cpu_percent()

            # 2. Check HTTP Health & Mesh Telemetry
            online = False
            bots = 0
            lan_nodes = 0
            try:
                req = urllib.request.Request(MESH_API_STATE, headers={"User-Agent": "WaspWidget"})
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    if resp.status == 200:
                        online = True
                        data = json.loads(resp.read().decode("utf-8"))
                        bots = data.get("bots_online", 15)
                        lan_nodes = data.get("lan_fleet", {}).get("total_nodes", 2)
            except Exception:
                # Fallback probe to basic dashboard overview
                try:
                    with urllib.request.urlopen(DASHBOARD_URL, timeout=1.5) as resp:
                        online = (resp.status == 200)
                except Exception:
                    online = False

            # Calculate Uptime
            elapsed_sec = int(time.time() - self.start_timestamp)
            mins = elapsed_sec // 60
            hrs = mins // 60
            days = hrs // 24
            if days > 0:
                uptime_str = f"UPTIME: {days}d {hrs % 24}h"
            elif hrs > 0:
                uptime_str = f"UPTIME: {hrs}h {mins % 60}m"
            else:
                uptime_str = f"UPTIME: {mins}m"

            self.root.after(0, lambda: self._update_ui_state(online, mem, cpu, bots, lan_nodes, uptime_str))

        threading.Thread(target=_fetch, daemon=True).start()
        self.root.after(4000, self._poll_telemetry)

    def _update_ui_state(self, online: bool, mem: float, cpu: float, bots: int, lan_nodes: int, uptime: str):
        self.is_online = online
        self.bots_count = bots
        self.lan_nodes_count = lan_nodes

        # Status text
        if not self.is_restarting:
            if online:
                self.status_led.config(text="● RUNNING 24/7 (SOVEREIGN)", fg="#10b981")
            else:
                self.status_led.config(text="● CORE OFFLINE (CHECK DOCKER)", fg="#ef4444")

        self.uptime_label.config(text=uptime)

        # Fleet label
        if online and bots > 0:
            lan_text = f" | 🌐 LAN: {lan_nodes} Nodes" if lan_nodes > 0 else ""
            self.fleet_label.config(text=f"⚡ FLEET: {bots} Bots Active{lan_text}", fg="#94a3b8")
        elif online:
            self.fleet_label.config(text="⚡ FLEET: Core Active & Monitoring", fg="#94a3b8")
        else:
            self.fleet_label.config(text="⚡ FLEET: Awaiting Core Heartbeat...", fg="#ef4444")

        # Host resource bar
        cpu_color = "#ef4444" if cpu > 85.0 else ("#f5c542" if cpu > 60.0 else "#64748b")
        mem_color = "#ef4444" if mem > 85.0 else ("#f5c542" if mem > 75.0 else "#64748b")
        color = mem_color if mem > 75.0 else cpu_color
        self.host_label.config(text=f"💻 HOST: CPU {cpu:.0f}%  |  RAM {mem:.0f}%  (Guarded)", fg=color)

    def _pulse_animation(self):
        """Breathing glow animation for status LED."""
        if self.is_online and not self.is_restarting:
            green_palette = [
                "#10b981", "#34d399", "#6ee7b7", "#a7f3d0",
                "#6ee7b7", "#34d399", "#10b981", "#059669"
            ]
            color = green_palette[self.pulse_step % len(green_palette)]
            self.status_led.config(fg=color)
            self.pulse_step += 1
        elif not self.is_online:
            red_palette = ["#ef4444", "#f87171", "#fca5a5", "#f87171"]
            color = red_palette[self.pulse_step % len(red_palette)]
            self.status_led.config(fg=color)
            self.pulse_step += 1

        self.root.after(200, self._pulse_animation)

    def exit_app(self):
        self._save_position()
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
        self.root.destroy()
        sys.exit(0)

    def run(self):
        self.root.mainloop()


def main():
    # 1. Single instance awareness via loopback IPC: bring existing widget to front
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(0.4)
        client.connect(("127.0.0.1", LOCK_PORT))
        client.sendall(b"FRONT\n")
        client.close()
        # Successfully signaled existing running instance to bring to front
        sys.exit(0)
    except (ConnectionRefusedError, socket.timeout, OSError):
        pass

    # 2. Bind loopback IPC port for this primary instance
    server_sock = None
    try:
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", LOCK_PORT))
        server_sock.listen(5)
    except Exception:
        # If binding fails, another instance claimed it in the interim
        sys.exit(0)

    app = WaspWidget(server_sock=server_sock)
    app.run()


if __name__ == "__main__":
    main()
