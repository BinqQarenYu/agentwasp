"""Launch the Agent WASP Web Cockpit in a clean, standalone native application window."""
import os
import sys
import subprocess
import webbrowser

DASHBOARD_URL = "http://127.0.0.1:1434/overview"

def open_cockpit():
    edge_paths = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        "msedge.exe"
    ]
    for path in edge_paths:
        try:
            if os.path.exists(path) or path == "msedge.exe":
                subprocess.Popen([path, f"--app={DASHBOARD_URL}"])
                return True
        except Exception:
            continue

    chrome_paths = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        "chrome.exe"
    ]
    for path in chrome_paths:
        try:
            if os.path.exists(path) or path == "chrome.exe":
                subprocess.Popen([path, f"--app={DASHBOARD_URL}"])
                return True
        except Exception:
            continue

    webbrowser.open(DASHBOARD_URL)
    return True

if __name__ == "__main__":
    open_cockpit()
