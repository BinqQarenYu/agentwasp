import tkinter as tk
from tkinter import messagebox
import webbrowser
import subprocess
import os
import sys
import threading
from PIL import Image, ImageTk

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
        self.root.attributes("-alpha", 0.9)  # Slight transparency
        
        # Position: Bottom Right
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        self.root.geometry(f"280x120+{screen_width - 300}+{screen_height - 180}")
        
        # Draggability
        self.root.bind("<Button-1>", self.start_move)
        self.root.bind("<B1-Motion>", self.do_move)
        
        self.setup_ui()
        
    def setup_ui(self):
        # Background Frame
        self.bg_frame = tk.Frame(self.root, bg="#1a1a1a", highlightthickness=1, highlightbackground="#f5c542")
        self.bg_frame.pack(fill=tk.BOTH, expand=True)
        
        # Icon
        try:
            img = Image.open(ICON_PATH)
            img = img.resize((60, 60), Image.Resampling.LANCZOS)
            self.icon_img = ImageTk.PhotoImage(img)
            self.icon_label = tk.Label(self.bg_frame, image=self.icon_img, bg="#1a1a1a", cursor="hand2")
            self.icon_label.pack(side=tk.LEFT, padx=15)
            self.icon_label.bind("<Button-1>", lambda e: self.open_dashboard())
        except Exception as e:
            print(f"Icon error: {e}")
            self.icon_label = tk.Label(self.bg_frame, text="WASP", fg="#f5c542", bg="#1a1a1a", font=("Segoe UI", 16, "bold"))
            self.icon_label.pack(side=tk.LEFT, padx=15)

        # Labels & Buttons
        self.info_frame = tk.Frame(self.bg_frame, bg="#1a1a1a")
        self.info_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        tk.Label(self.info_frame, text="AGENT WASP", fg="white", bg="#1a1a1a", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(15, 0))
        self.status_label = tk.Label(self.info_frame, text="● Checking status...", fg="#888", bg="#1a1a1a", font=("Segoe UI", 8))
        self.status_label.pack(anchor="w")
        
        self.btn_frame = tk.Frame(self.info_frame, bg="#1a1a1a")
        self.btn_frame.pack(anchor="w", pady=5)
        
        self.btn_dash = tk.Button(self.btn_frame, text="Dashboard", bg="#333", fg="white", font=("Segoe UI", 8), 
                                 command=self.open_dashboard, relief=tk.FLAT, padx=5)
        self.btn_dash.pack(side=tk.LEFT, padx=2)
        
        self.btn_start = tk.Button(self.btn_frame, text="Start Stack", bg="#333", fg="white", font=("Segoe UI", 8), 
                                  command=self.start_stack, relief=tk.FLAT, padx=5)
        self.btn_start.pack(side=tk.LEFT, padx=2)

        # Close button (Top right small)
        self.close_btn = tk.Label(self.bg_frame, text="✕", fg="#555", bg="#1a1a1a", font=("Arial", 8))
        self.close_btn.place(x=260, y=5)
        self.close_btn.bind("<Button-1>", lambda e: self.root.destroy())
        
        self.update_status()

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
        webbrowser.open(DASHBOARD_URL)

    def start_stack(self):
        self.status_label.config(text="● Launching...", fg="#f5c542")
        threading.Thread(target=self._run_docker_up).start()

    def _run_docker_up(self):
        try:
            subprocess.run(["docker", "compose", "up", "-d"], cwd=PROJECT_ROOT, check=True, capture_output=True)
            self.root.after(0, lambda: self.status_label.config(text="● Online", fg="#00ff00"))
        except Exception as e:
            self.root.after(0, lambda: self.status_label.config(text="● Error", fg="#ff0000"))
            print(f"Docker error: {e}")

    def update_status(self):
        # Quick health check
        threading.Thread(target=self._check_health).start()
        self.root.after(30000, self.update_status) # Update every 30s

    def _check_health(self):
        try:
            import urllib.request
            urllib.request.urlopen(f"{DASHBOARD_URL}", timeout=2)
            self.root.after(0, lambda: self.status_label.config(text="● Online", fg="#00ff00"))
        except:
            self.root.after(0, lambda: self.status_label.config(text="● Offline", fg="#ff0000"))

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = WaspWidget()
    app.run()
