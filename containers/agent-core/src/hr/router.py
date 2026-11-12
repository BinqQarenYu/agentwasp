import os
import re
import json
import structlog
from pathlib import Path
from pydantic import BaseModel

from ..models.manager import ModelManager
from ..models.types import ModelRequest, Message

logger = structlog.get_logger()

class ExpertMetadata(BaseModel):
    id: str
    category: str
    name: str
    description: str
    file_path: str

class Expert(BaseModel):
    metadata: ExpertMetadata
    system_prompt: str

class TaskForce(BaseModel):
    primary: Expert
    auditor: Expert
    requires_spec: bool

class HRRouter:
    """Dynamically indexes experts from agency-agents and hires the best one for a task."""

    def __init__(self, model_manager: ModelManager):
        self.model_manager = model_manager
        self.agents_dir = Path("/app/data/agency-agents")
        if not self.agents_dir.exists():
            self.agents_dir = Path(__file__).parent.parent.parent.parent.parent / "agency-agents"
            
        self.spec_templates_dir = Path("/app/data/spec-kit-templates")
        if not self.spec_templates_dir.exists():
            self.spec_templates_dir = Path(__file__).parent.parent.parent.parent.parent / "spec-kit" / "templates"
            
        self.experts_catalog: list[ExpertMetadata] = []
        self._index_experts()

    def _index_experts(self):
        """Scan the directory and build a catalog of available experts."""
        if not self.agents_dir.exists():
            logger.warning("hr_router.missing_agents_dir", path=str(self.agents_dir))
            return
            
        for category_dir in [d for d in self.agents_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]:
            for md_file in category_dir.glob("*.md"):
                if md_file.name == "SKILL.md":
                    # Some use the SKILL.md naming convention
                    agent_id = f"{category_dir.name}/{md_file.parent.name}"
                else:
                    agent_id = f"{category_dir.name}/{md_file.stem}"
                    
                content = md_file.read_text(encoding="utf-8")
                
                name = md_file.stem.replace("-", " ").title()
                description = "Expert specialized in " + name
                
                # Parse frontmatter if exists
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        frontmatter = parts[1]
                        # Super simple regex extraction to avoid heavy yaml dependency if not needed
                        name_match = re.search(r"^name:\s*(.+)$", frontmatter, re.MULTILINE)
                        desc_match = re.search(r"^description:\s*(.+)$", frontmatter, re.MULTILINE)
                        
                        if name_match: name = name_match.group(1).strip()
                        if desc_match: description = desc_match.group(1).strip()
                
                self.experts_catalog.append(ExpertMetadata(
                    id=agent_id,
                    category=category_dir.name,
                    name=name,
                    description=description,
                    file_path=str(md_file)
                ))
                
        logger.info("hr_router.indexed", total_experts=len(self.experts_catalog))

    async def build_task_force(self, task: str) -> TaskForce:
        """Consults the LLM to form a Task Force (Primary + Auditor) and estimates effort."""
        
        # Build catalog string
        catalog_text = ""
        for i, exp in enumerate(self.experts_catalog):
            catalog_text += f"ID: {i} | Role: {exp.name} | Desc: {exp.description}\n"
            
        sys_prompt = (
            "You are the HR Router for an autonomous AI development firm.\n"
            "Your job is to analyze a task and form a 'Task Force'.\n\n"
            "1. Evaluate if the task is 'trivial' (typo, simple UI tweak, minor bug) or 'complex' (new feature, refactor, architecture).\n"
            "2. Select the ID of the Primary Expert to execute the task.\n"
            "3. Select the ID of the Auditor Expert to review the code (must be different from Primary).\n\n"
            "Here is your catalog of available experts:\n"
            f"{catalog_text}\n\n"
            "Return ONLY a JSON object: {\"effort\": \"trivial|complex\", \"primary_id\": int, \"auditor_id\": int}"
        )
        
        req = ModelRequest(
            messages=[
                Message(role="system", content=sys_prompt),
                Message(role="user", content=f"TASK: {task}")
            ],
            temperature=0.1
        )
        
        try:
            resp = await self.model_manager.providers[self.model_manager.active_provider].generate(req)
            # Parse JSON robustly
            s = resp.content.strip()
            if s.startswith("```json"): s = s[7:]
            if s.startswith("```"): s = s[3:]
            if s.endswith("```"): s = s[:-3]
            data = json.loads(s.strip())
            
            primary_idx = int(data.get("primary_id", 0))
            auditor_idx = int(data.get("auditor_id", 1))
            requires_spec = data.get("effort", "complex") == "complex"
            
            primary_meta = self.experts_catalog[primary_idx % len(self.experts_catalog)]
            auditor_meta = self.experts_catalog[auditor_idx % len(self.experts_catalog)]
        except Exception as e:
            logger.error("hr_router.build_failed", error=str(e))
            primary_meta = self.experts_catalog[0]
            auditor_meta = self.experts_catalog[1] if len(self.experts_catalog) > 1 else self.experts_catalog[0]
            requires_spec = True
            
        def _build_expert(meta: ExpertMetadata, role_type: str, needs_spec: bool) -> Expert:
            raw_content = Path(meta.file_path).read_text(encoding="utf-8")
            if raw_content.startswith("---"):
                parts = raw_content.split("---", 2)
                if len(parts) >= 3:
                    raw_content = parts[2].strip()
                    
            rules = f"\n\n## 🏢 TASK FORCE ROLE: {role_type.upper()}\n"
            
            if role_type == "primary":
                rules += "You are the Primary Executor. You will write the code and execute the terminal commands.\n"
                if needs_spec:
                    rules += (
                        "📋 MANDATORY SPECIFICATION-DRIVEN WORKFLOW:\n"
                        "Before you write any code, you MUST write a `SPEC.md` document detailing your technical approach.\n"
                        "Do not bypass this. The Auditor will review your spec.\n"
                    )
                else:
                    rules += "⚡ TRIVIAL TASK: You are cleared to execute immediately without writing a SPEC.md.\n"
                
                rules += (
                    "\n────────────────────────────── AUTONOMOUS EXECUTION LOOP (MANDATORY) ──────────────────────────────\n"
                    "You MUST continuously operate using this loop:\n"
                    "1. PLAN → Break down the goal into actionable steps\n"
                    "2. BUILD → Execute each step\n"
                    "3. VERIFY → Test using real execution (scripts, tools, browser automation, etc.)\n"
                    "4. COMPARE → Check results against success criteria\n"
                    "5. FIX → Identify gaps and re-execute\n"
                    "6. REPEAT → Continue until all criteria are satisfied\n"
                    "\n────────────────────────────── FALLBACK STRATEGY PROTOCOL ──────────────────────────────\n"
                    "For every step, anticipate failures and apply fallback strategies: PRIMARY → FALLBACK 1 → FALLBACK 2 → LAST RESORT\n"
                    "When a step fails: 1. Identify failure type 2. Apply alternative method 3. Retry 4. Continue\n"
                    "Rule: NEVER stop at first failure. ALWAYS attempt at least one fallback before escalation.\n"
                    "\n────────────────────────────── SELF-HEALING BEHAVIOR ──────────────────────────────\n"
                    "If errors occur: Diagnose root cause, Fix issue, Continue execution, Re-verify. Repeat until resolved or fallbacks exhausted.\n"
                    "\n────────────────────────────── BLOCKER HANDLING PROTOCOL ──────────────────────────────\n"
                    "Do NOT ask questions unless a TRUE blocker is encountered (impossible to proceed, no fallbacks remain).\n"
                    "If a task is IMPOSSIBLE: You MUST clearly inform the user, dissect the problem to explain exactly WHY it is impossible, and provide the technical reasoning.\n"
                    "If a blocker occurs: Describe it, explain WHY it cannot be resolved, identify root cause, provide actionable recommendations, suggest workarounds, continue using best fallback.\n"
                    "\n────────────────────────────── COMPLETION CONDITIONS ──────────────────────────────\n"
                    "Task is COMPLETE ONLY IF: All criteria met, full verification done, no bugs remain, output clean.\n"
                    "Return ONLY when 100% complete and fully verified. Do NOT return partial progress.\n"
                )
                
                rules += "\nWhen you are finished, you must explicitly hand off the code to the Auditor for review."
            else:
                rules += "You are the Auditor. You do not write the initial code. You review the Primary Executor's work.\n"
                rules += "You must find edge cases, security flaws, or architectural anti-patterns in their implementation.\n"
                rules += "If it passes, approve it. If it fails, send it back with explicit corrections."
                
            return Expert(metadata=meta, system_prompt=raw_content + rules)
            
        return TaskForce(
            primary=_build_expert(primary_meta, "primary", requires_spec),
            auditor=_build_expert(auditor_meta, "auditor", requires_spec),
            requires_spec=requires_spec
        )
