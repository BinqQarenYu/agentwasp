"""Structured side-effect announcements.

The reliability gap WASP had was: the LLM could narrate any action it wanted
("I will send the email", "I scheduled it", "let me set that up") in free
text, regardless of whether the underlying skill actually ran. The previous
defense was a regex scrubber for known phrasings — fragile against drift.

This module replaces "scrub anything that looks like a side-effect claim"
with two complementary mechanisms:

  1. **Structured ACTIONS block**: derived deterministically from the
     skill_results of the turn. Always rendered when ≥1 side-effect skill
     ran. The single source of truth for "what actually happened."

  2. **Action-claim verification**: detects first-person action claims in
     the LLM's free text and strips any claim whose corresponding skill
     did NOT successfully run this turn. Generalizes the older two-pattern
     scrubber across phrasings.

Together: free text only contains verified claims; the structured block is
authoritative; an unauthorized announcement cannot survive.

The system prompt (prime.md §5.2) tells the LLM not to narrate side-effects
in free text. This module enforces it deterministically, regardless of
whether the LLM complies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


# ── Action verb taxonomy ─────────────────────────────────────────────────
# Each entry maps a verb family → the skill whose successful execution
# would back a claim using that verb. The detector finds first-person
# claims; the verifier checks the relevant skill ran.

@dataclass(frozen=True)
class ActionFamily:
    name: str               # internal id, e.g. "email_send"
    skill: str              # skill_name in skill_results to check
    success_marker: str     # output substring that confirms success
    verb_pattern: re.Pattern  # matches first-person claim sentences
    failure_keywords: tuple = ()  # error/output substrings that confirm THIS
                                  # family failed (not a sibling operation
                                  # like delete_all or list). Without a match
                                  # we suppress the FAILED record to avoid
                                  # phantom "task_create failed" on delete.


# Email send claims — Spanish + English, all tenses.
# Uses (?i) only (NOT x) so embedded literal spaces are kept significant.
# Note: "I'll" has no space between I and 'll, so the apostrophe form is its
# own alternative; "I will" requires whitespace.
_VERB_EMAIL = re.compile(
    r"(?i)\b(?:"
    r"(?:te\s+)?(?:lo\s+)?(?:env[íi][aáoeé](?:r|s|mos|ron|ré|rá|ría)?|mand[aáoeéó](?:r|s|mos|ron|ré|rá|ría)?|envio|envia|mando|manda)"
    r"\s+(?:.{0,40}?)?(?:correo|email|e-?mail|gmail|inbox|por\s+correo|via\s+email)|"
    r"voy\s+a\s+(?:enviar|mandar)(?:\s+.{0,40}?)?(?:correo|email|gmail)?|"
    r"procedo\s+a\s+(?:enviar|mandar)(?:lo|la)?|"
    # English: "I'll email you", "I will send", "I'm going to email"
    r"I(?:\s*'ll|\s+will|\s+have|\s*'ve|\s+am\s+going\s+to)\s+(?:send|email|forward|deliver)\b|"
    r"sending\s+(?:you\s+)?(?:this|the\s+\w+)\s+(?:by|via)\s+email|"
    r"now\s+I(?:\s*'ll|\s+will)?\s+(?:send|email)"
    r")\b"
)

# Task creation claims.
_VERB_TASK = re.compile(
    r"(?ix)\b(?:"
    # past / present-perfect ES
    r"(?:he|ya)\s+(?:programad[oa]|program[éeo]|cre[éeo]|cread[oa])\s+(?:una\s+|el\s+|la\s+)?(?:tarea|recordatorio\s+recurrente)|"
    # tarea X-ada/ado
    r"(?:tarea|task|recordatorio\s+recurrente)\s+(?:program(?:ada|ado|ed)|cread[ao]|scheduled|created)|"
    r"qued[óo]\s+programad[oa]|"
    # future ES — "crearé/programaré la tarea"
    r"(?:cre(?:ar[ée]|ar[áa]|o|ando)|program(?:ar[ée]|ar[áa]|o|ando))\s+(?:la\s+|una\s+|el\s+)?(?:tarea|recordatorio)|"
    r"voy\s+a\s+(?:crear|programar)\s+(?:la|una|el)?\s*(?:tarea|recordatorio)|"
    # past / future EN
    r"(?:I|i)\s+(?:have|'ve)?\s*(?:scheduled|created|set\s+up)\s+(?:a|the)\s+(?:task|recurring|reminder)|"
    r"I'?ll\s+(?:schedule|create|set\s+up)\s+(?:a|the)\s+(?:task|recurring|reminder)"
    r")\b"
)

# Agent creation claims.
_VERB_AGENT = re.compile(
    r"(?ix)\b(?:"
    r"(?:he\s+)?cre(?:ado|é)\s+(?:un|el|la)\s+(?:sub-?)?agente|"
    r"agente\s+(?:cread[oa]|nuevo)\s+(?:exitosamente|correctamente)|"
    r"(?:I|i)\s+(?:have|'ve)?\s*created\s+(?:a|the|an)\s+(?:sub-?)?agent|"
    r"agent\s+(?:created|set\s+up)|"
    r"voy\s+a\s+crear\s+(?:un|el)\s+(?:sub-?)?agente|"
    r"I'?ll\s+create\s+(?:a|the|an)\s+(?:sub-?)?agent"
    r")\b"
)

ACTION_FAMILIES = (
    # gmail: success_marker is empty so gmail.success=True is enough; failure
    # keywords specific to send/draft to avoid mislabeling read/search errors.
    ActionFamily(
        name="email_send", skill="gmail",
        success_marker="",
        verb_pattern=_VERB_EMAIL,
        failure_keywords=("send", "smtp", "deliver", "envío", "enviar", "to:", "recipient"),
    ),
    # task_create: only flag failed when error mentions create/duplicate.
    # Prevents phantom "task_create failed" on delete_all / trigger errors.
    ActionFamily(
        name="task_create", skill="task_manager",
        success_marker="Task created",
        verb_pattern=_VERB_TASK,
        failure_keywords=("create", "duplicate", "already exists", "schedule", "recurring",
                          "creación", "crear", "duplicada"),
    ),
    # agent_create: same reasoning.
    ActionFamily(
        name="agent_create", skill="agent_manager",
        success_marker="created",
        verb_pattern=_VERB_AGENT,
        failure_keywords=("create", "agent", "agente", "creación"),
    ),
)


@dataclass
class ActionRecord:
    family: str          # email_send | task_create | agent_create
    skill: str
    status: str          # "success" | "blocked" | "failed"
    summary: str         # short user-readable line


# ── Detection ────────────────────────────────────────────────────────────


def _skill_succeeded(skill_results, family: ActionFamily) -> bool:
    if not skill_results:
        return False
    # Phase 4/8: task_create succeeds for both fresh creation and dedup hit.
    _task_success_phrases = ("task created", "already exists", "not creating a duplicate")
    for r in skill_results:
        if (getattr(r, "skill_name", "") or "").lower() != family.skill:
            continue
        if not getattr(r, "success", False):
            continue
        if not family.success_marker:
            return True
        out_l = (getattr(r, "output", "") or "").lower()
        if family.success_marker.lower() in out_l:
            return True
        # Family-specific permissive matchers — dedup is a successful no-op.
        if family.name == "task_create" and any(p in out_l for p in _task_success_phrases):
            return True
    return False


def _skill_attempted(skill_results, family: ActionFamily) -> tuple[bool, str]:
    """(attempted, last_error) — used to mark FAILED in the structured block.

    Conservative: returns attempted=True only when an error message contains
    one of the family's failure_keywords. Without that match we cannot tell
    whether the failure was for this family's action or a sibling op
    (e.g. task_manager.delete_all returning an error must NOT be reported as
    "task_create failed"). Better silent than misleading.
    """
    if not skill_results:
        return False, ""
    err = ""
    for r in skill_results:
        if (getattr(r, "skill_name", "") or "").lower() != family.skill:
            continue
        if getattr(r, "success", False):
            continue  # success handled by _skill_succeeded
        err = getattr(r, "error", "") or err
    if not err:
        return False, ""
    if not family.failure_keywords:
        # Skill has no failure keywords defined — fall back to "any error counts"
        return True, err
    err_low = err.lower()
    if any(kw in err_low for kw in family.failure_keywords):
        return True, err
    return False, ""


# ── Failure-narrative scrubbing (when action actually succeeded) ─────────
#
# When goal_orchestrator returns a partial-failure narrative ("No pude
# completar la tarea" / "Could not complete") AND the action announcer
# detects that the specific action DID succeed in this turn, the failure
# narrative is contradictory and must be stripped. Otherwise users see:
#
#   "Could not complete the task. ...  Acciones: Email sent."
#
# which is worse than either alone — they can't tell what really happened.

_FAILURE_PHRASES_BY_FAMILY = {
    "email_send": re.compile(
        r"(?i)\b(?:"
        r"no\s+pude\s+(?:enviar|mandar|completar)\s+(?:el\s+)?(?:correo|email|mensaje|tarea)|"
        r"could\s+not\s+(?:send|deliver|complete)\s+(?:the\s+)?(?:email|message|task)|"
        r"failed\s+to\s+send|"
        r"el\s+sitio\s+no\s+devolvi[óo]\s+resultados|"
        r"posible\s+protecci[óo]n\s+anti-?bot|"
        r"no\s+pude\s+completar\s+la\s+tarea\s+en\s+el\s+sitio|"
        # Retry-offer phrases that imply failure when the email actually went
        r"[¿?]?\s*quieres\s+que\s+lo\s+intente\s+con\s+un\s+m[ée]todo\s+alternativo|"
        r"[¿?]?\s*do\s+you\s+want\s+(?:me\s+)?to\s+try\s+(?:an\s+)?alternative"
        r")[^.!?]*[.!?]"
    ),
    "task_create": re.compile(
        r"(?i)\b(?:"
        r"no\s+pude\s+crear\s+(?:la\s+)?tarea|"
        r"could\s+not\s+create\s+(?:the\s+)?(?:task|recurring)|"
        r"failed\s+to\s+create\s+(?:the\s+)?task"
        r")[^.!?]*[.!?]"
    ),
    "agent_create": re.compile(
        r"(?i)\b(?:"
        r"no\s+pude\s+crear\s+(?:el\s+)?(?:sub-?)?agente|"
        r"could\s+not\s+create\s+(?:the\s+)?(?:sub-?)?agent"
        r")[^.!?]*[.!?]"
    ),
}


def strip_contradicting_failures(
    response_text: str, skill_results,
) -> tuple[str, list[str]]:
    """Remove failure narratives for families that actually succeeded.
    Returns (cleaned_text, families_scrubbed)."""
    if not response_text:
        return response_text, []
    text = response_text
    scrubbed: list[str] = []
    for fam in ACTION_FAMILIES:
        if not _skill_succeeded(skill_results, fam):
            continue
        pat = _FAILURE_PHRASES_BY_FAMILY.get(fam.name)
        if not pat:
            continue
        if pat.search(text):
            text = pat.sub("", text)
            scrubbed.append(fam.name)
    if scrubbed:
        text = re.sub(r"\s{2,}", " ", text).strip(" ,;.\n")
    return text, scrubbed


# ── Free-text claim stripping ────────────────────────────────────────────


def strip_unverified_claims(
    response_text: str, skill_results,
) -> tuple[str, list[str]]:
    """Remove sentences containing first-person action claims that no
    successful skill_result backs. Returns (cleaned_text, families_stripped)."""
    if not response_text:
        return response_text, []
    text = response_text
    stripped: list[str] = []

    # Phase 4/8: when this turn was a task_create (or task already exists),
    # the response legitimately describes future side effects of the task
    # (e.g. "every day will email you weather"). Don't strip those — they
    # describe what the SCHEDULED task will do, not unauthorized actions.
    _was_task_create_turn = False
    for r in skill_results or []:
        try:
            if (getattr(r, "skill_name", "") or "").lower() != "task_manager":
                continue
            if not getattr(r, "success", False):
                continue
            out = (getattr(r, "output", "") or "")
            if "Task created:" in out or "already exists" in out or "Not creating a duplicate" in out:
                _was_task_create_turn = True
                break
        except Exception:
            continue

    for fam in ACTION_FAMILIES:
        if _skill_succeeded(skill_results, fam):
            continue  # claim is verified, leave it
        # Skip future-action descriptions when this turn scheduled a task
        if _was_task_create_turn and fam.name in ("email_send", "agent_create"):
            continue
        if not fam.verb_pattern.search(text):
            continue
        # Strip every sentence that contains the claim. A sentence is
        # delimited by . / ! / ? / line breaks.
        sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
        kept: list[str] = []
        any_drop = False
        for s in sentences:
            if fam.verb_pattern.search(s):
                any_drop = True
                continue
            kept.append(s)
        if any_drop:
            text = " ".join(p for p in kept if p.strip())
            stripped.append(fam.name)

    # Collapse whitespace + trim trailing orphan punctuation ONLY when a
    # claim was actually removed; otherwise return the text unchanged so we
    # don't mangle natural punctuation in pure Q&A responses.
    if stripped:
        text = re.sub(r"\s{2,}", " ", text).strip(" ,;.\n")
    return text, stripped


# ── Structured block ─────────────────────────────────────────────────────


def collect_actions(skill_results, lang: str = "en") -> list[ActionRecord]:
    """Build deterministic ActionRecord list from skill_results."""
    out: list[ActionRecord] = []
    for fam in ACTION_FAMILIES:
        if _skill_succeeded(skill_results, fam):
            out.append(ActionRecord(
                family=fam.name, skill=fam.skill, status="success",
                summary=_summarize(fam, skill_results, "success", lang=lang),
            ))
            continue
        attempted, err = _skill_attempted(skill_results, fam)
        if attempted:
            out.append(ActionRecord(
                family=fam.name, skill=fam.skill, status="failed",
                summary=_summarize(fam, skill_results, "failed", err, lang=lang),
            ))
    return out


def _summarize(fam: ActionFamily, skill_results, status: str, err: str = "", lang: str = "en") -> str:
    """One-line user-readable summary for an action.

    B8 fix: returns canonical English on success/failed verbs. Translation
    happens via apply_final_response_policy → response_binding's translator
    OR via the inline localised lookup below for the most common ES path.
    Synchronous code; cannot call apick here.
    """
    out = ""
    for r in skill_results or []:
        if (getattr(r, "skill_name", "") or "").lower() == fam.skill:
            out = (getattr(r, "output", "") or "").strip()
            break

    es = (lang or "").lower().startswith("es")

    if status == "success":
        if fam.name == "email_send":
            m = re.search(r"to\s+([\w.+-]+@[\w-]+\.[\w.-]+)", out, re.IGNORECASE)
            recip = m.group(1) if m else ""
            if es:
                return f"Correo enviado" + (f" a {recip}" if recip else "") + "."
            return f"Email sent" + (f" to {recip}" if recip else "") + "."
        if fam.name == "task_create":
            m = re.search(r"Task created:\s*([^\n]+)", out)
            name = m.group(1).strip() if m else ""
            if es:
                return f"Tarea programada" + (f": {name}" if name else "") + "."
            return f"Task scheduled" + (f": {name}" if name else "") + "."
        if fam.name == "agent_create":
            m = re.search(r"Agent\s+['\"]([^'\"]+)['\"]", out, re.IGNORECASE)
            if es:
                return f"Agente creado" + (f" ({m.group(1)})" if m else "") + "."
            return f"Agent created" + (f" ({m.group(1)})" if m else "") + "."
    if status == "failed":
        short_err = (err or "").splitlines()[0][:120] if err else "skill returned an error"
        if es:
            label = {"email_send": "envío de correo", "task_create": "creación de tarea",
                     "agent_create": "creación de agente"}.get(fam.name, fam.name)
            return f"{label} falló: {short_err}"
        return f"{fam.name} failed: {short_err}"
    return f"{fam.name}: {status}"


def render_actions_block(actions: list[ActionRecord], lang: str = "en") -> str:
    """Render the structured ACTIONS block. Empty string if no actions."""
    if not actions:
        return ""
    header = "Actions:" if lang != "es" else "Acciones:"
    lines = [f"  • {a.summary}" for a in actions]
    return f"\n\n{header}\n" + "\n".join(lines)


# ── Composite entry ──────────────────────────────────────────────────────


def apply_action_announcer(
    response_text: str, skill_results, *, user_lang: str = "en",
) -> tuple[str, dict[str, Any]]:
    """Strip unverified claims AND contradicting failures AND append the
    structured block.

    Returns (text, trace). trace = {
        "stripped_families":     list[str],   # claims removed (skill DIDN'T run)
        "scrubbed_failures":     list[str],   # failure narratives removed (skill DID run)
        "actions_rendered":      list[str],
    }
    """
    trace: dict[str, Any] = {
        "stripped_families": [],
        "scrubbed_failures": [],
        "actions_rendered":  [],
    }

    # 1. Remove unbacked action claims from free text
    text, stripped = strip_unverified_claims(response_text, skill_results)
    trace["stripped_families"] = stripped

    # 2. Remove failure narratives that contradict actual successes
    text, scrubbed = strip_contradicting_failures(text, skill_results)
    trace["scrubbed_failures"] = scrubbed

    # 3. Build deterministic actions block from skill_results
    actions = collect_actions(skill_results, lang=user_lang)
    trace["actions_rendered"] = [a.family for a in actions]

    # 4. Append the block (only when at least one action exists)
    if actions:
        block = render_actions_block(actions, lang=user_lang)
        if not text.strip():
            text = block.lstrip()
        else:
            text = text + block

    return text, trace
