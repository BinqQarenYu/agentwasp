import tkinter as tk
from tkinter import messagebox
import webbrowser
import subprocess
import os
import sys
import threading
from PIL import Image, ImageTk

# Enable High DPI awareness on Windows for ultra-crisp text and borders
try:
    from ctypes import windll
    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# --- Configuration ---
WASP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(WASP_DIR)
DASHBOARD_URL = "http://localhost:1434/overview"
ICON_PATH = os.path.join(WASP_DIR, "icon.png")

class WaspWidget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Agent Wasp Widget")
        
        # Window Setup: Frameless, Topmost, Transparent background (if possible)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.95)  # Premium solid presence
        
        # Position: Bottom Right (300x120 size for modern, spacious controls)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        self.root.geometry(f"300x120+{screen_width - 320}+{screen_height - 180}")
        
        # Draggability
        self.root.bind("<Button-1>", self.start_move)
        self.root.bind("<B1-Motion>", self.do_move)
        
        # State Variables
        self.auto_heal = True         # Keep stack always online automatically
        self.is_recovering = False     # Guard against concurrent docker operations
        self.auto_opened = False       # Only open the standalone app once on startup
        self.pulse_step = 0
        
        self.setup_ui()
        self.pulse_status()            # Start premium breathing animation loop
        self.update_status()           # Start health monitoring loop
        
    def setup_ui(self):
        # Background Frame with Agent Yellow border
        self.bg_frame = tk.Frame(self.root, bg="#111111", highlightthickness=1, highlightbackground="#f5c542")
        self.bg_frame.pack(fill=tk.BOTH, expand=True)
        
        # Icon / Logo
        try:
            img = Image.open(ICON_PATH)
            img = img.resize((60, 60), Image.Resampling.LANCZOS)
            self.icon_img = ImageTk.PhotoImage(img)
            self.icon_label = tk.Label(self.bg_frame, image=self.icon_img, bg="#111111", cursor="hand2")
            self.icon_label.pack(side=tk.LEFT, padx=15)
            self.icon_label.bind("<Button-1>", lambda e: self.open_dashboard())
        except Exception as e:
            print(f"Icon error: {e}")
            self.icon_label = tk.Label(self.bg_frame, text="🐝", fg="#f5c542", bg="#111111", font=("Segoe UI", 24))
            self.icon_label.pack(side=tk.LEFT, padx=15)
            self.icon_label.bind("<Button-1>", lambda e: self.open_dashboard())

        # Labels & Buttons Container
        self.info_frame = tk.Frame(self.bg_frame, bg="#111111")
        self.info_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        # Application Header
        tk.Label(self.info_frame, text="AGENT WASP", fg="#ffffff", bg="#111111", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(15, 0))
        
        # Status Label (Pulsing Indicator)
        self.status_label = tk.Label(self.info_frame, text="● Checking status...", fg="#888888", bg="#111111", font=("Segoe UI", 9, "bold"))
        self.status_label.pack(anchor="w", pady=(2, 5))
        
        # Buttons Panel
        self.btn_frame = tk.Frame(self.info_frame, bg="#111111")
        self.btn_frame.pack(anchor="w")
        
        # 1. Dashboard Button (Standalone App Mode launcher)
        self.btn_dash = tk.Button(self.btn_frame, text="Dashboard", bg="#222222", fg="#ffffff", font=("Segoe UI", 8, "bold"), 
                                 command=self.open_dashboard, relief=tk.FLAT, bd=0, padx=8, pady=3, activebackground="#f5c542", activeforeground="#111111")
        self.btn_dash.pack(side=tk.LEFT, padx=3)
        self.btn_dash.bind("<Enter>", lambda e: self.btn_dash.config(bg="#f5c542", fg="#111111"))
        self.btn_dash.bind("<Leave>", lambda e: self.btn_dash.config(bg="#222222", fg="#ffffff"))
        
        # 2. Start / Resume Stack Button
        self.btn_start = tk.Button(self.btn_frame, text="Start", bg="#222222", fg="#ffffff", font=("Segoe UI", 8, "bold"), 
                                  command=self.start_stack, relief=tk.FLAT, bd=0, padx=8, pady=3, activebackground="#f5c542", activeforeground="#111111")
        self.btn_start.pack(side=tk.LEFT, padx=3)
        self.btn_start.bind("<Enter>", lambda e: self.btn_start.config(bg="#f5c542", fg="#111111"))
        self.btn_start.bind("<Leave>", lambda e: self.btn_start.config(bg="#222222", fg="#ffffff"))

        # 3. Stop / Pause Stack Button
        self.btn_stop = tk.Button(self.btn_frame, text="Stop", bg="#222222", fg="#ffffff", font=("Segoe UI", 8, "bold"), 
                                 command=self.stop_stack, relief=tk.FLAT, bd=0, padx=8, pady=3, activebackground="#ff5555", activeforeground="#111111")
        self.btn_stop.pack(side=tk.LEFT, padx=3)
        self.btn_stop.bind("<Enter>", lambda e: self.btn_stop.config(bg="#ff5555", fg="#111111"))
        self.btn_stop.bind("<Leave>", lambda e: self.btn_stop.config(bg="#222222", fg="#ffffff"))

        # Small Premium Close Button (Top right)
        self.close_btn = tk.Label(self.bg_frame, text="✕", fg="#555555", bg="#111111", font=("Segoe UI", 9, "bold"), cursor="hand2")
        self.close_btn.place(x=280, y=5)
        self.close_btn.bind("<Button-1>", lambda e: self.root.destroy())
        self.close_btn.bind("<Enter>", lambda e: self.close_btn.config(fg="#ff5555"))
        self.close_btn.bind("<Leave>", lambda e: self.close_btn.config(fg="#555555"))

    def start_move(self, event):
        self.x = event.x
        self.y = event.y

    def do_move(self, event):
        deltax = event.x - self.x
        deltay = event.y - self.y
        x = self.root.winfo_x() + deltax
        y = self.root.winfo_y() + deltay
        self.root.geometry(f"+{x}+{y}")

    def open_dashboard(self):
        """Open the dashboard as a native standalone application window using Edge or Chrome app mode."""
        url = DASHBOARD_URL
        opened = False
        
        # 1. Try Microsoft Edge (guaranteed to exist on Windows)
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
                
        # 2. Try Google Chrome
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
                    
        # 3. Fallback to default browser if both app-mode launchers fail
        if not opened:
            webbrowser.open(url)

    def start_stack(self):
        """Manual or automated triggers to start the stack and enable auto-healing."""
        self.auto_heal = True
        self.status_label.config(text="● Launching...", fg="#f5c542")
        threading.Thread(target=self._run_docker_up).start()

    def stop_stack(self):
        """Manual trigger to stop the stack and temporarily disable auto-healing (pauses recovery)."""
        self.auto_heal = False
        self.status_label.config(text="● Stopping...", fg="#ff5555")
        threading.Thread(target=self._run_docker_down).start()

    def _run_docker_up(self):
        try:
            subprocess.run(["docker", "compose", "up", "-d"], cwd=PROJECT_ROOT, check=True, capture_output=True)
            # Health check will update the status label dynamically
        except Exception as e:
            self.root.after(0, lambda: self.status_label.config(text="● Launch Error", fg="#ff0000"))
            print(f"Docker up error: {e}")
            self.is_recovering = False

    def _run_docker_down(self):
        try:
            subprocess.run(["docker", "compose", "down"], cwd=PROJECT_ROOT, check=True, capture_output=True)
            self.root.after(0, lambda: self.status_label.config(text="● Offline (Paused)", fg="#ff5555"))
        except Exception as e:
            self.root.after(0, lambda: self.status_label.config(text="● Stop Error", fg="#ff0000"))
            print(f"Docker down error: {e}")

    def update_status(self):
        """Check container health, and trigger self-healing (auto-start) if offline."""
        threading.Thread(target=self._check_health).start()
        # Fast monitoring rate (15 seconds) to ensure the stack is "always online"
        self.root.after(15000, self.update_status)

    def _check_health(self):
        try:
            import urllib.request
            # Verify the webserver endpoint responds
            urllib.request.urlopen(f"{DASHBOARD_URL}", timeout=2)
            self.root.after(0, lambda: self.status_label.config(text="● Online", fg="#00ff00"))
            
            # Successfully online: reset recovery guard
            self.is_recovering = False
            
            # Standalone auto-launch: open the standalone window immediately when ready
            if not self.auto_opened:
                self.auto_opened = True
                self.root.after(500, self.open_dashboard)
        except Exception:
            # If stack is offline and auto_heal is enabled, trigger auto-recovery (always online)
            if self.auto_heal:
                if not self.is_recovering:
                    self.is_recovering = True
                    self.root.after(0, lambda: self.status_label.config(text="● Auto-Recovering...", fg="#f5c542"))
                    self._run_docker_up()
                else:
                    self.root.after(0, lambda: self.status_label.config(text="● Auto-Recovering...", fg="#f5c542"))
            else:
                self.root.after(0, lambda: self.status_label.config(text="● Offline (Paused)", fg="#ff5555"))

    def pulse_status(self):
        """Micro-animation: Breathe/pulse status indicator to feel responsive and alive."""
        status_text = self.status_label.cget("text")
        
        if "Online" in status_text:
            # Smooth breathing green light
            green_shades = ["#00aa00", "#00cc00", "#00ee00", "#00ff00", "#55ff55", "#00ff00", "#00ee00", "#00cc00"]
            self.status_label.config(fg=green_shades[self.pulse_step % len(green_shades)])
            self.pulse_step += 1
        elif "Recovering" in status_text or "Launching" in status_text:
            # Pulsing golden light
            gold_shades = ["#b38f00", "#d4af37", "#f5c542", "#ffd700", "#fff0a3", "#ffd700", "#f5c542", "#d4af37"]
            self.status_label.config(fg=gold_shades[self.pulse_step % len(gold_shades)])
            self.pulse_step += 1
            
        # Fast update for super-smooth animation frames (150ms)
        self.root.after(150, self.pulse_status)

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    import socket
    import sys
    
    # Enforce single instance lock using a robust self-cleaning socket lock on port 14349
    _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _lock_socket.bind(("127.0.0.1", 14349))
    except socket.error:
        # Another instance is already running, exit silently to prevent duplicates
        sys.exit(0)
        
    app = WaspWidget()
    app.run()

