"""Skill Crystallizer — Voyager-style Explore→Code→Store loop.

When the agent encounters a novel problem:
1. EXPLORE: Try different approaches, install deps, write test scripts
2. CODE: Distill the successful approach into a clean, parameterized skill
3. STORE: Save as L3 memory + executable Python module in the skill tree

Skills are real .py files that get better through use (reinforcement learning).
"""

import ast
import hashlib
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Skill:
    """A crystallized executable capability."""
    name: str
    description: str
    source_path: Path
    entry_function: str  # e.g., "fetch_price" or "solve.main"
    parameters: dict[str, str]  # param_name -> type_hint
    returns: str
    dependencies: list[str]  # pip packages
    success_count: int = 0
    failure_count: int = 0
    avg_execution_time: float = 0.0
    created_from_task: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / max(total, 1)

    @property
    def score(self) -> float:
        """Composite quality score for skill ranking."""
        return self.success_rate * (1 + 0.01 * self.success_count)


class Crystallizer:
    """Transforms raw exploration into reusable skills.

    The crystallizer watches the agent's execution, identifies successful
    patterns, extracts them into clean Python modules, and stores them
    in the skill tree for future use.
    """

    def __init__(self, skills_dir: Path, memory_lattice, genome, skill_tree=None):
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.memory = memory_lattice
        self.genome = genome
        self.skill_tree = skill_tree
        self.skills: dict[str, Skill] = {}
        self._scan()

    def _scan(self):
        """Discover existing skills."""
        for py_file in self.skills_dir.glob("**/*.py"):
            if py_file.name.startswith("_"):
                continue
            skill = self._parse_skill_file(py_file)
            if skill:
                self.skills[skill.name] = skill

    def _parse_skill_file(self, path: Path) -> Optional[Skill]:
        """Extract skill metadata from a Python file."""
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, Exception):
            return None

        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        if not functions:
            return None

        main_func = functions[0]
        params = {}
        for arg in main_func.args.args:
            annotation = ""
            if arg.annotation:
                annotation = ast.unparse(arg.annotation)
            params[arg.arg] = annotation or "Any"

        returns = "Any"
        if main_func.returns:
            returns = ast.unparse(main_func.returns)

        # Extract description from docstring or first comment
        description = ast.get_docstring(main_func) or path.stem.replace("_", " ")

        # Find imports for dependencies
        deps = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    deps.append(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    deps.append(node.module.split(".")[0])

        # Filter to likely pip packages
        stdlib = {"os", "sys", "time", "json", "re", "math", "random", "pathlib",
                  "hashlib", "ast", "subprocess", "functools", "itertools", "collections",
                  "typing", "dataclasses", "textwrap", "copy"}
        deps = [d for d in deps if d not in stdlib]

        return Skill(
            name=path.stem,
            description=description,
            source_path=path,
            entry_function=main_func.name,
            parameters=params,
            returns=returns,
            dependencies=deps,
        )

    def crystallize(self, task_description: str, working_code: str,
                    skill_name: str, tags: Optional[list[str]] = None) -> Skill:
        """Transform working code into a reusable skill.

        This is the core evolutionary operation: raw exploration → clean skill.
        """
        # Clean the code — wrap in a proper function if needed
        cleaned = self._clean_code(working_code, skill_name)

        # Write the skill file
        skill_path = self.skills_dir / f"{skill_name}.py"
        skill_path.write_text(cleaned, encoding="utf-8")

        # Parse it
        skill = self._parse_skill_file(skill_path) or Skill(
            name=skill_name,
            description=task_description[:200],
            source_path=skill_path,
            entry_function="main",
            parameters={},
            returns="Any",
            dependencies=[],
            created_from_task=task_description[:200],
        )

        if tags:
            skill.tags = tags
        skill.created_from_task = task_description[:200]

        self.skills[skill_name] = skill

        # Also add as a gene
        self.genome.add_gene(f"skill_{skill_name}", cleaned)

        # Register in skill tree (if available)
        if self.skill_tree:
            self.skill_tree.add_skill(skill_name, skill.dependencies, category="crystallized")

        # Store in memory lattice L3
        from ..memory.lattice import MemoryEntry
        entry = MemoryEntry(
            layer=3,
            key=f"skill:{skill_name}",
            content=f"Skill: {skill_name}\n{skill.description}\n"
                   f"Function: {skill.entry_function}({', '.join(skill.parameters)})\n"
                   f"Dependencies: {', '.join(skill.dependencies)}\n\n{cleaned}",
            tags=tags or [],
        )
        self.memory.store(entry)

        return skill

    def execute(self, skill_name: str, **kwargs) -> dict:
        """Execute a skill and track performance."""
        if skill_name not in self.skills:
            return {"error": f"Skill '{skill_name}' not found", "success": False}

        skill = self.skills[skill_name]
        start = time.perf_counter()

        try:
            # Dynamic import
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                skill_name, str(skill.source_path)
            )
            if spec is None or spec.loader is None:
                return {"error": "Could not load skill module", "success": False}

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            func = getattr(module, skill.entry_function)

            # First check if there's a monkey-patched version
            patched = self.genome.get_patch(skill.entry_function)
            if patched:
                func = patched

            result = func(**kwargs)

            elapsed = time.perf_counter() - start
            skill.success_count += 1
            skill.avg_execution_time = (
                skill.avg_execution_time * (skill.success_count - 1) + elapsed
            ) / skill.success_count

            # Reinforce memory
            self.memory.reinforce(3, f"skill:{skill_name}", success=True)

            return {"result": result, "elapsed": elapsed, "success": True}

        except Exception as e:
            elapsed = time.perf_counter() - start
            skill.failure_count += 1
            self.memory.reinforce(3, f"skill:{skill_name}", success=False)

            return {
                "error": str(e),
                "elapsed": elapsed,
                "success": False,
                "failure_count": skill.failure_count,
            }

    def _clean_code(self, raw_code: str, skill_name: str) -> str:
        """Clean and standardize raw exploration code into a proper skill module."""
        # Remove leading/trailing whitespace
        code = raw_code.strip()

        # Add shebang
        header = f'"""Skill: {skill_name} — Auto-crystallized by EVO-AGENT."""\n\n'

        # If no function defined, wrap in one
        if "def " not in code:
            code = f"def main(**kwargs):\n" + "\n".join(
                f"    {line}" for line in code.splitlines()
            )

        return header + code + "\n"

    def discover_trending_skills(self, limit: int = 5) -> list[Skill]:
        """Skills with improving success rates — worth investing in."""
        trending = [
            s for s in self.skills.values()
            if s.success_count >= 3 and s.success_rate > 0.5
        ]
        trending.sort(key=lambda s: -s.score)
        return trending[:limit]

    def find_weak_skills(self) -> list[Skill]:
        """Skills that need improvement or should be deprecated."""
        weak = [s for s in self.skills.values() if s.success_count + s.failure_count >= 3]
        weak.sort(key=lambda s: s.success_rate)
        return weak[:5]

    @property
    def stats(self) -> dict:
        return {
            "total_skills": len(self.skills),
            "avg_success_rate": sum(s.success_rate for s in self.skills.values()) / max(len(self.skills), 1),
            "trending": [s.name for s in self.discover_trending_skills(3)],
            "weak": [s.name for s in self.find_weak_skills()[:3]],
        }
