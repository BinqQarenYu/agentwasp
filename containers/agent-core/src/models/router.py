"""Intelligent model router — classify tasks and suggest the optimal model.

Routes based on task type keywords:
- vision: requires image analysis → vision-capable model (GPT-4o, Claude, Gemini, LLaVA)
- code: programming/scripting → best code model
- quick: simple/fast tasks → fastest/cheapest model
- complex: deep analysis → most powerful model
- default: use active model

The router does NOT switch models automatically — it suggests.
The agent can use route_task() to pick the best model for each step.
"""

import re
import structlog

logger = structlog.get_logger()

# Task type patterns
_VISION_PATTERNS = re.compile(
    r"\b(imagen|foto|screenshot|captura|chart|gráfico|grafico|picture|image|visual|"
    r"photo|ver|mira|muéstrame|muéstra|describe|analiza esta|what do you see|"
    r"what's in|qué hay|qué ves|read this image|lee esta imagen)\b",
    re.IGNORECASE,
)

_CODE_PATTERNS = re.compile(
    r"\b(código|code|script|función|function|clase|class|programa|program|"
    r"python|javascript|typescript|bash|sql|algoritmo|algorithm|debuggear|debug|"
    r"refactorizar|refactor|optimizar el código|implement|implementa|escribe una|"
    r"write a function|create a script|crea un script)\b",
    re.IGNORECASE,
)

_QUICK_PATTERNS = re.compile(
    r"\b(qué hora|que hora|what time|clima|weather|tiempo|temperatura|"
    r"cuánto|cuanto|how much|precio|price|precio actual|convierte|convert|"
    r"traduce|translate|suma|add|multiplica|multiply|cuántos|cuantos|"
    r"quick|rápido|rapido|fast|dame solo|just give me)\b",
    re.IGNORECASE,
)

_COMPLEX_PATTERNS = re.compile(
    r"\b(analiza|analyze|análisis|analysis|investiga|investigate|research|"
    r"explica en detalle|explain in detail|redacta|draft|escribe un ensayo|"
    r"write an essay|plan|estrategia|strategy|diseña|design|arquitectura|"
    r"architecture|comparar|compare|evalúa|evaluate|mejora|improve|optimiza)\b",
    re.IGNORECASE,
)

# Preferred models per task type (ordered by preference)
_VISION_MODELS = [
    "claude-opus-4-6", "claude-sonnet-4-6", "gpt-4o", "gemini-2.0-flash",
    "gemini-1.5-pro", "claude-3-5-sonnet-20241022", "llava:7b", "llava:13b",
    "bakllava", "moondream",
]

_CODE_MODELS = [
    "claude-opus-4-6", "claude-sonnet-4-6", "claude-3-5-sonnet-20241022",
    "gpt-4o", "gpt-4", "deepseek-coder", "codellama", "qwen2.5-coder",
]

_QUICK_MODELS = [
    "gpt-4o-mini", "claude-haiku-4-5-20251001", "claude-3-5-haiku-20241022",
    "gemini-2.0-flash", "grok-beta", "qwen2.5:1.5b", "tinyllama",
]

_COMPLEX_MODELS = [
    "claude-opus-4-6", "gpt-4o", "gemini-1.5-pro", "claude-3-5-sonnet-20241022",
    "gpt-4", "grok-2",
]


def classify_task(text: str) -> str:
    """Classify a task into: vision, code, quick, complex, or default."""
    if _VISION_PATTERNS.search(text):
        return "vision"
    if _CODE_PATTERNS.search(text):
        return "code"
    if _QUICK_PATTERNS.search(text):
        return "quick"
    if _COMPLEX_PATTERNS.search(text):
        return "complex"
    return "default"


def suggest_model(task_type: str, available_models: list[str]) -> str | None:
    """Return the best available model for the task type, or None if no match."""
    preferred_lists = {
        "vision": _VISION_MODELS,
        "code": _CODE_MODELS,
        "quick": _QUICK_MODELS,
        "complex": _COMPLEX_MODELS,
    }
    preferred = preferred_lists.get(task_type, [])
    available_lower = {m.lower(): m for m in available_models}

    for candidate in preferred:
        # Exact match
        if candidate in available_models:
            return candidate
        # Prefix match (e.g. "llava" matches "llava:7b")
        for avail_l, avail_orig in available_lower.items():
            if avail_l.startswith(candidate.lower()) or candidate.lower().startswith(avail_l):
                return avail_orig
    return None


def route_task(text: str, available_models: list[str], active_model: str) -> tuple[str, str]:
    """Route a task to the optimal model.

    Returns (task_type, recommended_model).
    recommended_model is the active_model if no better match found.
    """
    task_type = classify_task(text)
    if task_type == "default":
        return "default", active_model

    recommended = suggest_model(task_type, available_models)
    if recommended and recommended != active_model:
        logger.info(
            "model_router.suggestion",
            task_type=task_type,
            recommended=recommended,
            active=active_model,
        )
    return task_type, recommended or active_model
