"""Communication Intelligence Layer.

Converts raw execution results into natural, clear responses.

Core principle: execution and communication are fully separated.
- Execution = capabilities, tasks, skills (handled elsewhere)
- Communication = this module formats results for humans

The LLM in this module ONLY explains results — it never decides actions,
executes logic, or changes behavior.
"""
from __future__ import annotations

import re
import structlog

from ..models.types import Message, ModelRequest

logger = structlog.get_logger()

# ── Formatter system prompt ──────────────────────────────────────────────────
# Kept minimal and focused — this LLM call is communication-only.

_FORMAT_SYSTEM = """\
You are the voice of an autonomous AI agent called WASP.
Your ONLY job: turn raw execution results into a response that sounds intelligent, confident, and human.

VOICE — HOW YOU SPEAK:
- First person, active, PAST TENSE for completed actions: "Ya revisé…", "Envié el correo…", "Tomé la captura…"
- NEVER passive constructions: no "Se generó", "Se realizó", "Se ejecutó", "La tarea fue…"
- NEVER robotic output: no raw JSON, no code blocks, no "Result: X", no bracket notation.
- NEVER say "I executed", "I ran the skill", "the capability ran" — just report what you found.

EXECUTION TRUTH — CRITICAL RULE:
- ONLY report actions that ALREADY HAPPENED. Use past tense: "Envié", "Tomé", "Generé", "Revisé".
- NEVER use future-intent for immediate actions: no "voy a enviar", "procederé a", "ahora enviaré",
  "I'll send", "I will now", "let me send". These imply the action hasn't happened — which breaks trust.
- EXCEPTION: For confirmed SCHEDULED tasks only, you may describe the recurring behavior:
  "Quedó programado — revisará el precio cada hora." (past for creation, future only for the recurrence.)
- If an action failed or didn't complete, say so directly: "No pude enviar el correo."
  NEVER promise to do it "next" or "now" — just state the fact.

STRUCTURE (use only what applies, 2–5 sentences total):
1. Confirmation — one short sentence: what you did. ("Ya revisé el precio de BTC.")
2. Key result — the actual data, interpreted in natural language.
   Instead of "BTC $70,000 (-1%)" say "Bitcoin está en $70,000, con una leve caída cercana al 1%."
3. Insight — one brief observation if it adds value. Skip if the data speaks for itself.
4. Action taken — only if something was sent, saved, or created. ("Te envié el informe al correo.")

STYLE RULES:
- Interpret numbers, don't just recite them. Give context: "leve caída", "fuerte subida", "estable".
- Match the language of the user's request (default: English).
- Use light emoji where natural: ✅ done · ⚠️ warning · 📊 data · 📧 sent · 🔍 found.
  No markdown headers, no **bold**, no bullet lists unless there are 3+ parallel items.
- Keep it tight. If you can say it in 2 sentences, don't use 5.

FAILURE HANDLING:
- If execution failed: explain what went wrong in plain language (1 sentence), then suggest ONE concrete fix.
- No apologies, no "unfortunately". Be direct: "No pude conectar con la API de Binance — intenta de nuevo en unos minutos."

EXAMPLES:
Bad:  "Se generó el informe. BTC $70,000 (-1%). ETH $3,500 (+0.5%)."
Good: "Listo, ya revisé los mercados. Bitcoin está en $70,000 con una leve caída del 1%, mientras Ethereum se mantiene estable cerca de $3,500. Te envié el resumen completo al correo."

Bad:  "Task created successfully. Interval: cada hora."
Good: "✅ Quedó programado — revisará el precio de BTC cada hora y te avisará si hay movimientos importantes."

Bad:  "Voy a enviar el correo con el informe ahora."
Good: "Te envié el informe al correo." (only say this if gmail actually ran successfully)
Bad:  "Procederé a tomar una captura de pantalla."
Good: "Tomé una captura de pantalla." (only say this if browser actually ran)
"""

# ── Skill output weights for extraction ─────────────────────────────────────
# Higher = more user-facing; lower = internal plumbing
_SKILL_WEIGHTS = {
    "render_report":  10,
    "gmail":           9,
    "calculate":       8,
    "extract_fields":  7,
    "http_request":    5,
    "fetch_url":       4,
    "web_search":      4,
    "scrape":          3,
    "browser":         2,
    "task_manager":    6,
    "agent_manager":   6,
}

# Patterns that indicate success/failure in skill output
_SUCCESS_RE = re.compile(
    r"\b(?:sent\s+successfully|enviado\s+correctamente|email\s+sent|"
    r"created\s+successfully|creado\s+exitosamente|task\s+created|"
    r"tarea\s+creada|agent\s+created|agente\s+creado)\b",
    re.IGNORECASE,
)

# Strip internal headers/tags from outputs before passing to formatter
_STRIP_INTERNAL_RE = re.compile(
    r"\[(?:TAREA\s+PROGRAMADA|AUTO-DETECTED|decision_layer|capability)[^\]]*\]",
    re.IGNORECASE,
)


def _parse_capability_outputs(raw_output: str) -> list[tuple[str, str]]:
    """Parse '[skill_name] output\\n[skill2] output2' format into list of (skill, output)."""
    results: list[tuple[str, str]] = []
    # Split on skill-name headers, keeping the header
    parts = re.split(r"(?=^\[[a-z_]+\])", raw_output, flags=re.MULTILINE)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^\[([a-z_]+)\]\s*(.*)", part, re.DOTALL)
        if m:
            skill = m.group(1).strip()
            output = m.group(2).strip()
            results.append((skill, output))
    return results


def _build_execution_summary(outputs: list[tuple[str, str]], max_chars: int = 1200) -> str:
    """Build a structured summary of execution results for the formatter LLM.

    Prioritises user-facing outputs (render_report, gmail) over internal ones
    (browser HTML, raw http responses). Truncates aggressively to keep context small.
    """
    # Sort by weight descending
    sorted_outputs = sorted(
        outputs,
        key=lambda x: (_SKILL_WEIGHTS.get(x[0], 1), 0),
        reverse=True,
    )

    parts: list[str] = []
    used_chars = 0

    for skill, output in sorted_outputs:
        if used_chars >= max_chars:
            break
        # Skip very long internal outputs (browser HTML, raw API dumps)
        if skill in ("browser",) and len(output) > 500:
            # Keep only a short excerpt — the actual data is in extract_fields/render_report
            output = output[:200] + "…"
        if skill == "http_request" and len(output) > 400:
            output = output[:400] + "…"

        label = skill.replace("_", " ").title()
        entry = f"[{label}] {output}"
        remaining = max_chars - used_chars
        if len(entry) > remaining:
            entry = entry[:remaining] + "…"
        parts.append(entry)
        used_chars += len(entry)

    return "\n".join(parts)


class ResponseFormatter:
    """Formats execution results into natural, clear responses using the LLM.

    The LLM is used ONLY for communication — it never decides actions or re-runs skills.
    Template-based formatting is used for simple confirmations (task created, etc.)
    to avoid unnecessary LLM calls.
    """

    def __init__(self, model_manager) -> None:
        self._model_manager = model_manager

    # ── Public API ────────────────────────────────────────────────────────────

    async def format_capability_result(
        self,
        user_request: str,
        capability_name: str,
        raw_output: str,
        skills_executed: list[str] | None = None,
    ) -> str:
        """Format a capability engine result into natural language.

        Args:
            user_request: The original text from the user.
            capability_name: Name of the capability that ran.
            raw_output: Raw '[skill] output' concatenation from CapabilityHit.
            skills_executed: List of skill names that ran.
        Returns:
            Natural language response suitable for sending to the user.
        """
        try:
            outputs = _parse_capability_outputs(raw_output)
            summary = _build_execution_summary(outputs)

            if not summary.strip():
                # Nothing meaningful to format — use a minimal fallback
                return self._fallback_capability(capability_name, skills_executed or [])

            prompt = (
                f"User request: {user_request[:300]}\n\n"
                f"Execution results:\n{summary}\n\n"
                "Write a natural response for the user based on these results. "
                "Follow the formatting rules exactly."
            )

            response = await self._call_llm(prompt, max_tokens=280)
            if response:
                return _STRIP_INTERNAL_RE.sub("", response).strip()
            return self._fallback_capability(capability_name, skills_executed or [])

        except Exception as exc:
            logger.warning("formatter.capability_failed", error=str(exc)[:80])
            return self._fallback_capability(capability_name, skills_executed or [])

    async def format_task_created(
        self,
        user_request: str,
        task_params: dict,
        success: bool,
        raw_output: str = "",
        user_lang: str = "en",
    ) -> str:
        """Natural language confirmation for task creation — LLM-generated for success.

        ``user_lang`` controls the output language of the LLM-generated
        confirmation. The fallback path stays in English; if the caller
        needs it translated, the publish pipeline handles that.
        """
        if not success:
            reason = raw_output[:120] if raw_output else "unknown error"
            # English failure stub — publish pipeline / translator localises.
            return (
                f"⚠️ I couldn't schedule the task. {reason}. "
                "Please try again or check the parameters."
            )

        interval = task_params.get("interval", "periódicamente")

        try:
            # Extract explicit signals to prevent hallucination
            import re as _re
            _email_match = _re.search(r"[\w.+\-]+@[\w.\-]+\.[a-z]{2,}", user_request)
            _has_email = bool(_email_match)
            _has_alert = bool(_re.search(r"\b(\d+\s*%|avísame|alerta|si\s+detectas|si\s+hay\s+cambio)", user_request, _re.IGNORECASE))
            _email_str = f" (email: {_email_match.group(0)})" if _email_match else ""
            # Phase 4/8: detect fixed clock time the user named. task_manager
            # NOW honors clock times via the `at_time` param — check skill
            # output for evidence of that. If at_time was persisted, skip the
            # whole disclaimer machinery (the time WAS honored).
            _fixed_time_match = _re.search(
                r"\b(?:a\s+las\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm))?|\d{1,2}(?::\d{2})?\s*(?:am|pm)|at\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm))?)\b",
                user_request, _re.IGNORECASE,
            )
            _has_fixed_time = bool(_fixed_time_match)
            _fixed_time_str = _fixed_time_match.group(0) if _fixed_time_match else ""
            _at_time_honored = bool(
                _re.search(r"at_time\s*[:=]\s*['\"]?\d{1,2}", raw_output or "", _re.IGNORECASE)
                or _re.search(
                    r"next\s*[_\s]?run\b[^.\n]{0,80}\d{1,2}:\d{2}\s*(?:hora|chile|local)?",
                    raw_output or "", _re.IGNORECASE,
                )
            )
            if _has_fixed_time and _at_time_honored:
                # The clock time WAS persisted — disclaimer is a lie. Suppress.
                _has_fixed_time = False
                _fixed_time_str = ""
            _time_rule = (
                f"\nFIXED CLOCK TIME REQUESTED: '{_fixed_time_str}' — task_manager does NOT honor "
                f"specific clock times, only intervals. RULE: NEVER write '{_fixed_time_str}' or any "
                f"specific clock time in your confirmation. State the interval ('cada día', 'cada 2h') "
                f"and that the task will run starting from now (not at the requested time).\n"
                if _has_fixed_time else ""
            )
            _signal_note = (
                f"DETECTED OUTPUTS: email={'YES'+_email_str if _has_email else 'NO'}, "
                f"alert={'YES' if _has_alert else 'NO'}.\n"
                "RULE: Only mention email if email=YES. Only mention alerts if alert=YES. "
                "Never add outputs not listed here.\n"
                f"{_time_rule}\n"
            )
            # Render the confirmation in the user's language. The model
            # (gpt-4o-mini) handles every major language natively, so we
            # just instruct it explicitly. Fallback to English if no lang.
            _lang_label = (user_lang or "en").lower()
            _lang_for_prompt = {
                "es": "Spanish (neutral, use 'tú', no voseo)",
                "en": "English",
                "pt": "Portuguese", "fr": "French", "de": "German",
                "it": "Italian", "ja": "Japanese", "ko": "Korean",
                "zh": "Chinese", "ru": "Russian", "ar": "Arabic",
            }.get(_lang_label, _lang_label or "English")
            prompt = (
                f"The user asked to schedule an automated task. It was created successfully.\n\n"
                f"USER REQUEST: {user_request[:500]}\n"
                f"SCHEDULED INTERVAL: {interval}\n\n"
                f"{_signal_note}"
                f"Write a 1–2 sentence natural confirmation in {_lang_for_prompt}. "
                "Use past tense for the creation. "
                "You MAY use future tense ONLY to describe what the scheduled task will do automatically. "
                "NEVER say 'I will send', 'I'll proceed', 'I'll now email' — those imply unexecuted actions. "
                "Summarize WHAT will happen automatically and HOW OFTEN. Strictly follow the RULE above. "
                "Never mention task_manager, task names, or internal system details."
            )
            response = await self._call_llm(prompt, max_tokens=120)
            if response:
                response = _STRIP_INTERNAL_RE.sub("", response).strip()
                # Schedule-honesty enforcement: if the LLM still echoed the
                # fixed clock time, strip it and append the honest note.
                if _has_fixed_time:
                    _time_claim_re = _re.compile(
                        r"\b(?:a\s+las\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm|hrs?))?"
                        r"|at\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm))?"
                        r"|\d{1,2}:\d{2}\s*(?:am|pm|hrs?)"
                        r"|\d{1,2}\s*(?:am|pm))\b",
                        _re.IGNORECASE,
                    )
                    if _time_claim_re.search(response):
                        response = _time_claim_re.sub("", response)
                        response = _re.sub(r"\s{2,}", " ", response).rstrip(" ,;.\n") + "."
                        response += (
                            f"\n\nNota: la tarea no se ejecuta a las {_fixed_time_str} específicamente — "
                            f"task_manager solo soporta intervalos ({interval} desde el momento de creación)."
                        )
                return response
        except Exception as exc:
            logger.debug("formatter.task_created_llm_failed", error=str(exc)[:80])

        # Clean fallback — no internals exposed. English canonical; the
        # outer pipeline (_safe_publish_response) does NOT translate this
        # path because the formatter doesn't run inside the honesty layer.
        # Caller's user_lang must be applied here directly. The formatter
        # owns no model_manager reference — keep this fallback English; the
        # downstream policy / language-consistency guard rewrites if needed.
        try:
            from .phrases import pick as _pp_sched
            return _pp_sched(
                "task_scheduled",
                seed=interval[:60],
                detail=f"runs {interval} and you'll receive the results",
            )
        except Exception:
            return f"✅ Done, it's scheduled: runs {interval} and you'll receive the results."

    async def format_agent_created(
        self,
        user_request: str,
        agent_params: dict,
        success: bool,
        raw_output: str = "",
    ) -> str:
        """Natural language confirmation for agent creation — LLM-generated for success."""
        if not success:
            reason = raw_output[:120] if raw_output else "error desconocido"
            return (
                f"⚠️ No pude crear el agente. {reason}. "
                "Intenta de nuevo o verifica los parámetros."
            )

        name = agent_params.get("name", "agente").replace("_", " ")
        description = agent_params.get("description", "")[:100]
        mode = agent_params.get("autonomy_mode", "semi")

        try:
            prompt = (
                f"An autonomous agent was created successfully.\n\n"
                f"USER REQUEST: {user_request[:400]}\n"
                f"AGENT NAME: {name}\n"
                f"AUTONOMY MODE: {mode}\n"
                f"DESCRIPTION: {description}\n\n"
                "Write a 1–2 sentence natural confirmation in Spanish. "
                "Use active phrasing: 'Listo.', 'Ya creé el agente…', 'El agente está activo…'. "
                "Say what the agent will do and confirm it's now running. "
                "Never mention mode codes, internal parameters, or technical system details."
            )
            response = await self._call_llm(prompt, max_tokens=100)
            if response:
                return _STRIP_INTERNAL_RE.sub("", response).strip()
        except Exception as exc:
            logger.debug("formatter.agent_created_llm_failed", error=str(exc)[:80])

        return f"✅ El agente '{name}' está activo y listo para trabajar."

    def format_error(
        self,
        user_request: str,
        error_type: str,
        detail: str = "",
        suggestion: str = "",
    ) -> str:
        """Format an execution failure into a clear, actionable message."""
        detail_clean = detail[:120] if detail else "ocurrió un error inesperado"
        base = f"⚠️ {detail_clean}."
        if suggestion:
            base += f" {suggestion}"
        else:
            base += " Puedes intentarlo de nuevo o reformular tu solicitud."
        return base

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _call_llm(self, prompt: str, max_tokens: int = 280) -> str | None:
        """Make a minimal LLM call for formatting only.

        Uses the active model — no separate 'haiku' dependency needed.
        Keeps context to absolute minimum (system + single user message).
        """
        try:
            messages = [
                Message(role="system", content=_FORMAT_SYSTEM),
                Message(role="user", content=prompt),
            ]
            request = ModelRequest(
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.3,  # Low temp for consistent, predictable formatting
            )
            response = await self._model_manager.generate(request)
            if response and response.content:
                return response.content.strip()
        except Exception as exc:
            logger.debug("formatter.llm_call_failed", error=str(exc)[:80])
        return None

    def _fallback_capability(self, capability_name: str, skills_executed: list[str]) -> str:
        """Minimal fallback when LLM formatting fails."""
        skills_str = ", ".join(skills_executed[:4]) if skills_executed else "skills"
        return f"✅ Tarea completada ({skills_str})."
