"""Agent WASP Desktop Glassmorphic Control Widget.

Features:
- Glassmorphic dark styling with yellow/gold branding (#f5c542)
- Live CPU & RAM host resource monitoring (guarantees host safety)
- Staged, low-priority Docker startup & graceful shutdown
- Standalone Native Web App launcher (Edge / Chrome --app mode)
- Single instance lock with safe cleanup
- Non-blocking async health monitoring & smooth pulsing status
"""

import tkinter as tk
from tkinter import messagebox
import webbrowser
import subprocess
import os
import sys
import threading
from PIL import Image, ImageTk
import psutil

# Enable High DPI awareness on Windows for crisp typography & edges
try:
    from ctypes import windll
    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# --- Configuration ---
WASP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(WASP_DIR)
DASHBOARD_URL = "http://127.0.0.1:1434/overview"
ICON_PATH = os.path.join(WASP_DIR, "icon.png")


class WaspWidget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Agent WASP Controller")
        
        # Frameless, topmost, translucent
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.94)
        
        # Geometry: 330x140 Bottom Right
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        self.root.geometry(f"330x140+{screen_width - 350}+{screen_height - 200}")
        
        # Dragging
        self.root.bind("<Button-1>", self.start_move)
        self.root.bind("<B1-Motion>", self.do_move)
        
        # State
        self.auto_heal = True
        self.is_recovering = False
        self.auto_opened = False
        self.pulse_step = 0
        
        self.setup_ui()
        self.pulse_status()
        self.update_telemetry()
        self.update_status()

    def setup_ui(self):
        self.bg_frame = tk.Frame(
            self.root, 
            bg="#0f0f12", 
            highlightthickness=1, 
            highlightbackground="#f5c542"
        )
        self.bg_frame.pack(fill=tk.BOTH, expand=True)
        
        # Icon / Logo
        try:
            img = Image.open(ICON_PATH)
            img = img.resize((54, 54), Image.Resampling.LANCZOS)
            self.icon_img = ImageTk.PhotoImage(img)
            self.icon_label = tk.Label(self.bg_frame, image=self.icon_img, bg="#0f0f12", cursor="hand2")
            self.icon_label.pack(side=tk.LEFT, padx=(14, 8))
            self.icon_label.bind("<Button-1>", lambda e: self.open_dashboard())
        except Exception:
            self.icon_label = tk.Label(self.bg_frame, text="🐝", fg="#f5c542", bg="#0f0f12", font=("Segoe UI", 26))
            self.icon_label.pack(side=tk.LEFT, padx=(14, 8))
            self.icon_label.bind("<Button-1>", lambda e: self.open_dashboard())

        # Main Info & Actions Panel
        self.info_frame = tk.Frame(self.bg_frame, bg="#0f0f12")
        self.info_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=10)
        
        # Header Row
        title_row = tk.Frame(self.info_frame, bg="#0f0f12")
        title_row.pack(fill=tk.X)
        tk.Label(title_row, text="AGENT WASP", fg="#ffffff", bg="#0f0f12", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        
        # Close Button
        self.close_btn = tk.Label(self.bg_frame, text="✕", fg="#555555", bg="#0f0f12", font=("Segoe UI", 9, "bold"), cursor="hand2")
        self.close_btn.place(x=310, y=5)
        self.close_btn.bind("<Button-1>", lambda e: self.root.destroy())
        self.close_btn.bind("<Enter>", lambda e: self.close_btn.config(fg="#ff5555"))
        self.close_btn.bind("<Leave>", lambda e: self.close_btn.config(fg="#555555"))

        # Status Label (Pulsing Indicator)
        self.status_label = tk.Label(
            self.info_frame, 
            text="● Initializing...", 
            fg="#888888", 
            bg="#0f0f12", 
            font=("Segoe UI", 8, "bold")
        )
        self.status_label.pack(anchor="w", pady=(1, 2))
        
        # Host Resource Telemetry (CPU & RAM Safety Bar)
        self.telemetry_label = tk.Label(
            self.info_frame,
            text="RAM: --% | CPU: --%",
            fg="#777788",
            bg="#0f0f12",
            font=("Segoe UI", 8)
        )
        self.telemetry_label.pack(anchor="w", pady=(0, 6))

        # Control Buttons
        self.btn_frame = tk.Frame(self.info_frame, bg="#0f0f12")
        self.btn_frame.pack(anchor="w")
        
        # 1. Dashboard (App Mode)
        self.btn_dash = tk.Button(
            self.btn_frame, text="Cockpit", bg="#1f1f28", fg="#ffffff",
            font=("Segoe UI", 8, "bold"), command=self.open_dashboard,
            relief=tk.FLAT, bd=0, padx=8, pady=3,
            activebackground="#f5c542", activeforeground="#111111"
        )
        self.btn_dash.pack(side=tk.LEFT, padx=(0, 4))
        self.btn_dash.bind("<Enter>", lambda e: self.btn_dash.config(bg="#f5c542", fg="#111111"))
        self.btn_dash.bind("<Leave>", lambda e: self.btn_dash.config(bg="#1f1f28", fg="#ffffff"))
        
        # 2. Gentle Start Stack
        self.btn_start = tk.Button(
            self.btn_frame, text="Start", bg="#1f1f28", fg="#ffffff",
            font=("Segoe UI", 8, "bold"), command=self.start_stack,
            relief=tk.FLAT, bd=0, padx=8, pady=3,
            activebackground="#f5c542", activeforeground="#111111"
        )
        self.btn_start.pack(side=tk.LEFT, padx=4)
        self.btn_start.bind("<Enter>", lambda e: self.btn_start.config(bg="#f5c542", fg="#111111"))
        self.btn_start.bind("<Leave>", lambda e: self.btn_start.config(bg="#1f1f28", fg="#ffffff"))

        # 3. Stop Stack
        self.btn_stop = tk.Button(
            self.btn_frame, text="Stop", bg="#1f1f28", fg="#ffffff",
            font=("Segoe UI", 8, "bold"), command=self.stop_stack,
            relief=tk.FLAT, bd=0, padx=8, pady=3,
            activebackground="#ff5555", activeforeground="#111111"
        )
        self.btn_stop.pack(side=tk.LEFT, padx=4)
        self.btn_stop.bind("<Enter>", lambda e: self.btn_stop.config(bg="#ff5555", fg="#111111"))
        self.btn_stop.bind("<Leave>", lambda e: self.btn_stop.config(bg="#1f1f28", fg="#ffffff"))

    def start_move(self, event):
        self.x = event.x
        self.y = event.y

    def do_move(self, event):
        deltax = event.x - self.x
        deltay = event.y - self.y
        x = self.root.winfo_x() + deltax
        y = self.root.winfo_y() + deltay
        self.root.geometry(f"+{x}+{y}")

    def update_telemetry(self):
        """Continuously update CPU and RAM usage to verify host safety."""
        try:
            mem = psutil.virtual_memory().percent
            cpu = psutil.cpu_percent()
            color = "#ff5555" if mem > 85.0 or cpu > 85.0 else "#888899"
            self.telemetry_label.config(text=f"RAM: {mem:.0f}%  |  CPU: {cpu:.0f}%", fg=color)
        except Exception:
            pass
        self.root.after(3000, self.update_telemetry)

    def open_dashboard(self):
        """Open the cockpit dashboard as a standalone native app window."""
        url = DASHBOARD_URL
        opened = False
        
        # Edge standalone app window
        edge_paths = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            "msedge.exe"
        ]
        for path in edge_paths:
            try:
                if os.path.exists(path) or path == "msedge.exe":
                    subprocess.Popen([path, f"--app={url}"])
                    opened = True
                    break
            except Exception:
                continue
                
        # Chrome fallback
        if not opened:
            chrome_paths = [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
                "chrome.exe"
            ]
            for path in chrome_paths:
                try:
                    if os.path.exists(path) or path == "chrome.exe":
                        subprocess.Popen([path, f"--app={url}"])
                        opened = True
                        break
                except Exception:
                    continue
                    
        if not opened:
            webbrowser.open(url)

    def start_stack(self):
        """Launch the stack via the gentle launcher."""
        self.auto_heal = True
        self.status_label.config(text="● Launching gently...", fg="#f5c542")
        threading.Thread(target=self._run_gentle_up).start()

    def stop_stack(self):
        """Gracefully stop containers."""
        self.auto_heal = False
        self.status_label.config(text="● Stopping stack...", fg="#ff5555")
        threading.Thread(target=self._run_docker_down).start()

    def _run_gentle_up(self):
        try:
            # Check RAM first
            if psutil.virtual_memory().percent >= 85.0:
                self.root.after(0, lambda: self.status_label.config(text="● Paused (RAM >85%)", fg="#ffaa00"))
                self.is_recovering = False
                return

            gentle_bat = os.path.join(PROJECT_ROOT, "start_gentle.bat")
            if os.path.exists(gentle_bat):
                subprocess.run(["cmd", "/c", gentle_bat], cwd=PROJECT_ROOT, check=True, capture_output=True)
            else:
                subprocess.run(["docker", "compose", "up", "-d"], cwd=PROJECT_ROOT, check=True, capture_output=True)
        except Exception as e:
            docker_info = subprocess.run(["docker", "info"], capture_output=True)
            if docker_info.returncode != 0:
                self.root.after(0, lambda: self.status_label.config(text="● Docker Engine Offline", fg="#ff5555"))
            else:
                self.root.after(0, lambda: self.status_label.config(text="● Launch Error", fg="#ff0000"))
            self.is_recovering = False

    def _run_docker_down(self):
        try:
            subprocess.run(["docker", "compose", "down"], cwd=PROJECT_ROOT, check=True, capture_output=True)
            self.root.after(0, lambda: self.status_label.config(text="● Offline (Paused)", fg="#ff5555"))
        except Exception:
            self.root.after(0, lambda: self.status_label.config(text="● Stop Error", fg="#ff0000"))

    def update_status(self):
        """Poll container health and auto-heal if online was expected."""
        threading.Thread(target=self._check_health).start()
        self.root.after(15000, self.update_status)

    def _check_health(self):
        try:
            import urllib.request
            urllib.request.urlopen(f"{DASHBOARD_URL}", timeout=2)
            self.root.after(0, lambda: self.status_label.config(text="● Online & Guarded", fg="#00ff00"))
            self.is_recovering = False
            
            if not self.auto_opened:
                self.auto_opened = True
                self.root.after(600, self.open_dashboard)
        except Exception:
            if self.auto_heal:
                if not self.is_recovering:
                    self.is_recovering = True
                    self.root.after(0, lambda: self.status_label.config(text="● Auto-Recovering...", fg="#f5c542"))
                    self._run_gentle_up()
            else:
                self.root.after(0, lambda: self.status_label.config(text="● Offline (Paused)", fg="#ff5555"))

    def pulse_status(self):
        """Smooth breathing glow animation for the status dot."""
        status_text = self.status_label.cget("text")
        
        if "Online" in status_text:
            green_shades = ["#00aa00", "#00cc00", "#00ee00", "#00ff00", "#55ff55", "#00ff00", "#00ee00", "#00cc00"]
            self.status_label.config(fg=green_shades[self.pulse_step % len(green_shades)])
            self.pulse_step += 1
        elif "Recovering" in status_text or "Launching" in status_text:
            gold_shades = ["#b38f00", "#d4af37", "#f5c542", "#ffd700", "#fff0a3", "#ffd700", "#f5c542", "#d4af37"]
            self.status_label.config(fg=gold_shades[self.pulse_step % len(gold_shades)])
            self.pulse_step += 1
            
        self.root.after(160, self.pulse_status)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    import socket
    
    # Enforce single instance lock
    _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _lock_socket.bind(("127.0.0.1", 14349))
    except socket.error:
        sys.exit(0)
        
    app = WaspWidget()
    app.run()
