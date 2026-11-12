"""Autonomous Signal Generator — The breathing heart of MUE.

Without this, MUE is a sensory-deprived agent waiting for external input.
With this, MUE generates its OWN reasons to evolve: self-analysis,
curiosity drives, quality audits, and stagnation pressure.

This is what transforms MUE from a passive framework into a truly
autonomous self-evolving agent that never stops improving.
"""

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path

from ..evolution.signals import Signal, SignalDetector as _SD


class AutonomousSignalGenerator:
    """Generates evolution signals from internal drives — no external input needed.

    This is the key insight: a self-evolving agent must be self-motivated.
    It doesn't just react to errors; it proactively seeks improvement.
    """

    def __init__(self, inspector=None, genome=None, persona=None, emotions=None,
                 memory=None, work_dir: Path = None):
        self.inspector = inspector
        self.genome = genome
        self.persona = persona
        self.emotions = emotions
        self.memory = memory      # MemoryLattice — now actually USED for decisions
        self.work_dir = work_dir or Path(".")
        self.total_signals_generated = 0
        self.cycles_without_mutations = 0
        self._pressure_boost = 1.0
        self._last_quality_audit = 0.0
        self._last_market_check = 0.0

    def tick(self, cycle_count: int, evolution_stats: dict) -> list:
        """Generate all autonomous signals for one evolution cycle.

        Returns a list of Signal objects ready to inject into SignalDetector.
        """
        signals = []
        mutation_count = evolution_stats.get("total_mutations_applied", 0)

        # Track stagnation
        if mutation_count == 0:
            self.cycles_without_mutations += 1
        else:
            self.cycles_without_mutations = 0
            self._pressure_boost = max(1.0, self._pressure_boost * 0.8)

        # Drive 1: Self-analysis — what needs improvement?
        signals.extend(self.self_analyze())

        # Drive 2: Curiosity — random exploration
        signals.extend(self.random_exploration_drive(cycle_count))

        # Drive 3: Stagnation detection — pressure to evolve
        signals.extend(self.stagnation_detection(cycle_count))

        # Drive 4: Code quality audit (periodic)
        signals.extend(self.code_quality_audit())

        # Drive 5: Domain-context-aware analysis (adapts to current specialization)
        domain = "general"
        config_path = self.work_dir / "mue_config.json"
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text("utf-8"))
                domain = config.get("domain", "general")
            except Exception:
                pass
        signals.extend(self.domain_context_analysis(domain))

        # Drive 6: Creative synthesis — cross-domain idea generation
        signals.extend(self.creative_synthesis_drive(cycle_count))

        # Drive 7: Proactive initiative — what COULD MUE become?
        signals.extend(self.proactive_initiative_drive(cycle_count))

        self.total_signals_generated += len(signals)
        return signals

    def self_analyze(self) -> list:
        """Analyze own genes and generate improvement signals.

        Now queries the memory lattice to enrich signals with past outcomes:
        - Genes with repeated failures get higher severity
        - Genes with successful past mutations get lower urgency
        - Memory enriches context with actual history, not just static analysis
        """
        signals = []
        if not self.genome or not self.inspector:
            return signals

        try:
            targets = self.inspector.find_improvement_targets()
            for target in targets[:5]:
                gene_name = target.get("name", "unknown")
                urgency = target.get("urgency", 0.0)
                reasons = target.get("reasons", [])

                # MEMORY LOOP: query past outcomes for this gene
                if self.memory and gene_name != "unknown":
                    try:
                        past = self.memory.search_fts(
                            f"gene {gene_name}", layers=[1, 2, 5], limit=5
                        )
                        failures = [m for m in past if m.weight < 0]
                        successes = [m for m in past if m.weight > 0.5]

                        # Amplify urgency if gene has repeated failures
                        if len(failures) >= 2:
                            urgency = min(0.99, urgency * 1.5)
                            reasons.append(f"{len(failures)} past failures in memory")

                        # Reduce urgency if gene has consistent successes
                        if len(successes) >= 3 and urgency < 0.5:
                            urgency *= 0.7  # Not urgent — it's been working

                        # Add memory-sourced context
                        recent_errors = [m.content[:80] for m in failures[-2:]]
                        if recent_errors:
                            target["memory_errors"] = recent_errors
                    except Exception:
                        pass  # Memory optional — don't break without it

                if urgency > 0.3:
                    signals.append(Signal(
                        type="opportunity",
                        source="self_analysis",
                        message=f"Gene '{gene_name}' needs improvement: {'; '.join(reasons)}",
                        severity=min(0.9, urgency),
                        context={
                            "gene": gene_name,
                            "urgency": urgency,
                            "reasons": reasons,
                            "memory_enriched": self.memory is not None,
                        },
                    ))
        except Exception:
            pass

        return signals

    def random_exploration_drive(self, cycle_count: int) -> list:
        """Curiosity: occasionally explore random genes for improvement."""
        signals = []
        if not self.genome:
            return signals

        # Base probability ~15%, boosted by pressure
        prob = 0.15 * self._pressure_boost
        if random.random() < prob:
            genes = list(self.genome.genes.keys())
            if genes:
                target = random.choice(genes)
                if target not in ("mutator", "genome", "inspector", "solidify"):
                    signals.append(Signal(
                        type="opportunity",
                        source="curiosity",
                        message=f"Curiosity drive: explore improvements for gene '{target}'",
                        severity=0.25 * self._pressure_boost,
                        context={"gene": target, "drive": "curiosity"},
                    ))

        return signals

    def stagnation_detection(self, cycle_count: int) -> list:
        """Generate high-severity signals when no mutations happen for too long."""
        signals = []

        if self.cycles_without_mutations >= 3:
            # Boost exploration pressure
            self._pressure_boost = min(5.0, self._pressure_boost * 1.3)

            signals.append(Signal(
                type="stagnation",
                source="autonomous",
                message=f"Stagnation: {self.cycles_without_mutations} cycles without mutations. "
                        f"Increasing exploration pressure to {self._pressure_boost:.1f}x",
                severity=min(0.9, 0.4 + self.cycles_without_mutations * 0.1),
                context={
                    "cycles_without_mutations": self.cycles_without_mutations,
                    "pressure_boost": self._pressure_boost,
                },
            ))

        if self.cycles_without_mutations >= 10:
            self.cycles_without_mutations = 0
            self._pressure_boost = 3.0
            signals.append(Signal(
                type="stagnation",
                source="autonomous_critical",
                message=f"CRITICAL stagnation: evolution deadlocked. Force-resetting pressure.",
                severity=0.95,
                context={"action": "force_reset"},
            ))

        return signals

    def code_quality_audit(self) -> list:
        """Periodic code quality check — scan for weak patterns."""
        signals = []
        now = time.time()
        if now - self._last_quality_audit < 300:  # Every 5 minutes
            return signals
        self._last_quality_audit = now

        if not self.genome:
            return signals

        for name, gene in self.genome.genes.items():
            if name in ("mutator", "genome", "inspector", "solidify"):
                continue
            try:
                source = gene.source_path.read_text(encoding="utf-8")
                lines = source.split("\n")

                # Check for missing error handling
                if "try:" not in source and "def " in source:
                    signals.append(Signal(
                        type="quality",
                        source="code_audit",
                        message=f"Gene '{name}' has no error handling",
                        severity=0.2,
                        context={"gene": name, "issue": "no_error_handling"},
                    ))

                # Check for long functions (>50 lines between def and return)
                if len(lines) > 50:
                    signals.append(Signal(
                        type="quality",
                        source="code_audit",
                        message=f"Gene '{name}' is large ({len(lines)} lines) — consider refactoring",
                        severity=0.15,
                        context={"gene": name, "issue": "large_file", "lines": len(lines)},
                    ))

                # Check for low fitness genes — enrich with memory
                if gene.fitness < 0.2 and gene.mutation_count > 0:
                    # MEMORY LOOP: check why this gene keeps failing
                    memory_hint = ""
                    if self.memory:
                        try:
                            past = self.memory.search_fts(
                                f"error {name}", limit=3
                            )
                            if past:
                                snippets = [m.content[:60] for m in past[:2]]
                                memory_hint = f" | Past: {'; '.join(snippets)}"
                        except Exception:
                            pass

                    signals.append(Signal(
                        type="quality",
                        source="code_audit",
                        message=f"Gene '{name}' has low fitness ({gene.fitness:.2f}) despite {gene.mutation_count} mutations{memory_hint}",
                        severity=0.35,
                        context={"gene": name, "issue": "low_fitness", "fitness": gene.fitness},
                    ))

            except Exception:
                pass

        return signals

    def domain_context_analysis(self, domain: str = "general") -> list:
        """Drive 5: Domain-context-aware analysis.

        Reads the current domain specialization and generates evolution signals
        tailored to that domain. In trading mode, this would analyze markets.
        In coding mode, it scans for new libraries and patterns.
        In research mode, it looks for papers and data.
        This is GENERIC — it adapts to whatever domain MUE is specialized in.
        """
        signals = []
        now = time.time()
        if now - self._last_market_check < 180:  # Every 3 minutes
            return signals
        self._last_market_check = now

        # Domain-adaptive signals based on current specialization
        domain_signals = {
            "trading": [
                ("Deepen market analysis capabilities", "evolve_indicators"),
                ("Scan for profitable strategy patterns", "evolve_strategies"),
                ("Optimize risk management algorithms", "evolve_risk"),
            ],
            "coding": [
                ("Scan for new frameworks and libraries", "evolve_tools"),
                ("Improve code generation patterns", "evolve_generation"),
                ("Optimize algorithm implementations", "evolve_algorithms"),
            ],
            "research": [
                ("Search for recent papers and breakthroughs", "evolve_research"),
                ("Improve data analysis methodologies", "evolve_analysis"),
                ("Enhance hypothesis generation", "evolve_hypothesis"),
            ],
            "creative": [
                ("Explore new creative techniques", "evolve_creativity"),
                ("Improve content generation quality", "evolve_content"),
                ("Experiment with novel formats", "evolve_formats"),
            ],
            "general": [
                ("Scan environment for improvement opportunities", "evolve_general"),
                ("Identify weak subsystems to strengthen", "evolve_subsystems"),
                ("Explore cross-domain knowledge transfer", "evolve_cross_domain"),
            ],
        }

        options = domain_signals.get(domain, domain_signals["general"])
        msg, action = random.choice(options)

        # MEMORY LOOP: enrich with domain-relevant memories
        memory_context = {}
        if self.memory:
            try:
                domain_memories = self.memory.search_fts(
                    f"{domain} strategy pattern", limit=3
                )
                if domain_memories:
                    top = domain_memories[0]
                    msg += f" (memory: {top.content[:80]})"
                    memory_context["relevant_memory"] = top.content[:200]
            except Exception:
                pass

        signals.append(Signal(
            type="opportunity",
            source="domain_context",
            message=f"[{domain}] {msg}",
            severity=0.45,
            context={"domain": domain, "action": action, **memory_context},
        ))

        return signals

    def creative_synthesis_drive(self, cycle_count: int) -> list:
        """Drive 6: Creative synthesis — cross-domain idea generation.

        Combines concepts from unrelated genes, atouts, and memories to
        generate genuinely novel mutation ideas. This is where MUE's
        creativity lives — not just fixing problems, but inventing new
        capabilities by cross-pollinating ideas.
        """
        signals = []
        if not self.genome or len(self.genome.genes) < 2:
            return signals

        # Every 2-4 cycles, attempt creative synthesis
        if cycle_count % random.randint(2, 4) != 0:
            return signals

        gene_names = list(self.genome.genes.keys())
        if len(gene_names) < 2:
            return signals

        # Pick two random genes and synthesize a new capability
        a, b = random.sample(gene_names, 2)

        synthesis_templates = [
            f"Creative synthesis: combine '{a}' with '{b}' — what new capability emerges?",
            f"Innovation drive: merge patterns from '{a}' and '{b}' into a hybrid gene",
            f"Cross-pollination: apply '{a}' techniques to '{b}' — novel approach?",
            f"Lateral thinking: solve '{a}' limitations using '{b}' patterns",
            f"What if '{a}' and '{b}' shared a common interface? Design it.",
            f"Creative destruction: replace '{a}' with a '{b}'-inspired rewrite",
        ]
        message = random.choice(synthesis_templates)

        signals.append(Signal(
            type="innovation",
            source="creative_synthesis",
            message=message,
            severity=random.uniform(0.3, 0.7),
            context={
                "gene_a": a,
                "gene_b": b,
                "cycle": cycle_count,
                "mutation_type": "synthesis",
            },
        ))

        return signals

    def proactive_initiative_drive(self, cycle_count: int) -> list:
        """Drive 7: Proactive initiative — what COULD MUE become?

        Unlike self-analysis (reactive: what's broken?), this drive ASKS:
        - What capability is MUE missing entirely?
        - What would make MUE 10x more valuable?
        - What have we NEVER tried that could be revolutionary?

        This is the ambition engine. It generates signals for entirely new
        genes, subsystems, or capabilities that don't exist yet.
        """
        signals = []
        if not self.genome:
            return signals

        # Initiative fires every 3-5 cycles with increasing probability
        base_prob = 0.12 * self._pressure_boost
        if random.random() > base_prob:
            return signals

        initiative_templates = [
            # New capability ideas
            "Initiative: design a new gene for autonomous web research",
            "Initiative: create a predictive model that anticipates user needs",
            "Initiative: build a self-optimizing configuration system",
            "Initiative: develop a gene that generates unit tests for other genes",
            "Initiative: invent a new mutation strategy — \"metamorphosis\"",
            # Ambition signals
            "Ambition: what would make MUE 10x more powerful? Evolve toward that.",
            "Ambition: analyze top AI agents (AutoGPT, CrewAI, Aider) — what can we absorb?",
            "Ambition: design a plugin that makes MUE extensible by external developers",
            # Self-improvement meta
            "Meta: evolve the evolution loop itself — make mutations smarter",
            "Meta: improve the fitness function — reward novelty, not just stability",
            "Meta: design a gene that writes better genes",
            # Domain expansion
            "Expansion: what domain should MUE master next? Generate a new specializer config.",
            "Expansion: scan for emerging technologies MUE could absorb",
            "Expansion: identify the weakest subsystem and propose a redesign",
        ]

        message = random.choice(initiative_templates)
        severity = random.uniform(0.3, 0.65) * self._pressure_boost

        signals.append(Signal(
            type="initiative",
            source="proactive_initiative",
            message=message,
            severity=min(0.85, severity),
            context={
                "cycle": cycle_count,
                "pressure_boost": self._pressure_boost,
                "mutation_type": "initiative",
            },
        ))

        return signals

    @property
    def stats(self) -> dict:
        return {
            "total_signals_generated": self.total_signals_generated,
            "cycles_without_mutations": self.cycles_without_mutations,
            "pressure_boost": round(self._pressure_boost, 2),
        }
