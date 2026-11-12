"""Evolution Loop — The main cycle that drives continuous agent improvement.

Signal → Select → Mutate → Backup → Validate → Solidify → Commit → Repeat

v0.7 changes:
- Mitosis: when genes exceed 500 lines, they split into new genes
- Backup: every mutation backed up before application
- Pruning: bloated genes are pruned of duplicate code
- Fitness tracking: gene fitness updated after each mutation
- Dashboard every 5 cycles with comprehensive stats
"""

import json
import random
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from ..dna import Mutator
from ..personality import EmotionalState, Persona
from .signals import SignalDetector
from .solidify import Solidifier
from .rl_optimizer import RLOptimizer


class EvolutionLoop:
    """Continuous improvement engine for MUE."""

    def __init__(self, genome, memory_lattice, crystallizer,
                 emotions: EmotionalState, persona: Persona,
                 strategy: str = "balanced", autonomous_signals=None,
                 genes_dir: Path = None, auto_corrector=None,
                 miner=None, specializer=None, scorer=None, swarm=None,
                 goals=None,
                 task_runner=None, gene_mapper=None, fitness_updater=None,
                 diagnosis=None, curiosity=None, resource_monitor=None,
                 cycle_adapter=None, knowledge_transfer=None,
                 llm_mutator=None, sandbox=None):
        self.genome = genome
        self.memory = memory_lattice
        self.crystallizer = crystallizer
        self.emotions = emotions
        self.persona = persona
        self.strategy = strategy
        self.autonomous = autonomous_signals
        self.auto_corrector = auto_corrector
        self.miner = miner              # Auto-gluttony
        self.specializer = specializer   # Domain adaptation
        self.scorer = scorer            # Feedback loop
        self.rl = RLOptimizer(scorer)   # RL strategy selection
        self.swarm = swarm              # Multi-agent collaboration
        self._goals = goals             # Goal decomposer
        self.genes_dir = genes_dir or Path("genes")
        self.backup_dir = self.genes_dir / "_backup"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        # ── LAYER 2: Task execution & real feedback ──
        self.task_runner = task_runner
        self.gene_mapper = gene_mapper
        self.fitness_updater = fitness_updater

        # ── LAYER 3: Meta-cognition ──
        self.diagnosis = diagnosis
        self.curiosity = curiosity
        self.resources = resource_monitor
        self.cycle_adapter = cycle_adapter
        # Pass initial strategy to cycle adapter so it doesn't reset to "balanced"
        if self.cycle_adapter:
            self.cycle_adapter.base_strategy = self.strategy
            self.cycle_adapter.config.strategy = self.strategy
        self.knowledge_transfer = knowledge_transfer
        self.llm_mutator = llm_mutator  # LLM-guided mutations (chat mode only)

        self.detector = SignalDetector()
        self.mutator = Mutator(genome)
        self.solidifier = Solidifier(genome, sandbox=sandbox)

        self.total_evolutions = 0
        self.successful_evolutions = 0
        self.mitosis_count = 0
        self.prune_count = 0
        self.running = False
        self.cycle_count = 0

        # Restore state if saved from previous run
        self.load_state()

    def tick(self) -> dict:
        self.emotions.update()
        self.cycle_count += 1

        result = {
            "signals_detected": 0,
            "autonomous_signals": 0,
            "mutations_proposed": 0,
            "mutations_applied": 0,
            "mitosis_events": 0,
            "prune_events": 0,
            "total_mutations_applied": self.successful_evolutions,
            "genes_created": 0,
            "skills_crystallized": 0,
            "personality_changes": {},
            "cycle_time_ms": 0,
            "absorbed_patterns": 0,
            "absorbed_locally": 0,
        }

        start = time.perf_counter()

        # 0. Generate autonomous signals (the breathing heart)
        if self.autonomous:
            auto_signals = self.autonomous.tick(self.cycle_count, result)
            for sig in auto_signals:
                self.detector.signals.append(sig)
            result["autonomous_signals"] = len(auto_signals)

        # 0.5 Auto-correction immune system tick
        if self.auto_corrector:
            ac_result = self.auto_corrector.tick(self.genome, result)
            if ac_result.get("regression_detected"):
                result["regression_alert"] = True
                for suggestion in ac_result.get("suggestions", []):
                    self.detector.ingest_error(suggestion, source="auto_correction")

        # 0.6 AUTO-GLUTTONY: Absorb from GitHub (debounced: every 7 cycles)
        if self.miner and self.cycle_count % 7 == 0:
            try:
                # Determine domain for targeted mining
                domain = "trading"
                if self.specializer:
                    domain = getattr(self.specializer, 'domain', 'general')

                # Web absorption (GitHub)
                absorbed = self.miner.auto_mine_tick(self.cycle_count, domain=domain)
                result["absorbed_patterns"] = len(absorbed)

                # Local absorption every 3 cycles
                if self.cycle_count % 3 == 0:
                    local = self.miner.local_absorb(domain=domain)
                    result["absorbed_locally"] = len(local)
                    if local:
                        for pattern in local[:3]:
                            self.detector.ingest_outcome(
                                success=True,
                                task=f"Absorbed {pattern.pattern_type} from {pattern.source_repo}",
                                duration=0, source="absorption"
                            )
            except Exception as e:
                self.detector._last_silent_error = f"miner: {e}"

        # 0.7 AUTO-CRYSTALLIZE: Transform high-value atouts into skills
        if self.miner and self.crystallizer and self.miner.absorbed:
            try:
                recent = sorted(
                    self.miner.absorbed.values(),
                    key=lambda p: p.absorbed_at, reverse=True
                )[:10]  # Last 10 absorbed patterns
                for pattern in recent:
                    if pattern.value_assessment >= 0.4 and pattern.usage_count < 3:
                        # Auto-name: atout pattern_type from source
                        safe_name = re.sub(r'[^a-zA-Z0-9_]', '_',
                            f"{pattern.pattern_type}_{pattern.source_repo[:20]}"
                        ).strip('_').lower()
                        if safe_name and safe_name not in getattr(self.crystallizer, 'skills', {}):
                            try:
                                self.crystallizer.crystallize(
                                    task_description=pattern.description[:200],
                                    working_code=pattern.code,
                                    skill_name=f"auto_{safe_name}",
                                    tags=[pattern.pattern_type, "auto_crystallized"],
                                )
                                pattern.usage_count += 1
                                result["skills_crystallized"] += 1
                            except Exception as e:
                                self.detector._last_silent_error = f"crystallize: {e}"
            except Exception as e:
                self.detector._last_silent_error = f"crystallizer: {e}"

        # 0.8 SWARM TICK: Process delegated multi-agent tasks
        if self.swarm and self.cycle_count % 3 == 0:
            try:
                swarm_result = self.swarm.tick()
                if swarm_result.get("results_collected", 0) > 0:
                    result["swarm_tasks_completed"] = swarm_result["tasks_completed"]
                    insights = self.swarm.get_insights(limit=3)
                    for insight in insights:
                        self.detector.ingest_outcome(
                            success=True,
                            task=f"Swarm {insight['role']}: {insight['description'][:100]}",
                            duration=0, source="swarm"
                        )
            except Exception as e:
                self.detector._last_silent_error = f"swarm: {e}"

        # 1. Collect signals
        active = self.detector.get_active_signals(min_severity=0.2)
        result["signals_detected"] = len(active)

        if not active:
            result["cycle_time_ms"] = (time.perf_counter() - start) * 1000
            return result

        effective_strategy = self._modulate_strategy()

        # 2. Generate mutations (RL selects strategy per target gene)
        all_mutations = []
        for signal in active:
            # RL: select best strategy for this signal's context
            target_gene = self.rl.select_target_gene(self.genome)
            rl_strategy = self.rl.select_strategy(
                gene_name=target_gene,
                context={"signal_type": signal.type, "signal_source": signal.source},
            )
            # Use RL strategy for 60% of mutations, modulated for 40%
            use_strategy = rl_strategy if random.random() < 0.6 else effective_strategy
            mutations = self.mutator.propose_mutations(
                signal.message, strategy=use_strategy
            )
            all_mutations.extend(mutations)

        result["mutations_proposed"] = len(all_mutations)

        # ADAPTIVE RATE LIMIT: reduce mutations when gene count grows
        gene_count = len(self.genome.genes)
        max_mutations = 3 if gene_count <= 10 else (2 if gene_count <= 20 else 1)

        # 3. Apply mutations (adaptive max per cycle)
        for mutation in all_mutations[:max_mutations]:
            # BACKUP before mutation
            self._backup_gene(mutation.target_gene)

            # S5 FIX: Capture original source BEFORE apply for mitosis
            if mutation.triggers_mitosis and mutation.new_gene_name:
                gene_path = self.genes_dir / f"{mutation.target_gene}.py"
                if gene_path.exists():
                    original_full = gene_path.read_text(encoding="utf-8")
                    mutation._mitosis_original = original_full

            # Validate
            valid, reason = self.solidifier.validate(
                mutation.target_gene, mutation.new_source
            )
            if not valid:
                self.solidifier.rollback(mutation.target_gene)
                self.detector.ingest_error(
                    f"Validation failed for {mutation.target_gene}: {reason}",
                    source="evolution"
                )
                # Record in auto-corrector for learning
                if self.auto_corrector:
                    self.auto_corrector.on_error(
                        "validation_failed", reason, mutation.target_gene,
                        mutation.mutation_type, "evolution"
                    )
                # RL: record failed outcome
                self.rl.record_outcome(
                    strategy=mutation.mutation_type,
                    gene_name=mutation.target_gene,
                    success=False,
                    impact=0.0,
                )
                continue

            # Apply
            if self.mutator.apply(mutation):
                result["mutations_applied"] += 1
                self.total_evolutions += 1
                self.successful_evolutions += 1

                # Handle mitosis: create new gene from split
                if mutation.triggers_mitosis and mutation.new_gene_name:
                    self._handle_mitosis(mutation)
                    result["mitosis_events"] += 1
                    result["genes_created"] += 1
                    self.mitosis_count += 1

                # Handle prune
                if mutation.mutation_type == "prune":
                    result["prune_events"] += 1
                    self.prune_count += 1

                self.emotions.on_success(0.3)
                self.persona.evolve("breakthrough", 0.5)

                # Record mutation outcome in feedback scorer
                real_impact = self._calculate_impact(mutation)
                if self.scorer:
                    try:
                        from ..feedback.scorer import TaskRecord
                        self.scorer.record_task(TaskRecord(
                            task_id=f"evo_{self.cycle_count}_{mutation.target_gene}",
                            description=f"Mutation: {mutation.mutation_type} on {mutation.target_gene}",
                            genes_involved=[mutation.target_gene],
                            success=True,
                            duration_ms=10,
                            complexity=mutation.risk_level + 0.4,
                            impact_score=real_impact,
                        ))
                    except Exception as e:
                        self.detector._last_silent_error = f"scorer: {e}"

                # RL: record successful outcome
                self.rl.record_outcome(
                    strategy=mutation.mutation_type,
                    gene_name=mutation.target_gene,
                    success=True,
                    impact=real_impact,
                )

                # GOAL TRACKING: notify goal decomposer
                if hasattr(self, '_goals') and self._goals:
                    self._goals.on_gene_improved(mutation.target_gene, real_impact)

        # 4. Skill crystallization
        trending = self.crystallizer.discover_trending_skills(limit=3)
        if trending and self.emotions.vector.confidence > 0.6:
            for trend in trending:
                if trend.success_count >= 5 and trend.success_rate > 0.7:
                    self.memory.reinforce(3, f"skill:{trend.name}", True)
                    result["skills_crystallized"] += 1

        # 5. Personality evolution
        if result["mutations_applied"] > 0:
            result["personality_changes"] = self.persona.evolve("breakthrough", 0.3)
        elif result["signals_detected"] > 3 and result["mutations_applied"] == 0:
            self.emotions.on_failure(0.2)
            result["personality_changes"] = self.persona.evolve("major_failure", 0.2)

        # 5.5 LAYER 2: Real fitness update + gene death
        if self.fitness_updater and self.cycle_count % 3 == 0:
            try:
                self.fitness_updater.update_all()
                result["fitness_updated"] = True
                # Gene death: purge genes dead for 10+ cycles
                if self.cycle_count % 10 == 0:
                    purged = self.fitness_updater.purge_dead_genes()
                    if purged > 0:
                        result["genes_purged"] = purged
            except Exception as e:
                self.detector._last_silent_error = f"fitness_updater: {e}"

        # 5.6 LAYER 3: Meta-cognition ticks
        # Health diagnosis every 5 cycles
        if self.diagnosis and self.cycle_count % 5 == 0:
            try:
                reports = self.diagnosis.check_all(self.cycle_count, result)
                result["health_reports"] = len(reports)
                result["health_status"] = self.diagnosis.overall_health
            except Exception as e:
                self.detector._last_silent_error = f"diagnosis: {e}"

        # Curiosity: detect knowledge gaps every 10 cycles
        if self.curiosity and self.cycle_count % 10 == 0:
            try:
                new_gaps = self.curiosity.detect_gaps()
                result["new_knowledge_gaps"] = len(new_gaps)
            except Exception as e:
                self.detector._last_silent_error = f"curiosity: {e}"

        # Resource assessment every cycle
        if self.resources:
            try:
                self.resources.assess()
                result["throttle_level"] = self.resources.throttle_level
            except Exception as e:
                self.detector._last_silent_error = f"resources: {e}"

        # Knowledge transfer after successful mutations
        if (self.knowledge_transfer and result["mutations_applied"] > 0
                and self.cycle_count % 4 == 0):
            try:
                recent = [{
                    "gene": m.target_gene,
                    "fitness_delta": 0.2,
                } for m in [
                    self.mutator._last_mutations[i]
                    for i in range(min(3, len(
                        getattr(self.mutator, '_last_mutations', [])
                    )))
                ]] if hasattr(self.mutator, '_last_mutations') else []
                if recent:
                    transfers = self.knowledge_transfer.auto_transfer(recent)
                    result["knowledge_transfers"] = len(transfers)
            except Exception as e:
                self.detector._last_silent_error = f"knowledge_transfer: {e}"

        # 5.7 CYCLE ADAPTATION: adjust timing and strategy
        if self.cycle_adapter:
            try:
                new_config = self.cycle_adapter.adapt(result)
                self.strategy = new_config.strategy
                result["adapted_interval"] = new_config.interval_seconds
                result["adapted_strategy"] = new_config.strategy
                result["mutation_budget"] = new_config.max_mutations_per_cycle
            except Exception as e:
                self.detector._last_silent_error = f"cycle_adapter: {e}"

        # 6. Cleanup
        self.detector.clear_old()

        # 7. Commit
        if result["mutations_applied"] > 0:
            self.solidifier.commit(
                f"[{effective_strategy}] {result['mutations_applied']} mutations, "
                f"pressure={self.detector.get_evolution_pressure():.2f}"
            )

        # Persist state every 5 cycles
        if self.cycle_count % 5 == 0:
            self.save_state()

        # Periodic maintenance: vacuum DB + clean backups every 50 cycles
        if self.cycle_count % 50 == 0:
            self._maintenance()

        result["cycle_time_ms"] = (time.perf_counter() - start) * 1000
        return result

    def _maintenance(self):
        """Periodic cleanup: vacuum databases, rotate backups."""
        # Vacuum memory DB
        if self.memory and hasattr(self.memory, 'conn'):
            try:
                self.memory.conn.execute("PRAGMA optimize")
            except Exception:
                pass
        # Clean old backups (keep only last 3 per gene)
        for gene_name in list(self.genome.genes.keys())[:50]:
            try:
                backups = sorted(
                    self.backup_dir.glob(f"{gene_name}_*.py"),
                    key=lambda p: p.stat().st_mtime
                )
                for old in backups[:-3]:
                    old.unlink(missing_ok=True)
            except Exception:
                pass

    def _backup_gene(self, gene_name: str):
        """Create a timestamped backup before mutation."""
        gene_path = self.genes_dir / f"{gene_name}.py"
        if not gene_path.exists():
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = self.backup_dir / f"{gene_name}_{ts}.py"
        shutil.copy2(gene_path, backup_path)

        # Rotate: keep max 5 backups per gene
        backups = sorted(
            self.backup_dir.glob(f"{gene_name}_*.py"),
            key=lambda p: p.stat().st_mtime
        )
        for old in backups[:-5]:
            old.unlink(missing_ok=True)

    def _handle_mitosis(self, mutation):
        """Create a new gene from a mitosis split."""
        gene_path = self.genes_dir / f"{mutation.target_gene}.py"
        if not gene_path.exists():
            return

        # S5 FIX: Use captured original source from BEFORE apply
        # After mutator.apply(), the gene file was truncated to first_half
        # so we use the pre-apply snapshot stored in _mitosis_original
        full_source = getattr(mutation, '_mitosis_original', None)
        if full_source is None:
            full_source = gene_path.read_text(encoding="utf-8")

        first_half = mutation.new_source
        # Find the split boundary in the original source
        split_idx = full_source.find(first_half)
        if split_idx < 0 or len(first_half) < 10:
            return
        second_half = full_source[split_idx + len(first_half):].lstrip("\n")

        if not second_half.strip():
            return  # Nothing to split

        new_name = mutation.new_gene_name
        # Ensure unique name
        counter = 1
        while (self.genes_dir / f"{new_name}.py").exists():
            new_name = f"{mutation.new_gene_name}_{counter}"
            counter += 1

        # Write new gene
        new_path = self.genes_dir / f"{new_name}.py"
        new_path.write_text(
            f'"""Gene: {new_name} — split from {mutation.target_gene} '
            f'via mitosis at cycle {self.cycle_count}."""\n\n{second_half}',
            encoding="utf-8"
        )

        # Register in genome
        self.genome.add_gene(new_name, new_path.read_text(encoding="utf-8"))

    def _modulate_strategy(self) -> str:
        v = self.emotions.vector
        if v.frustration > 0.6:
            return "repair-only"
        if v.risk_tolerance > 0.7:
            return "innovate"
        if v.confidence < 0.3:
            return "harden"
        return self.strategy

    def _calculate_impact(self, mutation) -> float:
        """Calculate a REAL qualitative impact score for a mutation.

        No more constant 0.8. Impact is derived from:
        - Line count change (code volume affected)
        - Mutation type (prune > optimize > explore > repair)
        - Risk level (higher risk = higher potential impact)
        - Prior gene performance from scorer (weaker genes = bigger potential gain)

        Returns a value 0.0-1.0 that meaningfully differentiates mutations.
        """
        impact = 0.05  # Base — any successful mutation has SOME impact

        # 1. LINE DELTA: how much did the code change?
        if mutation.old_hash and mutation.target_gene in self.genome.genes:
            try:
                gene_path = self.genes_dir / f"{mutation.target_gene}.py"
                if gene_path.exists():
                    new_lines = mutation.new_source.count("\n")
                    old_content = ""
                    if mutation.target_gene in self.genome.genes:
                        old_gene = self.genome.genes[mutation.target_gene]
                        if old_gene.source_path.exists():
                            old_content = old_gene.source_path.read_text(encoding="utf-8")
                    if old_content:
                        old_lines = old_content.count("\n")
                        line_delta = abs(new_lines - old_lines)
                        # Normalize: 50+ line change = 0.3 impact
                        impact += min(0.3, line_delta / 50.0 * 0.3)
            except Exception:
                impact += 0.05  # Fallback

        # 2. MUTATION TYPE WEIGHT: different strategies have different value
        type_weights = {
            "prune": 0.12,      # Removing dead code = good maintenance
            "optimize": 0.10,   # Performance improvement
            "exploit": 0.08,    # Reinforcing what works
            "innovate": 0.09,   # Novel combination
            "mitosis": 0.07,    # Structural reorganization
            "repair": 0.06,     # Bug fixes
            "explore": 0.05,    # Unproven experiment
        }
        impact += type_weights.get(mutation.mutation_type, 0.04)

        # 3. RISK BONUS: higher risk mutations that succeed deserve more credit
        impact += min(0.1, mutation.risk_level * 0.2)

        # 4. GENE PERFORMANCE CONTEXT: improving a weak gene is more impactful
        if self.scorer:
            try:
                gene_score = self.scorer.get_mutation_priority(mutation.target_gene)
                if gene_score < 0.4:
                    impact += 0.08  # Improving a weak gene
                elif gene_score > 0.7:
                    impact += 0.02  # Reinforcing an already strong gene
            except Exception:
                pass

        return min(1.0, impact)

    def _decay_gene_fitness(self, decay_rate: float = 0.005):
        """Slowly decay fitness of unused genes."""
        for name, gene in self.genome.genes.items():
            if name in ("mutator", "genome", "inspector", "solidify"):
                continue
            if gene.mutation_count == 0:
                gene.fitness = max(0.05, gene.fitness - decay_rate)

    def start(self, interval_seconds: float = 30.0):
        """Start the continuous evolution loop (blocking)."""
        self.running = True
        current_interval = interval_seconds

        while self.running:
            result = self.tick()

            # Use adapted interval from cycle_adapter if available
            if self.cycle_adapter:
                current_interval = self.cycle_adapter.get_adaptive_interval()

            if result["mutations_applied"] > 0:
                parts = [f"{result['mutations_applied']} mutations"]
                if result.get("mitosis_events", 0) > 0:
                    parts.append(f"{result['mitosis_events']} mitosis")
                if result.get("prune_events", 0) > 0:
                    parts.append(f"{result['prune_events']} pruned")
                health = f" | health={result.get('health_status', '?')}" if "health_status" in result else ""
                print(f"[EVO] {', '.join(parts)} | "
                      f"genes={self.genome.stats['gene_count']} | "
                      f"strategy={result.get('adapted_strategy', self.strategy)} | "
                      f"interval={current_interval:.0f}s"
                      f"{health} | "
                      f"mood={self.emotions.vector.mood_label}")

            if self.cycle_count % 5 == 0 and self.cycle_count > 0:
                gs = self.genome.stats
                miner_stats = f" | atouts={self.miner.stats['total_atouts']}" if self.miner else ""
                gaps = f" | gaps={self.curiosity.stats['active_gaps']}" if self.curiosity else ""
                fitness_stats = ""
                if self.fitness_updater:
                    fu = self.fitness_updater.stats
                    fitness_stats = f" | dead={fu.get('genes_dead', 0)} purged={fu.get('genes_purged', 0)}"
                print(f"[DASH] cycle={self.cycle_count} | genes={gs['gene_count']} | "
                      f"mutations={gs['total_mutations']} | "
                      f"fitness={gs['avg_fitness']:.2f} | "
                      f"mood={self.emotions.vector.mood_label} | "
                      f"pressure={self.detector.get_evolution_pressure():.2f} | "
                      f"throttle={self.resources.throttle_level if self.resources else 0} | "
                      f"mitosis={self.mitosis_count} | pruned={self.prune_count}"
                      f"{miner_stats}{gaps}{fitness_stats}")
                self._decay_gene_fitness()
                self.save_state()

            time.sleep(current_interval)

    def stop(self):
        self.save_state()
        self.running = False

    # ── STATE PERSISTENCE ──────────────────────────────────────────

    @property
    def _state_file(self) -> Path:
        return self.genes_dir.parent / "mue_state.json"

    def save_state(self):
        """Save evolution state to disk so it survives restarts."""
        state = {
            "cycle_count": self.cycle_count,
            "total_evolutions": self.total_evolutions,
            "successful_evolutions": self.successful_evolutions,
            "mitosis_count": self.mitosis_count,
            "prune_count": self.prune_count,
            "strategy": self.strategy,
            "last_saved": time.time(),
            "total_mutations": self.genome.stats["total_mutations"],
        }
        if self.cycle_adapter:
            state["adapted_interval"] = self.cycle_adapter.get_adaptive_interval()
            state["adapted_strategy"] = self.cycle_adapter.get_adaptive_strategy()
        if self.resources:
            state["throttle_level"] = self.resources.throttle_level

        try:
            self._state_file.write_text(
                json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            pass

    def load_state(self):
        """Restore evolution state from disk. Returns True if state was loaded."""
        sp = self._state_file
        if not sp.exists():
            return False

        try:
            state = json.loads(sp.read_text(encoding="utf-8"))
            self.cycle_count = state.get("cycle_count", 0)
            self.total_evolutions = state.get("total_evolutions", 0)
            self.successful_evolutions = state.get("successful_evolutions", 0)
            self.mitosis_count = state.get("mitosis_count", 0)
            self.prune_count = state.get("prune_count", 0)
            if "strategy" in state:
                self.strategy = state["strategy"]
            if "total_mutations" in state:
                self.genome._base_mutation_count = state["total_mutations"]
            return True
        except (json.JSONDecodeError, KeyError, OSError):
            # Corrupted state — rename to .bak and start fresh
            bak = sp.with_suffix(".json.bak")
            try:
                sp.rename(bak)
            except Exception:
                sp.unlink(missing_ok=True)
            return False

    @property
    def stats(self) -> dict:
        return {
            "total_evolutions": self.total_evolutions,
            "success_rate": self.successful_evolutions / max(self.total_evolutions, 1),
            "signals": self.detector.summary,
            "validation": self.solidifier.stats,
            "genome": self.genome.stats,
            "strategy": self.strategy,
            "mitosis_count": self.mitosis_count,
            "prune_count": self.prune_count,
        }
