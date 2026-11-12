"""GeneSandbox — Isolated subprocess execution for gene tests.

Mutations can introduce infinite loops, os.system("rm -rf /"), or other
dangerous code. Running gene tests in a subprocess sandbox with timeout
prevents a rogue gene from taking down the whole agent.
"""

import subprocess
import sys
import time
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed


@dataclass
class SandboxResult:
    """Result of a sandboxed gene execution."""
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    timeout: bool = False
    duration_ms: float = 0.0
    error: str = ""
    gene_name: str = ""


class GeneSandbox:
    """Executes gene code in an isolated subprocess with timeout.

    Usage:
        sandbox = GeneSandbox()
        result = sandbox.test_gene("web_scout", gene_source)
        if result.success:
            print("Gene is safe to apply")
    """

    DEFAULT_TIMEOUT = 5.0  # seconds
    MAX_TIMEOUT = 15.0

    def __init__(self, timeout: float = None):
        self.timeout = timeout or self.DEFAULT_TIMEOUT
        self.history: list[SandboxResult] = []

    def test_gene(self, gene_name: str, source: str,
                  test_inputs: list = None) -> SandboxResult:
        """Execute a gene in a subprocess sandbox.

        Writes the gene to a temp file, runs `python -c "import <gene>"`
        in a subprocess with timeout. Catches fatal errors.
        """
        start = time.perf_counter()

        # Write gene to temp file
        tmpdir = Path(tempfile.mkdtemp(prefix="mue_sandbox_"))
        gene_file = tmpdir / f"{gene_name}.py"
        test_file = tmpdir / "test_runner.py"

        try:
            gene_file.write_text(source, encoding="utf-8")

            # Build a test script that imports and exercises the gene
            test_code = f'''
import sys
import os
sys.path.insert(0, r"{tmpdir}")

# Try importing the gene module
try:
    import {gene_name}
except Exception as e:
    print(f"SANDBOX_IMPORT_ERROR: {{e}}", file=sys.stderr)
    sys.exit(1)

# List public functions
funcs = [name for name in dir({gene_name})
         if not name.startswith("_")
         and callable(getattr({gene_name}, name, None))]

# Try calling no-arg functions
tested = 0
for fname in funcs[:5]:
    try:
        func = getattr({gene_name}, fname)
        result = func()
        tested += 1
    except TypeError:
        pass  # Function expects args — skip
    except Exception as e:
        print(f"SANDBOX_RUNTIME_ERROR: {{fname}}() -> {{e}}", file=sys.stderr)

print(f"SANDBOX_OK: imported={{True}} funcs={{len(funcs)}} tested={{tested}}")
sys.exit(0)
'''
            test_file.write_text(test_code, encoding="utf-8")

            # Run in subprocess with timeout
            proc = subprocess.run(
                [sys.executable, str(test_file)],
                capture_output=True, text=True,
                timeout=min(self.timeout, self.MAX_TIMEOUT),
                cwd=str(tmpdir),
            )

            duration = (time.perf_counter() - start) * 1000

            result = SandboxResult(
                success=(proc.returncode == 0),
                stdout=proc.stdout[:5000],
                stderr=proc.stderr[:5000],
                exit_code=proc.returncode,
                timeout=False,
                duration_ms=duration,
                gene_name=gene_name,
            )

        except subprocess.TimeoutExpired:
            duration = (time.perf_counter() - start) * 1000
            result = SandboxResult(
                success=False,
                timeout=True,
                duration_ms=duration,
                gene_name=gene_name,
                error=f"Gene '{gene_name}' timed out after {self.timeout}s — possible infinite loop",
            )

        except Exception as e:
            duration = (time.perf_counter() - start) * 1000
            result = SandboxResult(
                success=False,
                duration_ms=duration,
                gene_name=gene_name,
                error=str(e)[:500],
            )

        finally:
            # Cleanup temp files
            try:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass

        self.history.append(result)
        if len(self.history) > 50:
            self.history = self.history[-25:]
        return result

    def quick_validate(self, gene_name: str, source: str) -> tuple[bool, str]:
        """Fast syntax + import check. Returns (ok, reason)."""
        import ast
        try:
            ast.parse(source)
        except SyntaxError as e:
            return False, f"Syntax error: {e}"

        # Quick subprocess import check
        result = self.test_gene(gene_name, source)
        if result.timeout:
            return False, result.error
        if result.exit_code != 0:
            reason = result.stderr.split("SANDBOX_IMPORT_ERROR:")[-1].strip().split("\n")[0] if "SANDBOX_IMPORT_ERROR" in result.stderr else f"Exit code {result.exit_code}"
            return False, reason[:200]
        return True, "OK"

    def validate_batch(self, genes: list[tuple[str, str]], max_workers: int = 4) -> list[tuple[str, bool, str]]:
        """Validate multiple genes in parallel. Returns [(gene_name, ok, reason), ...]."""
        results = []
        with ThreadPoolExecutor(max_workers=min(max_workers, len(genes))) as executor:
            futures = {
                executor.submit(self.quick_validate, name, source): name
                for name, source in genes
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    ok, reason = future.result()
                    results.append((name, ok, reason))
                except Exception as e:
                    results.append((name, False, f"Validation error: {e}"))
        return results

    @property
    def stats(self) -> dict:
        total = len(self.history)
        passed = sum(1 for r in self.history if r.success)
        timeouts = sum(1 for r in self.history if r.timeout)
        return {
            "tests_run": total,
            "passed": passed,
            "failed": total - passed,
            "timeouts": timeouts,
            "success_rate": passed / max(total, 1),
        }
