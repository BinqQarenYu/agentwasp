🧹 [Code Health] Extract prompt configuration to constants.py

🎯 **What:** The code health issue addressed
Extracted massive static configuration data from `containers/agent-core/src/agent/context.py` into a new file `containers/agent-core/src/agent/constants.py`.

💡 **Why:** How this improves maintainability
Previously, `context.py` was heavily polluted by long static lists and prompts like `MODEL_CREATORS`, `PROVIDER_LABELS`, `SYSTEM_PROMPT`, and very long lists like `IDENTITY_POISON` and `SKILL_POISON` which were hardcoded directly inside the `build_context` function.

By cleanly separating configuration constants (`constants.py`) from runtime logic (`context.py`), it drastically reduces the file size and complexity of `context.py`. This improves readability, reduces the function's perceived complexity, and makes the file easier to maintain.

✅ **Verification:** How you confirmed the change is safe
- Extracted and imported the exact same constants without any modifications.
- Ran `make lint` to verify that `flake8` and `mypy` pass.
- Ran the full test suite (`pytest tests/`) to ensure no functionality is broken.

✨ **Result:** The improvement achieved
A much cleaner `context.py` with the massive lists moved to `constants.py`.
