import sys
import os
sys.path.append(os.path.abspath('containers/agent-core'))

import asyncio
from src.notebooklm import NotebookLMConnector

async def main():
    print("Testing NotebookLMConnector...")
    # NOTE: This uses a dummy URL. A real run would use an actual NotebookLM URL and valid Chrome profile
    connector = NotebookLMConnector(
        notebook_url="https://notebooklm.google.com/notebook/placeholder",
        user_data_dir="/tmp/dummy_profile"
    )

    # We won't actually run get_content() as it would require a real Chrome profile and X11 display,
    # which we don't have in this automated test environment. The unit tests verify the logic using mocks.
    print(f"Connector initialized with URL: {connector.notebook_url}")
    print("Verification complete.")

if __name__ == "__main__":
    asyncio.run(main())
