"""Plan Executor — Drives ExecutionPlan steps directly, bypassing LLM when possible.

For DETERMINISTIC steps: creates SkillCall objects and executes them directly
  without any LLM call. The system controls execution completely.

For LLM_ASSIST steps: returns a signal so the caller injects a narrow,
  constrained prompt. The LLM answers ONE factual question, the answer is
  parsed and applied to the step, then execution resumes deterministically.

For ADAPTIVE/FALLBACK steps: returns needs_llm=True with the plan-formatted
  prompt block. The LLM executes with full plan context.

Key guarantee: the LLM is NEVER asked "what should I do?" — only
  "what is the value of X?" (narrow, factual, schema-validated).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..skills.executor import SkillExecutor
    from .execution_planner import ExecutionPlan, ExecutionStep

from ..skills.types import SkillCall, SkillResult


@dataclass
class PlanStepExecution:
    """Result of one plan step execution attempt."""
    step_id: str
    skill_calls: list[SkillCall] = field(default_factory=list)
    results: list[SkillResult] = field(default_factory=list)
    success: bool = False
    output: str = ""
    needs_llm: bool = False     # True → caller must inject LLM prompt before resuming
    llm_query: str = ""         # Narrow factual question for LLM (LLM_ASSIST steps)
    is_plan_prompt: bool = False  # True → llm_query is a full plan prompt block


class PlanExecutor:
    """Executes deterministic plan steps directly, bypassing LLM when possible.

    Usage pattern in the execution loop:
        executor = PlanExecutor(plan, skill_executor)

        step_exec = await executor.try_execute_next(user_id, chat_id)
        if step_exec is None:
            break  # plan exhausted

        if not step_exec.needs_llm:
            # Direct execution succeeded — update plan state
            plan.mark_step_done(step_exec.step_id, step_exec.success, step_exec.output)
            # Add results to action_history and accumulated_results
            ...
        else:
            # LLM needed — inject step_exec.llm_query into prompt and call LLM
            # After LLM responds, call: executor.apply_llm_result(step_id, llm_text)
            ...
    """

    def __init__(self, plan: "ExecutionPlan", skill_executor: "SkillExecutor"):
        self.plan = plan
        self.skill_executor = skill_executor

    async def try_execute_next(
        self,
        user_id: str = "",
        chat_id: str = "",
    ) -> PlanStepExecution | None:
        """Try to execute the next pending plan step.

        Returns:
            PlanStepExecution with needs_llm=False → step executed directly
            PlanStepExecution with needs_llm=True → caller must involve LLM
            None → no more steps (plan complete or exhausted)
        """
        step = self.plan.next_step()
        if step is None:
            return None

        from .execution_planner import StepType

        if step.step_type == StepType.DETERMINISTIC:
            return await self._execute_deterministic(step, user_id, chat_id)

        elif step.step_type == StepType.LLM_ASSIST:
            # Signal: LLM must answer a narrow question before this step can run
            return PlanStepExecution(
                step_id=step.id,
                needs_llm=True,
                llm_query=step.llm_query,
                is_plan_prompt=False,
            )

        elif step.step_type == StepType.VALIDATION:
            return await self._execute_validation(step, user_id, chat_id)

        else:
            # ADAPTIVE or FALLBACK — LLM executes with full plan prompt context
            return PlanStepExecution(
                step_id=step.id,
                needs_llm=True,
                llm_query="",       # Caller uses format_plan_for_prompt(plan) instead
                is_plan_prompt=True,
            )

    async def execute_deterministic_sequence(
        self,
        user_id: str = "",
        chat_id: str = "",
    ) -> list[PlanStepExecution]:
        """Execute all consecutive DETERMINISTIC steps directly.

        Stops at the first non-DETERMINISTIC step or when plan is exhausted.
        Returns list of all executed step results (may be empty).
        """
        from .execution_planner import StepType

        executions: list[PlanStepExecution] = []
        while True:
            step = self.plan.next_step()
            if step is None or step.step_type != StepType.DETERMINISTIC:
                break

            step_exec = await self._execute_deterministic(step, user_id, chat_id)
            executions.append(step_exec)

            # Update plan state
            self.plan.mark_step_done(step.id, step_exec.success, step_exec.output)
            self.plan.mark_step_attempt(step.id)

            # Stop if step failed and it was required
            if not step_exec.success and step.required:
                break

        return executions

    async def _execute_deterministic(
        self,
        step: "ExecutionStep",
        user_id: str,
        chat_id: str,
    ) -> PlanStepExecution:
        """Execute a DETERMINISTIC step directly — no LLM involved."""
        self.plan.mark_step_attempt(step.id)

        skill_call = SkillCall(
            skill_name=step.skill,
            arguments={str(k): str(v) for k, v in step.params.items() if v is not None},
        )

        results = await self.skill_executor.execute_batch(
            [skill_call],
            user_id=user_id,
            chat_id=chat_id,
        )

        result = results[0] if results else SkillResult(
            skill_name=step.skill,
            success=False,
            output="",
            error="No result returned",
        )

        output = result.output or ""
        success = result.success and (
            not step.success_signal
            or step.success_signal in output
        )

        return PlanStepExecution(
            step_id=step.id,
            skill_calls=[skill_call],
            results=results,
            success=success,
            output=output,
        )

    async def _execute_validation(
        self,
        step: "ExecutionStep",
        user_id: str,
        chat_id: str,
    ) -> PlanStepExecution:
        """Execute a VALIDATION step (skill call + evidence check by caller)."""
        self.plan.mark_step_attempt(step.id)

        if not step.skill or not step.params:
            # Pure check — no skill call needed
            return PlanStepExecution(step_id=step.id, success=True, output="")

        skill_call = SkillCall(
            skill_name=step.skill,
            arguments={str(k): str(v) for k, v in step.params.items() if v is not None},
        )
        results = await self.skill_executor.execute_batch(
            [skill_call], user_id=user_id, chat_id=chat_id
        )
        output = results[0].output if results else ""
        success = bool(results and results[0].success)

        return PlanStepExecution(
            step_id=step.id,
            skill_calls=[skill_call],
            results=results,
            success=success,
            output=output,
        )

    def apply_llm_result(self, step_id: str, llm_text: str) -> bool:
        """Apply LLM's answer to an LLM_ASSIST step, resolving unknown param values.

        After the LLM answers the narrow factual query, its answer is parsed
        and injected into the step's params. The step is then upgraded to
        DETERMINISTIC so it can execute on the next round without LLM.

        Returns True if the answer was valid and applied successfully.
        """
        from .execution_planner import StepType

        for step in self.plan.steps:
            if step.id != step_id:
                continue

            schema = step.llm_answer_schema
            param_to_fill = step.param_hints.get("param_to_fill", "")

            if schema == "css_selector" and param_to_fill:
                selector = _extract_css_selector(llm_text)
                if selector:
                    step.params[param_to_fill] = selector
                    step.step_type = StepType.DETERMINISTIC
                    return True

            elif schema == "next_action_json":
                action_data = _extract_json_object(llm_text)
                if action_data and isinstance(action_data, dict):
                    action = action_data.get("action", "capture")
                    selector = action_data.get("selector") or ""
                    value = action_data.get("value") or ""
                    step.params.update({
                        "action": action,
                        **({"selector": selector} if selector else {}),
                        **({"text": value} if value and action == "type" else {}),
                        **({"code": value} if value and action == "execute_js" else {}),
                    })
                    step.step_type = StepType.ADAPTIVE
                    return True

            elif schema == "javascript_code" and param_to_fill:
                js_code = _extract_code(llm_text)
                if js_code:
                    step.params[param_to_fill] = js_code
                    step.step_type = StepType.DETERMINISTIC
                    return True

            elif schema == "form_field_list_json":
                # This is informational — mark step done, caller uses the text
                step.completed = True
                return True

        return False


# ---------------------------------------------------------------------------
# Answer extraction helpers
# ---------------------------------------------------------------------------

def _extract_css_selector(text: str) -> str:
    """Extract a CSS selector from LLM answer text."""
    text = text.strip()
    # Strip markdown code fences
    text = re.sub(r"```[^\n]*\n?", "", text).strip("`").strip()
    # Extract from backtick-quoted span
    m = re.search(r"`([^`\n]{1,200})`", text)
    if m:
        return m.group(1).strip()
    # Take first non-empty line that looks like a CSS selector
    for line in text.split("\n"):
        line = line.strip().strip("'\"")
        if line and re.match(r'^[#.\[\]a-zA-Z*][^"\n]{0,200}$', line):
            return line
    return text[:200]


def _extract_json_object(text: str) -> dict | list | None:
    """Extract the first JSON object or array from text."""
    # Find balanced JSON structure
    for pattern in (r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}', r'\[[^\[\]]*\]'):
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except (json.JSONDecodeError, ValueError):
                pass
    # Try parsing entire text as JSON
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_code(text: str) -> str:
    """Extract code from markdown code block or raw text."""
    m = re.search(r"```(?:javascript|js|python)?\n?(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()
