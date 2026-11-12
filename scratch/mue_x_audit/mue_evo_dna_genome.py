"""Genome — The agent's DNA represented as executable code modules.

Genes are individual source files that can be mutated.
Capsules are validated gene combinations that solve specific problem classes.
The genome is self-describing: it can read, understand, and rewrite itself.

KERNEL INTEGRITY: PROTECTED_FILES (mutator, genome, inspector, solidify) are
hash-verified before any mutation. If a protected file's hash doesn't match the
sealed kernel hash, mutation is rejected. This prevents silent corruption of the
agent's core — the genes that control evolution itself must remain trustworthy.
"""

import ast
import hashlib
import json
import sys
import importlib.util
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# ═══════════════════════════════════════════════════════════════
# KERNEL INTEGRITY — Files that must NOT be silently mutated
# ═══════════════════════════════════════════════════════════════
KERNEL_FILES = {"mutator", "genome", "inspector", "solidify"}
KERNEL_HASHES: dict[str, str] = {}  # gene_name → sha256_hex


class KernelIntegrityError(Exception):
    """Raised when a kernel gene mutation is rejected due to hash mismatch."""
    pass

@dataclass
class Gene:
    """A single evolvable unit — one source file or code block."""
    name: str
    source_path: Path
    content_hash: str
    ast_tree: Optional[ast.AST] = None
    fitness: float = 0.0
    mutation_count: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_mutated: Optional[str] = None
    parent_hash: Optional[str] = None
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> "Gene":
        content = path.read_text(encoding="utf-8")
        h = hashlib.sha256(content.encode()).hexdigest()[:16]
        try:
            tree = ast.parse(content)
        except SyntaxError:
            tree = None
        return cls(
            name=path.stem,
            source_path=path,
            content_hash=h,
            ast_tree=tree,
            tags=[path.suffix.replace(".", "")],
        )

    @classmethod
    def from_source(cls, name: str, source: str, save_dir: Path) -> "Gene":
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / f"{name}.py"
        path.write_text(source, encoding="utf-8")
        return cls.from_file(path)


@dataclass
class Capsule:
    """A battle-tested combination of genes that solves a problem class."""
    name: str
    description: str
    gene_names: list[str]
    trigger_signals: list[str]  # Keywords/patterns that activate this capsule
    success_rate: float = 0.0
    use_count: int = 0
    problem_class: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Genome:
    """The complete genetic material of the agent — all genes + capsules."""

    def __init__(self, root_dir: Path):
        self.root_dir = Path(root_dir)
        self.genes_dir = self.root_dir / "genes"
        self.capsules_dir = self.root_dir / "capsules"
        self.genes_dir.mkdir(parents=True, exist_ok=True)
        self.capsules_dir.mkdir(parents=True, exist_ok=True)

        self.genes: dict[str, Gene] = {}
        self.capsules: dict[str, Capsule] = {}
        self._live_patches: dict[str, Callable] = {}  # Monkey-patched functions
        self._evolution_log: list[dict] = []
        self._base_mutation_count: int = 0  # Persisted across restarts

    def seal_kernel(self) -> dict[str, str]:
        """Record SHA256 hashes of all KERNEL_FILES. Call once after bootstrap."""
        global KERNEL_HASHES
        for gene_name in KERNEL_FILES:
            gene_path = self.genes_dir / f"{gene_name}.py"
            if gene_path.exists():
                content = gene_path.read_text(encoding="utf-8")
                h = hashlib.sha256(content.encode()).hexdigest()
                KERNEL_HASHES[gene_name] = h
        return dict(KERNEL_HASHES)

    def verify_kernel(self) -> tuple[bool, list[str]]:
        """Check that all KERNEL_FILES match their sealed hashes.
        Returns (is_intact, list_of_violated_genes).
        """
        violations = []
        for gene_name, sealed_hash in KERNEL_HASHES.items():
            gene_path = self.genes_dir / f"{gene_name}.py"
            if not gene_path.exists():
                violations.append(f"{gene_name}: FILE MISSING")
                continue
            current = hashlib.sha256(
                gene_path.read_text(encoding="utf-8").encode()
            ).hexdigest()
            if current != sealed_hash:
                violations.append(
                    f"{gene_name}: hash mismatch (sealed={sealed_hash[:12]}..., current={current[:12]}...)"
                )
        return len(violations) == 0, violations

    def is_kernel_intact(self, gene_name: str) -> bool:
        """Check if a specific gene's hash matches the sealed kernel hash."""
        if gene_name not in KERNEL_FILES:
            return True  # Non-kernel genes aren't protected
        if gene_name not in KERNEL_HASHES:
            return True  # Not yet sealed — allow (bootstrap phase)
        gene_path = self.genes_dir / f"{gene_name}.py"
        if not gene_path.exists():
            return False
        current = hashlib.sha256(
            gene_path.read_text(encoding="utf-8").encode()
        ).hexdigest()
        return current == KERNEL_HASHES[gene_name]

    def scan(self) -> int:
        """Discover all genes and capsules from the filesystem. Non-destructive."""
        # M1 FIX: Don't clear before scanning — only remove genes whose files vanished
        scanned_names: set[str] = set()
        for py_file in self.genes_dir.rglob("*.py"):
            gene = Gene.from_file(py_file)
            self.genes[gene.name] = gene
            scanned_names.add(gene.name)

        # Remove genes whose files no longer exist on disk
        stale = [n for n in self.genes if n not in scanned_names]
        for n in stale:
            del self.genes[n]

        # Reload capsules (preserve existing)
        for caps_file in self.capsules_dir.glob("*.json"):
            if caps_file.name.startswith("_"):
                continue
            try:
                data = json.loads(caps_file.read_text(encoding="utf-8"))
                capsule = Capsule(
                    name=data["name"],
                    description=data.get("description", ""),
                    gene_names=data.get("gene_names", []),
                    trigger_signals=data.get("trigger_signals", []),
                    problem_class=data.get("problem_class", ""),
                )
                self.capsules[capsule.name] = capsule
            except (json.JSONDecodeError, KeyError):
                pass
        return len(scanned_names)

    def add_gene(self, name: str, source: str) -> Gene:
        gene = Gene.from_source(name, source, self.genes_dir)
        self.genes[name] = gene
        self._log_event("gene_added", {"name": name, "hash": gene.content_hash})
        return gene

    def remove_gene(self, name: str) -> bool:
        if name not in self.genes:
            return False
        gene = self.genes[name]
        if gene.source_path.exists():
            gene.source_path.unlink()
        del self.genes[name]
        self._log_event("gene_removed", {"name": name})
        return True

    def mutate_gene(self, name: str, new_source: str, reason: str = "", force: bool = False) -> Gene:
        """Rewrite a gene's source code — the core evolutionary operation.

        Kernel genes (mutator, genome, inspector, solidify) are hash-verified
        before mutation. If the file has been tampered with outside the approved
        path, mutation is rejected unless force=True.
        """
        old_hash = None
        old_mutation_count = 0
        old_fitness = 0.0

        if name in self.genes:
            old_gene = self.genes[name]
            old_hash = old_gene.content_hash
            old_mutation_count = old_gene.mutation_count
            old_fitness = old_gene.fitness

        # KERNEL INTEGRITY: reject mutation if kernel hash doesn't match
        if name in KERNEL_FILES and name in KERNEL_HASHES and not force:
            gene_path = self.genes_dir / f"{name}.py"
            if gene_path.exists():
                current_hash = hashlib.sha256(
                    gene_path.read_text(encoding="utf-8").encode()
                ).hexdigest()
                if current_hash != KERNEL_HASHES[name]:
                    raise KernelIntegrityError(
                        f"Cannot mutate kernel gene '{name}': hash mismatch. "
                        f"File may have been tampered with. Use force=True to override."
                    )

        new_gene = Gene.from_source(name, new_source, self.genes_dir)
        new_gene.mutation_count = old_mutation_count + 1
        new_gene.parent_hash = old_hash
        new_gene.last_mutated = datetime.now(timezone.utc).isoformat()
        new_gene.fitness = old_fitness  # Preserve fitness across mutations

        self.genes[name] = new_gene
        self._log_event("gene_mutated", {
            "name": name, "old_hash": old_hash,
            "new_hash": new_gene.content_hash, "reason": reason,
        })
        return new_gene

    def add_capsule(self, capsule: Capsule) -> None:
        """Register a validated gene combination."""
        self.capsules[capsule.name] = capsule
        caps_path = self.capsules_dir / f"{capsule.name}.json"
        caps_path.write_text(json.dumps({
            "name": capsule.name,
            "description": capsule.description,
            "gene_names": capsule.gene_names,
            "trigger_signals": capsule.trigger_signals,
            "problem_class": capsule.problem_class,
        }, indent=2))

    def find_capsule(self, signal: str) -> Optional[Capsule]:
        """Match a signal to the best capsule for this problem."""
        best, best_score = None, 0
        for caps in self.capsules.values():
            score = sum(1 for s in caps.trigger_signals if s.lower() in signal.lower())
            weighted = score * (1 + caps.success_rate) * (1 + 0.01 * caps.use_count)
            if weighted > best_score:
                best, best_score = caps, weighted
        if best:
            best.use_count += 1
        return best

    def monkey_patch(self, func_name: str, new_func: Callable) -> None:
        """Runtime self-modification — replace a function while running."""
        self._live_patches[func_name] = new_func
        self._log_event("monkey_patched", {"function": func_name})

    def get_patch(self, func_name: str) -> Optional[Callable]:
        return self._live_patches.get(func_name)

    def _log_event(self, event_type: str, data: dict) -> None:
        self._evolution_log.append({
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        })

    @property
    def stats(self) -> dict:
        return {
            "gene_count": len(self.genes),
            "capsule_count": len(self.capsules),
            "total_mutations": self._base_mutation_count + sum(g.mutation_count for g in self.genes.values()),
            "live_patches": len(self._live_patches),
            "evolution_events": len(self._evolution_log),
            "avg_fitness": sum(g.fitness for g in self.genes.values()) / max(len(self.genes), 1),
        }

    def export_dna_snapshot(self) -> dict:
        """Full serializable snapshot of the agent's DNA."""
        return {
            "stats": self.stats,
            "genes": {name: {
                "hash": g.content_hash,
                "mutations": g.mutation_count,
                "fitness": g.fitness,
                "last_mutated": g.last_mutated,
                "tags": g.tags,
            } for name, g in self.genes.items()},
            "capsules": {name: {
                "description": c.description,
                "genes": c.gene_names,
                "success_rate": c.success_rate,
                "use_count": c.use_count,
            } for name, c in self.capsules.items()},
            "evolution_log": self._evolution_log[-50:],  # Last 50 events
        }
