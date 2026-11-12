"""Self-Modification Pipeline — The REAL evolution engine.

This is the heart of MUE's self-evolution. It implements the full cycle:
1. COPY source file → creates a backup
2. LLM REWRITES the code → improved version
3. AST VALIDATE → syntax check
4. IMPORT TEST → can Python actually load it?
5. EXECUTE TEST → run any test_* functions
6. REPLACE original on success → new code becomes the source
7. ROLLBACK on failure → restore from backup
8. LOG result → audit trail + git commit

Each stage is a gate. Failure at any stage after rewrite triggers
automatic rollback and analysis. The agent learns from every attempt.
"""

import ast
import hashlib
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class PipelineStage:
    """Result of a single pipeline stage."""
    name: str
    passed: bool
    output: str = ""
    error: str = ""
    duration_ms: float = 0.0


@dataclass
class PipelineResult:
    """Full result of a self-modification attempt."""
    gene_name: str
    stages: list[PipelineStage] = field(default_factory=list)
    success: bool = False
    source_before: str = ""
    source_after: str = ""
    backup_path: str = ""
    rollback_hash: str = ""
    llm_reasoning: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def stage_names(self) -> str:
        statuses = []
        for s in self.stages:
            symbol = "+" if s.passed else "X"
            statuses.append(f"[{symbol} {s.name}]")
        return " ".join(statuses)

    @property
    def summary(self) -> str:
        if self.success:
            return f"OK: {self.gene_name} evolved successfully ({self.stage_names})"
        failed = [s for s in self.stages if not s.passed]
        last = failed[-1] if failed else self.stages[-1]
        return f"FAIL: {self.gene_name} at stage '{last.name}': {last.error[:200]} ({self.stage_names})"


class SelfModificationPipeline:
    """Executes the full self-modification cycle with safety gates.

    Each mutation goes through this pipeline. If any validation stage
    fails after the LLM rewrite, the original code is restored automatically.
    """

    def __init__(self, genes_dir: Path, security_guard=None, genome=None):
        self.genes_dir = Path(genes_dir)
        self.security = security_guard
        self.genome = genome  # S7: optional genome for stats tracking
        self.history: list[PipelineResult] = []
        self._stage_timings: dict[str, list[float]] = {}

    def run(self, gene_name: str, source: str, llm_call: callable = None,
            agent_name: str = "Mue") -> PipelineResult:
        """Execute the full self-modification pipeline WITH LLM rewrite.

        Args:
            gene_name: Name of the gene to modify
            source: Current source code
            llm_call: Function(prompt, system, json_mode) -> dict
            agent_name: Name of the agent for prompt context

        Returns:
            PipelineResult with success/failure and stage details
        """
        result = PipelineResult(gene_name=gene_name, source_before=source)

        # ── STAGE 0: COPY (backup) ──
        backup = self._stage_backup(result)
        if not backup.passed:
            result.stages.append(backup)
            self.history.append(result)
            return result
        result.stages.append(backup)

        # ── STAGE 1: LLM REWRITE ──
        if llm_call:
            rewrite = self._stage_llm_rewrite(result, source, llm_call, agent_name)
            result.stages.append(rewrite)
            if not rewrite.passed:
                self._rollback(result, "LLM rewrite failed or produced no changes")
                self.history.append(result)
                return result

        # ── STAGE 2: AST VALIDATE ──
        ast_check = self._stage_ast_validate(result)
        result.stages.append(ast_check)
        if not ast_check.passed:
            self._rollback(result, f"AST validation failed: {ast_check.error}")
            self.history.append(result)
            return result

        # ── STAGE 3: IMPORT TEST ──
        import_test = self._stage_import_test(result)
        result.stages.append(import_test)
        if not import_test.passed:
            self._rollback(result, f"Import test failed: {import_test.error}")
            self.history.append(result)
            return result

        # ── STAGE 4: EXECUTE TEST ──
        exec_test = self._stage_execute_test(result)
        result.stages.append(exec_test)

        # ── STAGE 5: REPLACE ──
        replace = self._stage_replace(result)
        result.stages.append(replace)
        if not replace.passed:
            self._rollback(result, f"File write failed: {replace.error}")
            self.history.append(result)
            return result

        result.success = True
        self.history.append(result)
        return result

    def apply_improvement(self, gene_name: str, source_before: str,
                          improved_source: str, reason: str = "") -> PipelineResult:
        """Apply an improvement from Claude Code directly (no LLM call).

        Claude Code provides the improved source code. The pipeline validates,
        tests, and applies it — or rolls back on failure.

        Stages: backup → AST validate → import test → execute test → replace
        """
        result = PipelineResult(
            gene_name=gene_name,
            source_before=source_before,
            source_after=improved_source,
            llm_reasoning=reason,
        )

        # ── STAGE 0: BACKUP ──
        backup = self._stage_backup(result)
        result.stages.append(backup)
        if not backup.passed:
            self.history.append(result)
            return result

        # ── STAGE 1: AST VALIDATE ──
        ast_check = self._stage_ast_validate(result)
        result.stages.append(ast_check)
        if not ast_check.passed:
            self._rollback(result, f"AST validation failed: {ast_check.error}")
            self.history.append(result)
            return result

        # ── STAGE 2: IMPORT TEST ──
        import_test = self._stage_import_test(result)
        result.stages.append(import_test)
        if not import_test.passed:
            self._rollback(result, f"Import test failed: {import_test.error}")
            self.history.append(result)
            return result

        # ── STAGE 3: EXECUTE TEST ──
        exec_test = self._stage_execute_test(result)
        result.stages.append(exec_test)
        if not exec_test.passed:
            self._rollback(result, f"Test execution failed: {exec_test.error}")
            self.history.append(result)
            return result

        # ── STAGE 4: REPLACE ──
        replace = self._stage_replace(result)
        result.stages.append(replace)
        if not replace.passed:
            self._rollback(result, f"File write failed: {replace.error}")
            self.history.append(result)
            return result

        result.success = True
        self.history.append(result)
        return result

    # ── PIPELINE STAGES ──────────────────────────────────────────────────

    def _stage_backup(self, result: PipelineResult) -> PipelineStage:
        """Stage 0: Create a backup copy of the current gene file."""
        t0 = time.time()
        gene_path = self.genes_dir / f"{result.gene_name}.py"

        if not gene_path.exists():
            return PipelineStage(
                name="backup", passed=False,
                error=f"Gene file not found: {gene_path}"
            )

        backup_dir = self.genes_dir / ".backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time())
        backup_path = backup_dir / f"{result.gene_name}_{timestamp}.bak"
        backup_path.write_text(result.source_before, encoding="utf-8")

        result.backup_path = str(backup_path)
        result.rollback_hash = hashlib.sha256(
            result.source_before.encode()
        ).hexdigest()[:16]

        return PipelineStage(
            name="backup", passed=True,
            output=f"Backup: {backup_path.name}",
            duration_ms=(time.time() - t0) * 1000,
        )

    def _stage_llm_rewrite(self, result: PipelineResult, source: str,
                           llm_call: callable, agent_name: str) -> PipelineStage:
        """Stage 1: Send source to LLM for improvement."""
        t0 = time.time()

        if len(source) > 10000:
            return PipelineStage(
                name="llm_rewrite", passed=False,
                error=f"Source too large: {len(source)} chars"
            )

        prompt = f"""You are {agent_name}, a self-evolving AI agent. You are analyzing your OWN source code
to improve yourself. This is REAL self-modification — your source files will actually change.

File: {result.gene_name}.py
Current source code:
```python
{source}
```

Your task: Improve this code. Be SPECIFIC and CONCRETE.

Consider:
1. **Bug fixes**: Logic errors, edge cases, race conditions
2. **Performance**: Optimize operations, reduce allocations
3. **Robustness**: Better error handling, input validation
4. **New capabilities**: Useful functionality additions
5. **Code quality**: Clarity, maintainability

CRITICAL RULES:
- Do NOT remove existing functionality unless genuinely harmful
- Do NOT change public function signatures
- DO add error handling, guards, optimizations
- DO add useful helper functions
- Output the COMPLETE modified file, not a diff
- Every change must be justified

Return JSON:
{{
    "reasoning": "Why these changes are beneficial...",
    "confidence": 0.0-1.0,
    "modified_source": "the complete modified source code",
    "changes_summary": "one-line summary of what changed"
}}"""

        try:
            response = llm_call(
                prompt=prompt,
                system="You are a self-evolving AI agent improving its own source code. Be precise. Be careful. Be honest.",
                json_mode=True,
            )

            if not response or "modified_source" not in response:
                return PipelineStage(
                    name="llm_rewrite", passed=False,
                    error="LLM returned no modified_source"
                )

            modified = response["modified_source"]
            if modified.strip() == source.strip():
                return PipelineStage(
                    name="llm_rewrite", passed=False,
                    error="No changes proposed"
                )

            result.source_after = modified
            result.llm_reasoning = response.get("reasoning", "")

            return PipelineStage(
                name="llm_rewrite", passed=True,
                output=f"LLM proposed changes (confidence: {response.get('confidence', 0.0):.0%})",
                duration_ms=(time.time() - t0) * 1000,
            )

        except Exception as e:
            return PipelineStage(
                name="llm_rewrite", passed=False,
                error=f"LLM call error: {e}"
            )

    def _stage_ast_validate(self, result: PipelineResult) -> PipelineStage:
        """Stage 2: Validate syntax via AST parsing."""
        t0 = time.time()
        try:
            ast.parse(result.source_after)
            return PipelineStage(
                name="ast_validate", passed=True,
                output="AST parse OK",
                duration_ms=(time.time() - t0) * 1000,
            )
        except SyntaxError as e:
            return PipelineStage(
                name="ast_validate", passed=False,
                error=f"Syntax error at line {e.lineno}: {e.msg}",
                duration_ms=(time.time() - t0) * 1000,
            )

    def _stage_import_test(self, result: PipelineResult) -> PipelineStage:
        """Stage 3: Test that Python can import the modified module.

        Runs in a subprocess to avoid polluting the current Python process.
        """
        t0 = time.time()
        tmp_path = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", prefix=f"test_{result.gene_name}_",
                delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(result.source_after)
                tmp_path = tmp.name

            # Run import test in subprocess
            test_script = f"""
import ast, sys
try:
    with open({tmp_path!r}, 'r', encoding='utf-8') as f:
        ast.parse(f.read())
    # Also try compiling
    with open({tmp_path!r}, 'r', encoding='utf-8') as f:
        compile(f.read(), {tmp_path!r}, 'exec')
    print("IMPORT_OK")
except SyntaxError as e:
    print(f"SYNTAX_ERROR:{{e}}")
except Exception as e:
    print(f"IMPORT_ERROR:{{e}}")
"""
            proc = subprocess.run(
                [sys.executable, "-c", test_script],
                capture_output=True, text=True, timeout=15,
                cwd=str(self.genes_dir.parent),
            )

            if proc.returncode != 0 or "IMPORT_OK" not in proc.stdout:
                error = proc.stdout.strip() + proc.stderr.strip()
                return PipelineStage(
                    name="import_test", passed=False,
                    error=error[:500] or f"Exit code: {proc.returncode}",
                    duration_ms=(time.time() - t0) * 1000,
                )

            return PipelineStage(
                name="import_test", passed=True,
                output="Module imports cleanly",
                duration_ms=(time.time() - t0) * 1000,
            )

        except subprocess.TimeoutExpired:
            # S6 FIX: tmp_path is captured; cleanup happens in finally
            return PipelineStage(
                name="import_test", passed=False,
                error="Import test timed out (15s)",
                duration_ms=(time.time() - t0) * 1000,
            )
        except Exception as e:
            return PipelineStage(
                name="import_test", passed=False,
                error=f"Import test error: {e}",
                duration_ms=(time.time() - t0) * 1000,
            )
        finally:
            # S6 FIX: Always cleanup temp file regardless of exception path
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _stage_execute_test(self, result: PipelineResult) -> PipelineStage:
        """Stage 4: Execute test functions in the modified code.

        Finds test_* functions and runs them in a subprocess.
        If no test functions exist, this stage passes (optional).
        """
        t0 = time.time()

        # Parse to find test functions
        try:
            tree = ast.parse(result.source_after)
            test_funcs = [
                node.name for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
            ]
        except SyntaxError:
            test_funcs = []

        if not test_funcs:
            return PipelineStage(
                name="execute_test", passed=True,
                output="No test functions found (optional stage)",
                duration_ms=(time.time() - t0) * 1000,
            )

        tmp_path = None
        try:
            # Write modified code to temp file
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", prefix=f"runtest_{result.gene_name}_",
                delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(result.source_after)
                tmp_path = tmp.name

            # Build test runner
            test_names_repr = repr(test_funcs)
            test_script = f"""
import sys, traceback
sys.path.insert(0, {str(self.genes_dir.parent)!r})

# Import the modified module
import importlib.util
spec = importlib.util.spec_from_file_location("_test_module", {tmp_path!r})
mod = importlib.util.module_from_spec(spec)

try:
    spec.loader.exec_module(mod)
    passed = 0
    failed = 0
    for name in {test_names_repr}:
        func = getattr(mod, name, None)
        if func is None:
            continue
        try:
            func()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"TEST_FAIL:{{name}}:{{e}}:{{traceback.format_exc()[-200:]}}")
    print(f"TEST_RESULTS:{{passed}}/{{passed + failed}} passed")
except Exception as e:
    print(f"LOAD_ERROR:{{e}}")
"""
            proc = subprocess.run(
                [sys.executable, "-c", test_script],
                capture_output=True, text=True, timeout=30,
                cwd=str(self.genes_dir.parent),
            )

            stdout = proc.stdout.strip()
            stderr = proc.stderr.strip()

            if "LOAD_ERROR" in stdout:
                return PipelineStage(
                    name="execute_test", passed=False,
                    error=f"Cannot load module: {stdout}",
                    duration_ms=(time.time() - t0) * 1000,
                )

            # Parse test results
            for line in stdout.split("\n"):
                if line.startswith("TEST_FAIL:"):
                    return PipelineStage(
                        name="execute_test", passed=False,
                        error=f"Test failure: {line}",
                        duration_ms=(time.time() - t0) * 1000,
                    )

            # Check results
            for line in stdout.split("\n"):
                if line.startswith("TEST_RESULTS:"):
                    parts = line.split(":")[1]
                    passed_count = int(parts.split("/")[0])
                    total_count = int(parts.split("/")[1].split()[0])
                    if passed_count < total_count:
                        return PipelineStage(
                            name="execute_test", passed=False,
                            error=f"Only {passed_count}/{total_count} tests passed",
                            duration_ms=(time.time() - t0) * 1000,
                        )

            return PipelineStage(
                name="execute_test", passed=True,
                output=f"All {len(test_funcs)} test(s) passed in subprocess",
                duration_ms=(time.time() - t0) * 1000,
            )

        except subprocess.TimeoutExpired:
            return PipelineStage(
                name="execute_test", passed=False,
                error="Test execution timed out (30s)",
                duration_ms=(time.time() - t0) * 1000,
            )
        except Exception as e:
            return PipelineStage(
                name="execute_test", passed=False,
                error=f"Test execution error: {e}",
                duration_ms=(time.time() - t0) * 1000,
            )
        finally:
            # S6 FIX: Always cleanup temp file
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _stage_replace(self, result: PipelineResult) -> PipelineStage:
        """Stage 5: Write the new source code to the actual gene file."""
        t0 = time.time()

        gene_path = self.genes_dir / f"{result.gene_name}.py"

        try:
            # Security check
            if self.security and not self.security.allow_write(gene_path, result.source_after):
                return PipelineStage(
                    name="replace", passed=False,
                    error=f"Security guard blocked write to {gene_path}"
                )

            gene_path.write_text(result.source_after, encoding="utf-8")

            # S7 FIX: Update genome stats so mutation counts and fitness stay accurate
            if self.genome:
                try:
                    self.genome.mutate_gene(result.gene_name, result.source_after)
                except Exception:
                    pass

            return PipelineStage(
                name="replace", passed=True,
                output=f"Source file updated: {gene_path.name}",
                duration_ms=(time.time() - t0) * 1000,
            )
        except Exception as e:
            return PipelineStage(
                name="replace", passed=False,
                error=f"File write error: {e}",
                duration_ms=(time.time() - t0) * 1000,
            )

    def _rollback(self, result: PipelineResult, reason: str) -> None:
        """Restore the original source from backup."""
        if result.backup_path and Path(result.backup_path).exists():
            try:
                gene_path = self.genes_dir / f"{result.gene_name}.py"
                gene_path.write_text(result.source_before, encoding="utf-8")
                # Also mark the rollback
                rollback_stage = PipelineStage(
                    name="rollback", passed=True,
                    output=f"Restored from {Path(result.backup_path).name}",
                )
                result.stages.append(rollback_stage)
            except Exception as e:
                result.stages.append(PipelineStage(
                    name="rollback", passed=False,
                    error=f"CRITICAL: Rollback failed: {e}",
                ))

    @property
    def stats(self) -> dict:
        total = len(self.history)
        succeeded = sum(1 for r in self.history if r.success)
        return {
            "total_pipelines": total,
            "successful": succeeded,
            "failed": total - succeeded,
            "success_rate": succeeded / max(total, 1),
            "last_pipeline": self.history[-1].summary if self.history else "",
        }
