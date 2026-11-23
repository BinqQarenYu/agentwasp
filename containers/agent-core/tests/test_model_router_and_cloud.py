import pytest
from src.models.router import is_model_fit_for_host_pc, classify_task_hardened
from src.models.quota_guard import quota_guard, DEFAULT_QUOTAS


def test_is_model_fit_for_host_pc():
    # Local lightweight models should fit
    fit, _ = is_model_fit_for_host_pc("llama3.2:3b")
    assert fit is True

    # Local oversized models (>14B) should be rejected to protect host RAM
    fit_heavy, msg = is_model_fit_for_host_pc("llama3.3:70b")
    assert fit_heavy is False
    assert "exceeds host PC hardware limits" in msg

    fit_32b, _ = is_model_fit_for_host_pc("qwen2.5:32b")
    assert fit_32b is False

    # Ollama Cloud models should bypass local RAM checks since they run remotely
    fit_cloud1, msg_cloud1 = is_model_fit_for_host_pc("glm-4.7:cloud")
    assert fit_cloud1 is True
    assert "Cloud-hosted model runs remotely" in msg_cloud1

    fit_cloud2, msg_cloud2 = is_model_fit_for_host_pc("nemotron-3-super:cloud")
    assert fit_cloud2 is True
    assert "Cloud-hosted model runs remotely" in msg_cloud2


def test_classify_task_hardened():
    assert classify_task_hardened("Check my balance and calculate invoice tax for $500") == "financial_math_high_risk"
    assert classify_task_hardened("Audit this smart contract for re-entrancy vulnerabilities") == "deep_engineering_high_risk"
    assert classify_task_hardened("say hello") == "trivial_local_tier"


@pytest.mark.asyncio
async def test_ollama_cloud_quota_entry():
    assert "ollama:cloud" in DEFAULT_QUOTAS
    cfg = DEFAULT_QUOTAS["ollama:cloud"]
    assert cfg["max_rpm"] == 30
    assert cfg["max_rpd"] == 3000
    assert cfg["min_request_interval"] == 1.0

    # Acquire slot shouldn't raise error
    await quota_guard.acquire_slot("ollama:cloud")
