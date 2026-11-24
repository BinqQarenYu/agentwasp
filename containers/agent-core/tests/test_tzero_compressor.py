# -*- coding: utf-8 -*-
"""Unit tests for T-Zero Deterministic AST Token Compression Engine."""

import pytest
from src.models.tzero_compressor import TZeroCompressor, tzero_compressor
from src.models.types import Message, ModelRequest


def test_python_ast_reduction():
    compressor = TZeroCompressor(line_threshold=5)
    sample_code = """import os
from typing import List, Optional

class PaymentProcessor:
    \"\"\"Handles credit card transactions securely.\"\"\"
    def __init__(self, api_key: str, timeout: int = 30):
        self.api_key = api_key
        self.timeout = timeout
        self.retries = 3
        print("Initialized processor")

    async def charge(self, amount: float, currency: str = "USD") -> dict:
        \"\"\"Executes a charge request.\"\"\"
        if amount <= 0:
            raise ValueError("Invalid amount")
        result = {"status": "success", "amount": amount}
        return result

def helper_calc(val: int) -> int:
    a = val * 2
    b = a + 10
    return b
"""
    reduced = compressor.reduce_code(sample_code, lang="python")

    # Checks
    assert "class PaymentProcessor:" in reduced
    assert "def __init__(self, api_key: str, timeout: int=30):" in reduced or "def __init__(self, api_key: str, timeout: int = 30):" in reduced
    assert "async def charge(self, amount: float, currency: str='USD') -> dict:" in reduced or "async def charge(self, amount: float, currency: str = \"USD\") -> dict:" in reduced
    assert "def helper_calc(val: int) -> int:" in reduced
    assert "Handles credit card transactions securely." in reduced
    assert "Executes a charge request." in reduced

    # Verify implementation bodies were pruned
    assert "print(\"Initialized processor\")" not in reduced
    assert "raise ValueError(\"Invalid amount\")" not in reduced
    assert "a = val * 2" not in reduced
    assert len(reduced) < len(sample_code)


def test_ts_polyglot_reduction():
    compressor = TZeroCompressor(line_threshold=5)
    ts_code = """export class UserService {
    private db: Database;
    constructor(db: Database) {
        this.db = db;
        console.log("DB connected");
        this.initPool();
    }

    public async getUser(id: string): Promise<User> {
        const query = "SELECT * FROM users WHERE id = " + id;
        const res = await this.db.query(query);
        return res.rows[0];
    }
}"""
    reduced = compressor.reduce_code(ts_code, lang="typescript")
    assert "export class UserService {" in reduced
    assert "getUser(id: string): Promise<User>" in reduced or "getUser" in reduced
    assert len(reduced) < len(ts_code)


def test_compress_messages_threshold():
    compressor = TZeroCompressor(line_threshold=10)

    # 1. Short snippet (< 10 lines) -> should NOT be compressed
    short_code = """```python
def add(a: int, b: int) -> int:
    return a + b
```"""
    msg_short = [Message(role="user", content=f"How does this look?\n{short_code}")]
    res_short, saved_short = compressor.compress_messages(msg_short)
    assert saved_short == 0
    assert "return a + b" in res_short[0].content

    # 2. Long snippet (> 10 lines) -> MUST be compressed
    long_code = """```python
class DataPipeline:
    def step1(self):
        x = 1
        y = 2
        z = x + y
        return z

    def step2(self):
        a = "hello"
        b = a + " world"
        c = b.upper()
        return c

    def step3(self):
        arr = []
        for i in range(10):
            arr.append(i)
        return arr
```"""
    msg_long = [Message(role="user", content=f"Please analyze this:\n{long_code}")]
    res_long, saved_long = compressor.compress_messages(msg_long)
    assert saved_long > 0
    assert "class DataPipeline:" in res_long[0].content
    assert "for i in range(10):" not in res_long[0].content


def test_diff_reduction():
    compressor = TZeroCompressor(line_threshold=5)
    diff_text = """diff --git a/app.py b/app.py
index 1234..5678 100644
--- a/app.py
+++ b/app.py
@@ -10,7 +10,7 @@
 line1
 line2
 line3
 line4
 line5
-old_val = 1
+new_val = 2
 line6
 line7
 line8"""
    reduced = compressor.reduce_code(diff_text, lang="diff")
    assert "+new_val = 2" in reduced
    assert "-old_val = 1" in reduced
    assert "[unmodified context pruned]" in reduced


@pytest.mark.asyncio
async def test_model_manager_tzero_preflight():
    from unittest.mock import AsyncMock, MagicMock
    from src.models.manager import ModelManager
    from src.models.types import TokenUsage, ModelResponse

    mgr = ModelManager()
    mock_prov = MagicMock()
    mock_prov.health_check = AsyncMock(return_value=True)
    mock_prov.available_models = MagicMock(return_value=["test-model"])
    mock_prov.generate = AsyncMock(return_value=ModelResponse(
        content="Response received",
        model_used="test-model",
        provider="mock",
        usage=TokenUsage(prompt_tokens=50, completion_tokens=10, total_tokens=60)
    ))

    mgr.providers = {"mock": mock_prov}
    mgr.fallback_order = ["mock"]
    mgr.active_provider = "mock"
    mgr.active_model = "test-model"

    long_code = """```python
class VeryLongService:
    def method_one(self):
        a = 1
        b = 2
        return a + b
    def method_two(self):
        c = 3
        d = 4
        return c * d
    def method_three(self):
        e = 5
        f = 6
        return e - f
```"""
    req = ModelRequest(messages=[Message(role="user", content=f"Review this code:\n{long_code}")])
    resp = await mgr.generate(req)

    assert resp.content == "Response received"
    # Verify the message that was sent to the provider was compressed by T-Zero
    call_args = mock_prov.generate.call_args[0][0]
    sent_content = call_args.messages[0].content
    assert "class VeryLongService:" in sent_content
    assert "return a + b" not in sent_content  # Body pruned to ...

