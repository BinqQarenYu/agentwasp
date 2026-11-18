"""Check host RAM usage and ensure headroom exists before launching."""
import sys
import psutil

mem = psutil.virtual_memory()
sys_ok = mem.percent < 90.0
print(f"RAM Usage: {mem.percent:.1f}% (Threshold: 90%) - Headroom OK: {sys_ok}")
sys.exit(0 if sys_ok else 1)
