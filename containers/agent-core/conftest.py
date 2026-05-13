# Root-level conftest — required for pytest to find pytest_asyncio plugin
# across all sub-directories without the non-top-level conftest deprecation warning.
pytest_plugins = ["pytest_asyncio"]
