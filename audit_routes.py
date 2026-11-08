import urllib.request
import urllib.error
import sys

# All primary user-facing UI and status pages from the registered routes
pages = [
    "/overview",
    "/chat",
    "/models/",
    "/cognitive/",
    "/agents/",
    "/skills/",
    "/world-model/",
    "/memory/",
    "/audit/",
    "/traces/",
    "/tasks/",
    "/health/",
    "/brain/",
    "/goals/",
    "/config/",
    "/identity",
    "/metrics/",
    "/knowledge-graph/",
    "/integrations/",
    "/behavioral-rules/",
    "/self-improve/",
    "/skill-evolution/",
    "/vector-memory/",
    "/subscriptions/",
    "/workspaces/",
    "/reset/",
    "/scheduler/",
    "/state/json",
    "/health/api/latest",
    "/audit/api/stats",
    "/scheduler/api/status",
]

base = "http://127.0.0.1:1434"
failed = []
passed = []

for r in pages:
    url = f"{base}{r}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AgentWASP-Auditor"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
            if status in (200, 302):
                passed.append((r, status))
            else:
                failed.append((r, status, "Unexpected status"))
    except urllib.error.HTTPError as e:
        failed.append((r, e.code, str(e)))
    except Exception as e:
        failed.append((r, 0, str(e)))

print("=" * 60)
print(f"AUDIT RESULTS: {len(passed)} PASSED, {len(failed)} FAILED")
print("=" * 60)
for r, s in passed:
    print(f"  [PASS {s}] {r}")

if failed:
    print("\nFAILED PAGES:")
    for r, s, err in failed:
        print(f"  [FAIL {s}] {r} -> {err}")
    sys.exit(1)
else:
    print("\n>>> ZERO INTERNAL SERVER ERRORS (100% HEALTHY) <<<")
    sys.exit(0)
