"""Launch Ollama in background with BelowNormal priority if not already running."""
import os
import sys
import subprocess
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:11434/", timeout=0.8)
    print("[Ollama] Local inference engine is active on port 11434.")
    sys.exit(0)
except Exception:
    pass

ollama_path = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")
if not os.path.exists(ollama_path):
    print("[Ollama] Ollama binary not found in standard path. Skipping.")
    sys.exit(0)

try:
    # BELOW_NORMAL_PRIORITY_CLASS (0x4000) | CREATE_NO_WINDOW (0x08000000)
    creationflags = 0x00004000 | 0x08000000
    subprocess.Popen(
        [ollama_path, "serve"],
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    print("[Ollama] Started local inference engine in background (BelowNormal priority).")
except Exception as e:
    print(f"[Ollama] Could not start Ollama: {e}")
