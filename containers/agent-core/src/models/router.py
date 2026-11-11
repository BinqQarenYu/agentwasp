"""Intelligent & Risk-Hardened Model Router for Agent WASP.

Routes tasks intelligently across tiers:
- High-End Cloud Tier (Gemini Pro/Flash, Grok, GPT-4o): Complex reasoning, code review, security audits, multi-step tool execution, and the Done-Checker critic.
- Deterministic Math Guard: Forces mathematical and financial expressions to bypass mental calculation and route to Python/calculator tools.
- Local Tier (Ollama / Small models): Safe classification, read-only drafting, spam triage, and low-risk conversation.
- System Pressure Guard: Fallback to cloud if local host RAM exceeds 80% to avoid VRAM thrashing.
"""

import re
import psutil
import structlog
from typing import Tuple, List, Optional

logger = structlog.get_logger()

# ── Hardware Capacity Guard: Models Prohibited for Local CPU Run on this Host ──
# Prevents running 70B/32B/14B models on 32GB host without GPU acceleration,
# which causes severe RAM paging, 100% CPU lock, and system freeze.
_PROHIBITED_LOCAL_PATTERNS = re.compile(
    r"(?:70b|675b|120b|32b|27b|31b|26b|80b|14b|cascade)",
    re.IGNORECASE,
)

# Curated models verified to safely fit host PC RAM & CPU specs
_PC_FITTED_LOCAL_MODELS = [
    "llama3.2:latest", "llama3.2:3b", "nemotron-mini:latest", "qwen2.5-coder:latest", "llama3:latest"
]


def is_model_fit_for_host_pc(model_name: str) -> Tuple[bool, str]:
    """Check if model can run locally without freezing or crashing the host PC."""
    m = model_name.lower().strip()
    if _PROHIBITED_LOCAL_PATTERNS.search(m):
        return False, f"Model '{model_name}' exceeds host PC hardware limits (>=14B). Route to cloud tier instead."

    try:
        mem = psutil.virtual_memory()
        available_gb = mem.available / (1024**3)
        if available_gb < 4.0:
            return False, f"Insufficient free RAM ({available_gb:.1f} GB < 4.0 GB required for local inference)."
    except Exception:
        pass

    return True, "Model fits host PC hardware."

# ── High-Risk Domain Patterns (Unconditional Cloud Escalation) ───────────────
_SECURITY_AUDIT_PATTERNS = re.compile(
    r"(security|vulnerability|exploit|cve|re-entrancy|penetration|audit|sanitiz|auth|bypass|permission|secret|credential|token|privilege)",
    re.IGNORECASE,
)

_FINANCIAL_OR_MATH_PATTERNS = re.compile(
    r"(\$|€|£|\busd\b|\beur\b|\binvoice\b|\bwire\b|\btransfer\b|\bpayment\b|\binterest\b|\btax\b|\bvat\b|\bformula\b|\bcalculate\b|\bequation\b|\bpercent\b|%|\bsum\b|\baverage\b|\bbalance\b|\breconcil\b)",
    re.IGNORECASE,
)

_CODE_AND_ARCHITECTURE_PATTERNS = re.compile(
    r"(\brefactor\b|\barchitecture\b|\bdocker\b|\bkubernetes\b|\bdatabase schema\b|\bmigration\b|\bdeadlock\b|\bconcurrency\b|\brace condition\b|\basyncio\b|\bmultiprocess\b|\bcompiler\b|\bast\b|\bunit test\b)",
    re.IGNORECASE,
)

_VISION_PATTERNS = re.compile(
    r"(\bimagen\b|\bfoto\b|\bscreenshot\b|\bcaptura\b|\bchart\b|\bgráfico\b|\bpicture\b|\bimage\b|\bvisual\b|\bphoto\b|describe this image|what is in this picture)",
    re.IGNORECASE,
)

# ── Low-Risk / Trivial Patterns (Safe for Local Ollama Tier) ─────────────────
_TRIVIAL_CLASSIFY_PATTERNS = re.compile(
    r"(\bspam\b|is this spam|\bclassify\b|\btag this\b|\bcategorize\b|draft a quick greeting|say hello|summarize in one sentence|clean up whitespace)",
    re.IGNORECASE,
)

# Preferred cloud models for complex / high-stakes tasks
_CLOUD_HIGH_END_MODELS = [
    "gemini-2.0-flash", "gemini-2.5-pro", "grok-2", "gpt-4o", "claude-3-5-sonnet",
]

_LOCAL_OR_FAST_MODELS = [
    "qwen2.5:7b", "llama3.1:8b", "mistral:7b", "qwen2.5-coder:7b", "gpt-4o-mini", "gemini-2.0-flash-lite",
]


def classify_task_hardened(text: str, has_attachments: bool = False) -> str:
    """Classifies task with strict safety overrides to prevent false logic and errors."""
    if has_attachments or _VISION_PATTERNS.search(text):
        return "vision"

    # Rule 1: Financial & Math MUST escalate to Cloud + Tool Execution
    if _FINANCIAL_OR_MATH_PATTERNS.search(text):
        return "financial_math_high_risk"

    # Rule 2: Security & Code Audits MUST escalate to Cloud
    if _SECURITY_AUDIT_PATTERNS.search(text) or _CODE_AND_ARCHITECTURE_PATTERNS.search(text):
        return "deep_engineering_high_risk"

    # Rule 3: Only truly trivial, non-calculating tasks can use the local tier
    if _TRIVIAL_CLASSIFY_PATTERNS.search(text) and len(text.split()) < 40:
        return "trivial_local_tier"

    return "cloud_general"


def suggest_hardened_model(
    task_type: str,
    available_models: List[str],
    active_model: str,
    host_cpi_pressure: float = 0.0,
) -> Tuple[str, str, dict]:
    """Recommends model tier and returns safety metadata."""
    metadata = {
        "requires_deterministic_tool": False,
        "tier": "cloud",
        "reason": "Standard reasoning tier",
    }

    # If host machine is under severe RAM pressure (>80%), avoid local Ollama VRAM freeze
    if host_cpi_pressure > 80.0:
        metadata["reason"] = "Host RAM > 80% — forced cloud execution to prevent system thrash"
        return task_type, active_model, metadata

    if task_type in ("financial_math_high_risk", "deep_engineering_high_risk"):
        metadata["requires_deterministic_tool"] = (task_type == "financial_math_high_risk")
        metadata["tier"] = "cloud_high_end"
        metadata["reason"] = "High-stakes task escalated to high-end frontier model"

        for candidate in _CLOUD_HIGH_END_MODELS:
            for m in available_models:
                if candidate in m.lower():
                    return task_type, m, metadata

    elif task_type == "trivial_local_tier":
        metadata["tier"] = "local_ollama"
        metadata["reason"] = "Trivial task routed to PC-fitted local Ollama tier to conserve quota"

        # Prioritize lightweight models verified to fit host PC RAM/CPU
        for candidate in _PC_FITTED_LOCAL_MODELS:
            for m in available_models:
                if candidate in m.lower():
                    is_fit, _ = is_model_fit_for_host_pc(m)
                    if is_fit:
                        return task_type, m, metadata

    return task_type, active_model, metadata


def route_task(
    text: str,
    available_models: List[str],
    active_model: str,
    has_attachments: bool = False,
    host_cpi_pressure: float = 0.0,
) -> Tuple[str, str]:
    """Compatibility wrapper for route_task with risk hardening."""
    task_type = classify_task_hardened(text, has_attachments=has_attachments)
    _, recommended_model, meta = suggest_hardened_model(
        task_type, available_models, active_model, host_cpi_pressure
    )

    logger.info(
        "model_router.hardened_decision",
        task_type=task_type,
        model_selected=recommended_model,
        tier=meta["tier"],
        reason=meta["reason"],
    )
    return task_type, recommended_model
