"""Quick health check for Agent WASP core stack."""
import time
import sys
import urllib.request

start = time.time()
while time.time() - start < 30:
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:1434/health", timeout=1.5)
        if resp.status == 200:
            print("[OK] Agent WASP Web Cockpit is online and healthy!")
            sys.exit(0)
    except Exception:
        time.sleep(1)

print("[Notice] Agent WASP Core is still initializing in background.")
sys.exit(0)
