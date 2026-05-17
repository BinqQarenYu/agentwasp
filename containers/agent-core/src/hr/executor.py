import structlog
from typing import Optional
from pydantic import BaseModel

from ..models.manager import ModelManager
from ..models.types import ModelRequest, Message
from .router import TaskForce

logger = structlog.get_logger()

class ExecutionResult(BaseModel):
    success: bool
    final_output: str
    revisions_used: int
    auditor_feedback: Optional[str] = None

class TaskForceExecutor:
    """Manages the execution loop between a Primary and an Auditor."""

    def __init__(self, model_manager: ModelManager, max_revisions: int = 2):
        self.model_manager = model_manager
        self.max_revisions = max_revisions

    async def execute_task(self, task: str, task_force: TaskForce, primary_executor=None) -> ExecutionResult:
        """Runs the task force loop: Primary drafts -> Auditor reviews -> Retry if failed."""
        
        logger.info(
            "task_force.execute.start", 
            task=task, 
            primary=task_force.primary.metadata.name, 
            auditor=task_force.auditor.metadata.name,
            requires_spec=task_force.requires_spec
        )

        # Primary's working memory
        primary_messages = [
            Message(role="system", content=task_force.primary.system_prompt),
            Message(role="user", content=f"Execute the following task:\n\n{task}")
        ]

        current_output = ""
        revisions = 0
        
        while revisions <= self.max_revisions:
            # 1. Primary Execution
            if primary_executor:
                current_output = await primary_executor(primary_messages)
            else:
                req = ModelRequest(messages=primary_messages, temperature=0.2)
                primary_resp = await self.model_manager.providers[self.model_manager.active_provider].generate(req)
                current_output = primary_resp.content.strip()
            
            logger.info("task_force.primary.complete", revision=revisions)

            # 2. Auditor Review
            auditor_sys = task_force.auditor.system_prompt
            auditor_prompt = (
                f"You are auditing the following output for the task: '{task}'.\n\n"
                f"### PRIMARY OUTPUT:\n{current_output}\n\n"
                "Review this carefully. If it is perfect and ready for production, reply EXACTLY with 'APPROVED'.\n"
                "If there are flaws, edge cases, or missing specifications, detail the errors. Do NOT say 'APPROVED'."
            )
            
            req_auditor = ModelRequest(
                messages=[
                    Message(role="system", content=auditor_sys),
                    Message(role="user", content=auditor_prompt)
                ],
                temperature=0.1
            )
            
            auditor_resp = await self.model_manager.providers[self.model_manager.active_provider].generate(req_auditor)
            feedback = auditor_resp.content.strip()

            # 3. Check decision
            if feedback.startswith("APPROVED") or "APPROVED" in feedback.split("\n")[0]:
                logger.info("task_force.auditor.approved", revision=revisions)
                return ExecutionResult(
                    success=True,
                    final_output=current_output,
                    revisions_used=revisions
                )
                
            # 4. Handle Rejection
            logger.warning("task_force.auditor.rejected", revision=revisions, feedback=feedback[:100] + "...")
            revisions += 1
            
            if revisions > self.max_revisions:
                logger.error("task_force.max_revisions_exceeded", limit=self.max_revisions)
                return ExecutionResult(
                    success=False,
                    final_output=current_output,
                    revisions_used=self.max_revisions,
                    auditor_feedback=feedback
                )
                
            # Pass feedback back to primary
            if not primary_messages or primary_messages[-1].role != "assistant" or primary_messages[-1].content != current_output:
                primary_messages.append(Message(role="assistant", content=current_output))
            primary_messages.append(Message(
                role="user", 
                content=f"The Auditor rejected your implementation with the following feedback:\n\n{feedback}\n\nPlease fix the issues."
            ))

        return ExecutionResult(success=False, final_output=current_output, revisions_used=revisions)
