import asyncio
import json
import sys
import os

sys.path.insert(0, "/app")
from src.integrations.vault import SecretVault

async def main():
    try:
        secret = os.environ.get("DASHBOARD_SECRET", "wasp_dashboard_secret_2026_long_and_secure")
        v = SecretVault('redis://redis:6379', secret)
        creds = await v.get_all('github')
        if creds:
            print(json.dumps({k: "HIDDEN" for k in creds.keys()}))
        else:
            print("NONE")
    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    asyncio.run(main())
