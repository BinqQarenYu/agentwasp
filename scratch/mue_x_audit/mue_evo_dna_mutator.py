"""Mutator — Intelligent code mutation engine with anti-cancer safeguards.

Generates and applies code mutations to the agent's DNA.
Key improvements over v0.6:
- Deduplication: never adds code that already exists in the gene
- Size limits: max 800 lines per gene, triggers mitosis signal
- Trading focus: mutations oriented toward trading capabilities
- Fitness scoring: evaluates mutation quality and updates gene fitness
- Pruning: removes dead code and duplicate patterns
"""

import ast
import hashlib
import random
from dataclasses import dataclass, field
from typing import Optional

PROTECTED_FILES = {"mutator", "genome", "inspector", "solidify"}
MAX_GENE_LINES = 500
MITOSIS_THRESHOLD = 350


@dataclass
class Mutation:
    """A single code transformation with metadata."""
    target_gene: str
    mutation_type: str
    old_hash: str
    new_source: str
    diff: str = ""
    reason: str = ""
    risk_level: float = 0.0
    validated: bool = False
    fitness_delta: float = 0.0
    triggers_mitosis: bool = False
    new_gene_name: str = ""


class Mutator:
    """Generates intelligent, deduplicated mutations for the agent's genetic code."""

    MUTATION_STRATEGIES = ["explore", "exploit", "repair", "optimize", "innovate", "prune"]

    def __init__(self, genome):
        self.genome = genome
        self.mutation_history: list[Mutation] = []
        self.stagnation_counter: dict[str, int] = {}
        self.applied_patterns: dict[str, set] = {}  # gene -> set of pattern hashes

    def propose_mutations(self, signal: str, strategy: str = "balanced") -> list[Mutation]:
        candidates = []
        signal_lower = signal.lower()

        weights = {
            "balanced": {"repair": 0.3, "optimize": 0.25, "explore": 0.2, "exploit": 0.15, "innovate": 0.1},
            "innovate": {"explore": 0.35, "innovate": 0.3, "optimize": 0.2, "repair": 0.1, "exploit": 0.05},
            "harden": {"optimize": 0.4, "repair": 0.35, "exploit": 0.15, "innovate": 0.05, "explore": 0.05},
            "repair-only": {"repair": 1.0},
        }
        w = weights.get(strategy, weights["balanced"])

        for name, gene in self.genome.genes.items():
            if name in PROTECTED_FILES:
                continue
            if self.stagnation_counter.get(name, 0) > 8:
                continue

            source = gene.source_path.read_text(encoding="utf-8") if gene.source_path.exists() else ""
            lines = source.split("\n")

            # Prune check: if gene is bloated, prune before anything else
            if len(lines) > MAX_GENE_LINES:
                mut = self._generate_prune(name, source, lines)
                if mut:
                    candidates.append(mut)
                    continue

            # Mitosis check: gene is large enough to split
            if len(lines) > MITOSIS_THRESHOLD:
                mut = self._generate_mitosis(name, source, lines)
                if mut:
                    candidates.append(mut)

            # Repair signals
            if any(e in signal_lower for e in ("error", "exception", "failed", "traceback", "timeout", "bug")):
                if random.random() < w["repair"]:
                    mut = self._generate_repair(name, source, signal)
                    if mut:
                        candidates.append(mut)

            # Optimization signals
            if any(o in signal_lower for o in ("slow", "optimize", "performance", "memory", "quality", "smell")):
                if random.random() < w["optimize"]:
                    mut = self._generate_optimization(name, source)
                    if mut:
                        candidates.append(mut)

            # Exploration: trading-focused new capabilities (NOT blind append)
            if random.random() < w["explore"] * 0.25:
                mut = self._generate_exploration(name, source, lines)
                if mut:
                    candidates.append(mut)

            # Exploit: reinforce what works
            if random.random() < w.get("exploit", 0.15):
                mut = self._generate_exploit(name, source)
                if mut:
                    candidates.append(mut)

            # Innovate: combine existing patterns
            if random.random() < w.get("innovate", 0.08):
                mut = self._generate_innovate(name, source)
                if mut:
                    candidates.append(mut)

        candidates.sort(key=lambda m: m.risk_level)
        return candidates

    def apply(self, mutation: Mutation) -> bool:
        try:
            ast.parse(mutation.new_source)

            self.genome.mutate_gene(
                mutation.target_gene,
                mutation.new_source,
                reason=mutation.reason,
            )
            mutation.validated = True
            self.mutation_history.append(mutation)
            self.stagnation_counter[mutation.target_gene] = 0

            # Track pattern for dedup — hash the SOURCE, not the reason.
            # Two mutations with different code but same reason must NOT collide.
            pattern_key = hashlib.sha256(mutation.new_source.encode()).hexdigest()[:16]
            if mutation.target_gene not in self.applied_patterns:
                self.applied_patterns[mutation.target_gene] = set()
            self.applied_patterns[mutation.target_gene].add(pattern_key)

            # Update fitness
            if mutation.target_gene in self.genome.genes:
                gene = self.genome.genes[mutation.target_gene]
                gene.fitness = min(1.0, gene.fitness + mutation.fitness_delta + 0.02)

            return True
        except (SyntaxError, Exception):
            self.stagnation_counter[mutation.target_gene] = \
                self.stagnation_counter.get(mutation.target_gene, 0) + 1
            return False

    # ═══════════════════════════════════════════════════════════════
    # PRUNE — Remove duplicate/dead code
    # ═══════════════════════════════════════════════════════════════

    def _generate_prune(self, name: str, source: str, lines: list) -> Optional[Mutation]:
        """Aggressively prune bloated genes. Remove duplicate functions and dead code."""
        seen_funcs: dict[str, int] = {}
        new_lines = []
        removed = 0

        for line in lines:
            stripped = line.strip()
            # Track ALL function/class definitions for dedup
            if stripped.startswith("def ") or stripped.startswith("class "):
                func_name = stripped.split("(")[0].split("(")[0].replace("def ", "").replace("class ", "").split(":")[0].strip()
                if func_name in seen_funcs:
                    removed += 1
                    continue
                seen_funcs[func_name] = len(new_lines)
            new_lines.append(line)

        if removed == 0:
            return None

        new_source = "\n".join(new_lines)
        return Mutation(
            target_gene=name,
            mutation_type="prune",
            old_hash=hashlib.sha256(source.encode()).hexdigest()[:16],
            new_source=new_source,
            reason=f"Pruned {removed} duplicate functions",
            risk_level=0.1,
            fitness_delta=0.05,
        )

    # ═══════════════════════════════════════════════════════════════
    # MITOSIS — Split large genes into new genes
    # ═══════════════════════════════════════════════════════════════

    def _generate_mitosis(self, name: str, source: str, lines: list) -> Optional[Mutation]:
        """Split a gene that has grown too large into two genes."""
        # Find class/function boundaries near the midpoint
        mid = len(lines) // 2
        split_point = mid
        for i in range(mid, min(mid + 50, len(lines))):
            if lines[i].strip().startswith("class ") or lines[i].strip().startswith("def "):
                split_point = i
                break

        if split_point == mid:
            return None

        first_half = "\n".join(lines[:split_point])
        # Keep the first half as the current gene
        return Mutation(
            target_gene=name,
            mutation_type="mitosis",
            old_hash=hashlib.sha256(source.encode()).hexdigest()[:16],
            new_source=first_half,
            reason=f"Mitosis: gene too large ({len(lines)} lines), splitting",
            risk_level=0.35,
            fitness_delta=0.08,
            triggers_mitosis=True,
            new_gene_name=f"{name}_child",
        )

    # ═══════════════════════════════════════════════════════════════
    # REPAIR — Add error handling
    # ═══════════════════════════════════════════════════════════════

    def _generate_repair(self, name: str, source: str, signal: str) -> Optional[Mutation]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None

        transformer = ErrorHandlerInjector(signal)
        try:
            new_tree = transformer.visit(tree)
            ast.fix_missing_locations(new_tree)
            new_source = ast.unparse(new_tree)
        except Exception:
            return None

        if new_source == source:
            return None

        return Mutation(
            target_gene=name,
            mutation_type="repair",
            old_hash=hashlib.sha256(source.encode()).hexdigest()[:16],
            new_source=new_source,
            reason=f"Added error handling for: {signal[:80]}",
            risk_level=0.15,
            fitness_delta=0.03,
        )

    # ═══════════════════════════════════════════════════════════════
    # OPTIMIZE — Real performance improvements
    # ═══════════════════════════════════════════════════════════════

    def _generate_optimization(self, name: str, source: str) -> Optional[Mutation]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None

        transformer = OptimizationInjector()
        try:
            new_tree = transformer.visit(tree)
            ast.fix_missing_locations(new_tree)
            new_source = ast.unparse(new_tree)
        except Exception:
            return None

        if new_source.strip() == source.strip():
            return None

        # Inject missing imports required by new AST nodes
        new_source = self._inject_imports(source, new_source)

        return Mutation(
            target_gene=name,
            mutation_type="optimize",
            old_hash=hashlib.sha256(source.encode()).hexdigest()[:16],
            new_source=new_source,
            reason="Performance optimization pass",
            risk_level=0.25,
            fitness_delta=0.04,
        )

    # ═══════════════════════════════════════════════════════════════
    # EXPLORE — Domain-agnostic new capabilities (ANTI-CANCER)
    # ═══════════════════════════════════════════════════════════════

    _exploration_pool = [
        # Domain-agnostic patterns — useful across ALL domains
        {
            "name": "retry_handler",
            "code": (
                "\n\nimport functools\nimport time\n\n"
                "def _retry_on_failure(max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):\n"
                '    """Decorator: retry a function on exception with exponential backoff."""\n'
                "    def decorator(func):\n"
                "        @functools.wraps(func)\n"
                "        def wrapper(*args, **kwargs):\n"
                "            _delay = delay\n"
                "            for attempt in range(max_retries):\n"
                "                try:\n"
                "                    return func(*args, **kwargs)\n"
                "                except Exception as e:\n"
                "                    if attempt == max_retries - 1:\n"
                "                        raise\n"
                "                    time.sleep(_delay)\n"
                "                    _delay *= backoff\n"
                "            return None\n"
                "        return wrapper\n"
                "    return decorator\n"
            ),
            "tags": ["general", "resilience", "error_handling"],
        },
        {
            "name": "safe_divider",
            "code": (
                "\n\ndef _safe_divide(a: float, b: float, default: float = 0.0) -> float:\n"
                '    """Divide safely, returning default when divisor is zero."""\n'
                "    return a / b if abs(b) > 1e-10 else default\n"
            ),
            "tags": ["general", "math", "safety"],
        },
        {
            "name": "batch_processor",
            "code": (
                "\n\ndef _process_in_batches(items: list, batch_size: int = 100, processor=None):\n"
                '    """Process a large list in batches to manage memory."""\n'
                "    results = []\n"
                "    for i in range(0, len(items), batch_size):\n"
                "        batch = items[i:i + batch_size]\n"
                "        if processor:\n"
                "            results.extend(processor(item) for item in batch)\n"
                "        else:\n"
                "            results.extend(batch)\n"
                "    return results\n"
            ),
            "tags": ["general", "performance", "memory"],
        },
        {
            "name": "circuit_breaker",
            "code": (
                "\n\nimport time\n\n"
                "class _CircuitBreaker:\n"
                '    """Prevents cascading failures by stopping calls after threshold."""\n'
                "    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):\n"
                "        self.failure_threshold = failure_threshold\n"
                "        self.recovery_timeout = recovery_timeout\n"
                "        self._failures = 0\n"
                "        self._last_failure_time = 0.0\n"
                "        self._state = 'closed'\n"
                "    @property\n"
                "    def is_open(self) -> bool:\n"
                "        if self._state == 'closed':\n"
                "            return False\n"
                "        if time.time() - self._last_failure_time > self.recovery_timeout:\n"
                "            self._state = 'half_open'\n"
                "            return False\n"
                "        return True\n"
                "    def record_failure(self):\n"
                "        self._failures += 1\n"
                "        self._last_failure_time = time.time()\n"
                "        if self._state == 'half_open' or self._failures >= self.failure_threshold:\n"
                "            self._state = 'open'\n"
                "    def record_success(self):\n"
                "        self._failures = 0\n"
                "        self._state = 'closed'\n"
            ),
            "tags": ["general", "resilience", "stability"],
        },
        {
            "name": "config_loader",
            "code": (
                "\n\nimport json\nfrom pathlib import Path\n\n"
                "def _load_config_with_defaults(config_path: str, defaults: dict) -> dict:\n"
                '    """Load JSON config, merging with sensible defaults for missing keys."""\n'
                "    path = Path(config_path)\n"
                "    if not path.exists():\n"
                "        return defaults\n"
                "    loaded = json.loads(path.read_text(encoding='utf-8'))\n"
                "    merged = {**defaults, **loaded}\n"
                "    return merged\n"
            ),
            "tags": ["general", "config", "startup"],
        },
        {
            "name": "metrics_collector",
            "code": (
                "\n\nimport time\nfrom collections import defaultdict\n\n"
                "class _MetricsCollector:\n"
                '    """Lightweight metrics: count, timing, and success/failure tracking."""\n'
                "    def __init__(self):\n"
                "        self.counters = defaultdict(int)\n"
                "        self.timings = defaultdict(list)\n"
                "        self.outcomes = defaultdict(lambda: {'success': 0, 'failure': 0})\n"
                "    def track(self, name: str, elapsed: float, success: bool):\n"
                "        self.counters[name] += 1\n"
                "        self.timings[name].append(elapsed)\n"
                "        key = 'success' if success else 'failure'\n"
                "        self.outcomes[name][key] += 1\n"
                "    def avg_time(self, name: str) -> float:\n"
                "        vals = self.timings.get(name, [])\n"
                "        return sum(vals) / len(vals) if vals else 0.0\n"
                "    def success_rate(self, name: str) -> float:\n"
                "        o = self.outcomes[name]\n"
                "        total = o['success'] + o['failure']\n"
                "        return o['success'] / max(total, 1)\n"
            ),
            "tags": ["general", "monitoring", "analytics"],
        },
        {
            "name": "async_gather",
            "code": (
                "\n\nimport asyncio\n\n"
                "async def _gather_with_timeout(coros: list, timeout: float = 30.0, default=None):\n"
                '    """Run multiple coroutines concurrently with a timeout."""\n'
                "    try:\n"
                "        results = await asyncio.wait_for(asyncio.gather(*coros, return_exceptions=True), timeout=timeout)\n"
                "        return [r if not isinstance(r, Exception) else default for r in results]\n"
                "    except asyncio.TimeoutError:\n"
                "        return [default] * len(coros)\n"
            ),
            "tags": ["general", "async", "performance"],
        },
        {
            "name": "rate_limiter",
            "code": (
                "\n\nimport time\nimport threading\n\n"
                "class _RateLimiter:\n"
                '    """Token-bucket rate limiter for API calls."""\n'
                "    def __init__(self, max_calls: int = 10, period: float = 1.0):\n"
                "        self.max_calls = max_calls\n"
                "        self.period = period\n"
                "        self._tokens = max_calls\n"
                "        self._last_refill = time.time()\n"
                "        self._lock = threading.Lock()\n"
                "    def acquire(self) -> bool:\n"
                "        with self._lock:\n"
                "            now = time.time()\n"
                "            elapsed = now - self._last_refill\n"
                "            self._tokens = min(self.max_calls, self._tokens + elapsed * (self.max_calls / self.period))\n"
                "            self._last_refill = now\n"
                "            if self._tokens >= 1.0:\n"
                "                self._tokens -= 1.0\n"
                "                return True\n"
                "            return False\n"
            ),
            "tags": ["general", "api", "throttling"],
        },
        {
            "name": "type_validator",
            "code": (
                "\n\ndef _validate_kwargs(kwargs: dict, required: list[str], optional: list[str] = None) -> dict:\n"
                '    """Validate and extract typed keyword arguments."""\n'
                "    result = {}\n"
                "    missing = [k for k in required if k not in kwargs]\n"
                "    if missing:\n"
                "        raise ValueError(f'Missing required arguments: {missing}')\n"
                "    for key in required:\n"
                "        result[key] = kwargs[key]\n"
                "    if optional:\n"
                "        for key in optional:\n"
                "            if key in kwargs:\n"
                "                result[key] = kwargs[key]\n"
                "    return result\n"
            ),
            "tags": ["general", "validation", "safety"],
        },
        {
            "name": "lazy_property",
            "code": (
                "\n\nclass _LazyProperty:\n"
                '    """Descriptor: compute once, cache forever."""\n'
                "    def __init__(self, func):\n"
                "        self.func = func\n"
                "        self.name = func.__name__\n"
                "    def __get__(self, obj, cls):\n"
                "        if obj is None:\n"
                "            return self\n"
                "        value = self.func(obj)\n"
                "        obj.__dict__[self.name] = value\n"
                "        return value\n"
            ),
            "tags": ["general", "performance", "caching"],
        },
    ]
    _exploration_index = 0

    def _generate_exploration(self, name: str, source: str, lines: list) -> Optional[Mutation]:
        """Generate domain-agnostic exploration mutations with dedup protection."""
        # SIZE LIMIT: don't append if gene is already large
        if len(lines) > MITOSIS_THRESHOLD:
            return None

        # Try each pattern, cycling through the pool
        for _ in range(len(self._exploration_pool)):
            pattern = self._exploration_pool[self._exploration_index % len(self._exploration_pool)]
            self._exploration_index += 1

            # DEDUP: check if this pattern already exists in the gene
            if pattern["name"] in source:
                continue

            # DEDUP: check if we've already applied this to this gene
            if name in self.applied_patterns and pattern["name"] in self.applied_patterns[name]:
                continue

            new_source = source + pattern["code"]
            return Mutation(
                target_gene=name,
                mutation_type="explore",
                old_hash=hashlib.sha256(source.encode()).hexdigest()[:16],
                new_source=new_source,
                reason=f"Exploration: added {pattern['name']} ({pattern['tags'][0]})",
                risk_level=0.3,
                fitness_delta=0.02,
            )

        return None  # All patterns already exist in this gene

    # ═══════════════════════════════════════════════════════════════
    # EXPLOIT — Reinforce successful patterns
    # ═══════════════════════════════════════════════════════════════

    def _generate_exploit(self, name: str, source: str) -> Optional[Mutation]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None

        transformer = ExploitInjector()
        try:
            new_tree = transformer.visit(tree)
            ast.fix_missing_locations(new_tree)
            new_source = ast.unparse(new_tree)
        except Exception:
            return None

        if new_source.strip() == source.strip():
            return None

        # Inject missing imports
        new_source = self._inject_imports(source, new_source)

        return Mutation(
            target_gene=name,
            mutation_type="exploit",
            old_hash=hashlib.sha256(source.encode()).hexdigest()[:16],
            new_source=new_source,
            reason="Exploit: reinforced successful patterns",
            risk_level=0.2,
            fitness_delta=0.03,
        )

    # ═══════════════════════════════════════════════════════════════
    # INNOVATE — Combine existing patterns
    # ═══════════════════════════════════════════════════════════════

    def _generate_innovate(self, name: str, source: str) -> Optional[Mutation]:
        caps = self.genome.capsules
        if caps and random.random() < 0.4:
            capsule = random.choice(list(caps.values()))
            injection = (
                f"\n# Innovation: combined capsule '{capsule.name}'\n"
                f"def _compose_{capsule.name.replace(' ', '_').replace('-', '_')}():\n"
                f"    # Genes: {', '.join(capsule.gene_names)}\n"
                f"    # {capsule.description}\n"
                f"    pass\n"
            )
            if injection.split("def _")[1].split("(")[0] in source:
                return None
            return Mutation(
                target_gene=name,
                mutation_type="innovate",
                old_hash=hashlib.sha256(source.encode()).hexdigest()[:16],
                new_source=source + injection,
                reason=f"Innovation: capsule '{capsule.name}'",
                risk_level=0.45,
                fitness_delta=0.01,
            )
        return None

    def _inject_imports(self, original_source: str, new_source: str) -> str:
        """Ensure required imports exist when AST transformers add new references."""
        imports_to_add = []
        # Check for functools.lru_cache usage
        if 'functools' in new_source and 'functools' not in original_source:
            if 'import functools' not in new_source.split('\n')[0:20]:
                imports_to_add.append('import functools')
        # Check for time usage (from exploration patterns)
        if 'time.' in new_source and 'time' not in original_source:
            if 'import time' not in new_source.split('\n')[0:20]:
                imports_to_add.append('import time')
        # Check for threading usage
        if 'threading.' in new_source and 'threading' not in original_source:
            if 'import threading' not in new_source.split('\n')[0:20]:
                imports_to_add.append('import threading')
        # Check for json usage
        if 'json.' in new_source and 'json' not in original_source:
            if 'import json' not in new_source.split('\n')[0:20]:
                imports_to_add.append('import json')
        # Check for asyncio usage
        if 'asyncio.' in new_source and 'asyncio' not in original_source:
            if 'import asyncio' not in new_source.split('\n')[0:20]:
                imports_to_add.append('import asyncio')
        # Check for collections usage
        if 'defaultdict' in new_source and 'collections' not in original_source:
            if 'from collections' not in new_source.split('\n')[0:20]:
                imports_to_add.append('from collections import defaultdict')

        if not imports_to_add:
            return new_source

        lines = new_source.split('\n')
        # Insert imports after existing imports or at line 1
        insert_at = 1
        for i, line in enumerate(lines):
            if line.startswith(('import ', 'from ')):
                insert_at = i + 1
        for imp in imports_to_add:
            lines.insert(insert_at, imp)
            insert_at += 1
        return '\n'.join(lines)

    @property
    def stats(self) -> dict:
        return {
            "total_mutations": len(self.mutation_history),
            "recent_types": [m.mutation_type for m in self.mutation_history[-10:]],
            "stagnant_genes": {k: v for k, v in self.stagnation_counter.items() if v > 3},
        }


# ═══════════════════════════════════════════════════════════════
# AST TRANSFORMERS
# ═══════════════════════════════════════════════════════════════

class ErrorHandlerInjector(ast.NodeTransformer):
    """Wraps unprotected function calls in try/except."""

    def __init__(self, error_signal: str):
        self.error_signal = error_signal

    def visit_Expr(self, node):
        if isinstance(node.value, ast.Call):
            handler = ast.ExceptHandler(
                type=ast.Name(id="Exception", ctx=ast.Load()),
                name="e",
                body=[ast.Expr(value=ast.Call(
                    func=ast.Name(id="print", ctx=ast.Load()),
                    args=[ast.JoinedStr(values=[
                        ast.Constant(value=f"[EVO] Error: "),
                        ast.FormattedValue(value=ast.Name(id="e", ctx=ast.Load()), conversion=-1),
                    ])],
                    keywords=[],
                ))],
            )
            return ast.Try(body=[node], handlers=[handler], orelse=[], finalbody=[])
        return node


class OptimizationInjector(ast.NodeTransformer):
    """Applies real AST-level optimizations: constant folding, join replacement, lru_cache."""

    def __init__(self):
        self.changes_made = False

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Add):
            parts = []
            current = node
            while isinstance(current, ast.BinOp) and isinstance(current.op, ast.Add):
                parts.append(current.right)
                current = current.left
            parts.append(current)
            parts.reverse()
            if len(parts) >= 3 and all(isinstance(p, ast.Constant) and isinstance(p.value, str) for p in parts):
                folded = "".join(p.value for p in parts)
                self.changes_made = True
                return ast.Constant(value=folded)
        return node

    def visit_For(self, node):
        self.generic_visit(node)
        # Detect: for x in y: result.append(f(x)) → list comprehension
        if (len(node.body) == 1 and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Call)):
            call = node.body[0].value
            if (isinstance(call.func, ast.Attribute)
                    and call.func.attr == 'append'
                    and isinstance(call.func.value, ast.Name)):
                list_name = call.func.value.id
                if len(call.args) == 1:
                    elt = ast.unparse(call.args[0])
                    target = ast.unparse(node.target)
                    iter_src = ast.unparse(node.iter)
                    new_src = f"{list_name}.extend([{elt.replace(target, '_' + target)} for _{target} in {iter_src}])"
                    try:
                        new_tree = ast.parse(new_src)
                        self.changes_made = True
                        return ast.Expr(value=new_tree.body[0].value)
                    except SyntaxError:
                        pass
        return node

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        has_yield = any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node))
        has_global = any(isinstance(n, ast.Global) for n in ast.walk(node))
        has_io = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id in ('print', 'open', 'input') for n in ast.walk(node))
        if not has_yield and not has_global and not has_io and len(node.args.args) >= 1:
            docstring = ast.get_docstring(node)
            is_pure = docstring and "pure" in docstring.lower()
            already_cached = any(
                isinstance(d, ast.Call) and hasattr(d.func, 'attr') and d.func.attr == 'lru_cache'
                for d in node.decorator_list
            )
            if is_pure and not already_cached:
                cache_decorator = ast.Call(
                    func=ast.Attribute(value=ast.Name(id='functools', ctx=ast.Load()), attr='lru_cache', ctx=ast.Load()),
                    args=[ast.Constant(value=128)],
                    keywords=[],
                )
                node.decorator_list.insert(0, cache_decorator)
                self.changes_made = True
        return node


class ExploitInjector(ast.NodeTransformer):
    """Reinforces successful patterns: adds type hints, __repr__, @property, and import cleanup."""

    def __init__(self):
        self.changes_made = False

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        has_yield = any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node))
        has_global = any(isinstance(n, ast.Global) for n in ast.walk(node))
        if not has_yield and not has_global and len(node.args.args) >= 1:
            docstring = ast.get_docstring(node)
            if docstring and "cache" not in docstring.lower() and "pure" in docstring.lower():
                already_cached = any(
                    isinstance(d, ast.Call) and hasattr(d.func, 'attr') and d.func.attr == 'lru_cache'
                    for d in node.decorator_list
                )
                if not already_cached:
                    cache_decorator = ast.Call(
                        func=ast.Attribute(value=ast.Name(id='functools', ctx=ast.Load()), attr='lru_cache', ctx=ast.Load()),
                        args=[ast.Constant(value=128)],
                        keywords=[],
                    )
                    node.decorator_list.insert(0, cache_decorator)
                    self.changes_made = True
        return node

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        has_repr = any(n.name == '__repr__' for n in node.body if isinstance(n, ast.FunctionDef))
        has_dataclass = any(
            isinstance(d, ast.Name) and d.id == 'dataclass'
            for d in node.decorator_list
        )
        if not has_repr and not has_dataclass:
            fields = [n for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
            if fields:
                field_names = [f.target.id for f in fields[:5]]
                fmt_parts = ", ".join(f"{n}={{{n}!r}}" for n in field_names)
                repr_body = ast.parse(
                    f"def __repr__(self): return f\"{node.name}({fmt_parts})\""
                ).body[0]
                node.body.append(repr_body)
                self.changes_made = True
        return node
