"""MUE Core Agent — Pure MCP agent powered by Claude Code.

Claude Code IS the LLM. MUE is the body (filesystem, evolution, memory,
security, MCP tools). Claude Code provides the intelligence.

MUE has:
- Claude Code tools (bash, read/write/edit, glob, grep, web, MCP bridge)
- Self-modification pipeline (validate → test → apply → rollback)
- Self-reflection and continuous improvement
- Security monitoring and audit trail
- MCP plugin creation (generates its own tools)
- GitHub absorption (mines code patterns)
- 6-layer memory lattice (SQLite FTS5)
- Emotional engine with personality evolution
- AST-based mutations for headless autonomous mode

Usage:
    python mue.py              # MCP server mode (Claude Code connects)
    python mue.py --headless   # Autonomous evolution loop (AST-only)
"""

import time
from pathlib import Path

from .dna import Genome, Gene, Inspector
from .dna.sandbox import GeneSandbox
from .memory import MemoryLattice, EpisodicMemory, HybridRetriever
from .skills import Crystallizer, SkillTree
from .personality import EmotionalState, Persona
from .evolution import EvolutionLoop, SignalDetector
from .absorption import GitHubMiner
from .powers import PowerTools
from .llm_dna import LLMDNAMutator
from .security import SecurityGuard
from .self_reflection import SelfReflection
# Lazy: SelfModificationPipeline, PluginCreator only needed in MCP mode
from .autonomy import GoalDecomposer, RevenueEngine
from .autonomy.autonomous_signals import AutonomousSignalGenerator
from .autonomy.auto_correction import ErrorPatternDB, SelfBenchmark, RegressionDetector, AutoCorrector
from .specialization import DomainSpecializer
from .feedback import TaskScorer, TaskRecord
from .swarm import SwarmOrchestrator
from .bootstrap import Bootstrapper
from .tasks import TaskRunner, GeneTaskMapper, FitnessUpdater
from .meta import (SelfDiagnosis, CuriosityEngine, ResourceMonitor,
                   CycleAdapter, KnowledgeTransfer)


class MueAgent:
    """The complete self-evolving AI agent — powered by Claude Code."""

    def __init__(self, work_dir: str = ".", config: dict | None = None):
        self.work_dir = Path(work_dir).resolve()
        if config is not None:
            self.config = config
        else:
            self.config = self._load_config()
        self.agent_name = self.config.get("name", "Mue")

        # Create directories
        self.genes_dir = self.work_dir / "genes"
        self.skills_dir = self.work_dir / "skills"
        self.atouts_dir = self.work_dir / "atouts"
        self.plugins_dir = self.work_dir / "plugins"
        for d in [self.genes_dir, self.skills_dir, self.atouts_dir, self.plugins_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # ── SECURITY (activated FIRST — immune system) ──
        self.security = SecurityGuard(self.work_dir)

        # ── FOUNDATION LAYER ──
        self.genome = Genome(self.work_dir)
        self.memory = MemoryLattice(str(self.work_dir / "mue_memory.db"))
        self.episodic = EpisodicMemory(self.memory)
        self.retriever = HybridRetriever(self.memory)
        self.emotions = EmotionalState()
        self.persona = Persona(name=self.agent_name)
        self.skill_tree = SkillTree()

        # ── POWER TOOLS (Claude Code capabilities via MCP) ──
        self.tools = PowerTools(self.work_dir, self.security)

        # ── SKILLS & CRYSTALLIZATION ──
        self.crystallizer = Crystallizer(self.skills_dir, self.memory, self.genome, self.skill_tree)

        # ── DNA MUTATION (AST-based for headless mode; Claude Code for LLM) ──
        self.llm_dna = LLMDNAMutator(self.genome, self.tools, self.memory, self.persona)

        # ── SELF-REFLECTION ──
        self.reflection = SelfReflection(
            self.persona, self.emotions, self.genome, self.llm_dna, self.tools
        )

        # ── AUTONOMOUS SIGNAL GENERATOR (the breathing heart) ──
        self.autonomous_signals = AutonomousSignalGenerator(
            inspector=None,  # Set after inspector is created below
            genome=self.genome,
            persona=self.persona,
            emotions=self.emotions,
            memory=self.memory,   # Memory loop: signals now query past outcomes
            work_dir=self.work_dir,
        )

        # ── AUTO-CORRECTION (immune system — learns from every error) ──
        self.error_db = ErrorPatternDB(self.work_dir / "mue_errors.db")
        self.benchmark = SelfBenchmark(self.error_db, self.genes_dir)
        self.regression_detector = RegressionDetector(self.error_db)
        self.auto_corrector = AutoCorrector(self.error_db, self.benchmark, self.regression_detector)

        # ── ABSORPTION (GitHub mining — created BEFORE evolution loop) ──
        self.miner = GitHubMiner(self.atouts_dir, self.memory, self.genome, project_root=self.work_dir.parent)

        # ── DOMAIN SPECIALIZATION (created BEFORE evolution loop) ──
        self.specializer = DomainSpecializer(self.work_dir / "mue_config.json")

        # ── FEEDBACK LOOP (task scoring → gene reinforcement) ──
        self.scorer = TaskScorer(self.work_dir / "mue_feedback.db")

        # ── AUTONOMY ──
        self.goals = GoalDecomposer(self.persona, self.memory, genome=self.genome)
        self.revenue = RevenueEngine(self.crystallizer, self.persona, self.emotions)

        # ── SWARM ORCHESTRATOR (multi-agent collaboration) ──
        self.swarm = SwarmOrchestrator(agent_name=self.agent_name)
        # Wire resources so agent handlers can access memory, genome, and miner
        self.swarm.memory = self.memory
        self.swarm.genome = self.genome
        self.swarm.miner = self.miner

        # ── LAYER 2: TASK EXECUTION & REAL FEEDBACK ──
        self.task_runner = TaskRunner()
        self.gene_mapper = GeneTaskMapper(self.genome)
        self.fitness_updater = FitnessUpdater(self.genome, self.gene_mapper)

        # ── LAYER 3: META-COGNITION ──
        self.diagnosis = SelfDiagnosis(
            genome=self.genome,
            rl_optimizer=None,  # RL lives inside EvolutionLoop, wired later
            scorer=self.scorer,
            error_db=self.error_db,
        )
        self.curiosity = CuriosityEngine(
            genome=self.genome,
            memory=self.memory,
            tasks_dir=self.work_dir / "tasks",
        )
        self.resources = ResourceMonitor(work_dir=self.work_dir)
        self.cycle_adapter = CycleAdapter(resource_monitor=self.resources)
        self.sandbox = GeneSandbox(timeout=5.0)
        self.knowledge_transfer = KnowledgeTransfer(
            genome=self.genome,
            memory=self.memory,
        )

        # ── EVOLUTION LOOP (AST-based mutations for headless mode) ──
        strategy = self.config.get("evolution_strategy", "balanced")
        self.evolution = EvolutionLoop(
            self.genome, self.memory, self.crystallizer,
            self.emotions, self.persona, strategy=strategy,
            autonomous_signals=self.autonomous_signals,
            genes_dir=self.genes_dir,
            auto_corrector=self.auto_corrector,
            miner=self.miner,
            specializer=self.specializer,
            scorer=self.scorer,
            swarm=self.swarm,
            goals=self.goals,
            task_runner=self.task_runner,
            gene_mapper=self.gene_mapper,
            fitness_updater=self.fitness_updater,
            diagnosis=self.diagnosis,
            curiosity=self.curiosity,
            resource_monitor=self.resources,
            cycle_adapter=self.cycle_adapter,
            knowledge_transfer=self.knowledge_transfer,
            llm_mutator=self.llm_dna,
            sandbox=self.sandbox,
        )
        self.detector = self.evolution.detector  # Shared reference

        # ── MCP PLUGIN CREATOR (lazy import: only needed in MCP mode) ──
        from .mcp.plugin_creator import PluginCreator
        self.plugin_creator = PluginCreator(self.plugins_dir, self.tools)

        # ── SELF-MODIFICATION PIPELINE (lazy import: only needed in MCP mode) ──
        from .self_modification import SelfModificationPipeline
        self.self_mod = SelfModificationPipeline(self.genes_dir, self.security)

        # ── INSPECTOR ──
        self.inspector = Inspector(self.genome)
        # Wire inspector into autonomous signals (was None before)
        self.autonomous_signals.inspector = self.inspector

        # Set birth time
        self.persona.birth_time = time.time()

        # Scan existing genes
        self._load_existing()

        # Auto-bootstrap on first launch (0 genes → create seed DNA + memories)
        bootstrap = Bootstrapper(self)
        bootstrap_result = bootstrap.run()
        if bootstrap_result["genes_created"] > 0:
            for line in bootstrap_result.get("output", []):
                print(line)

        # ── KERNEL INTEGRITY: seal hashes of protected core genes ──
        if not bootstrap_result.get("genes_created"):  # Don't seal if just bootstrapped
            intact, violations = self.genome.verify_kernel()
            if not intact:
                print(f"[KERNEL] WARNING: {len(violations)} kernel violation(s) detected!")
                for v in violations:
                    print(f"  - {v}")
        self.genome.seal_kernel()

    def _load_config(self) -> dict:
        """Load config from mue_config.json if it exists."""
        config_path = self.work_dir / "mue_config.json"
        if config_path.exists():
            try:
                import json
                return json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _load_existing(self):
        """Load existing genes without re-writing files (non-destructive)."""
        for py_file in self.genes_dir.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            gene = Gene.from_file(py_file)
            self.genome.genes[gene.name] = gene

    def chat(self, message: str) -> dict:
        """Process a chat message. Triggers evolution and self-reflection."""
        self.emotions.update()

        is_task = any(message.lower().startswith(kw)
                      for kw in ("do ", "make ", "create ", "build ", "fetch ",
                                 "get ", "find ", "search ", "code ", "write ",
                                 "evolve ", "mine ", "absorb ", "learn ", "improve ",
                                 "analyze ", "scan ", "deploy ", "test "))

        if message.lower().startswith("mine "):
            return self._handle_mine_command(message)
        if message.lower().startswith("reflect"):
            return self._handle_reflect_command()

        if is_task:
            self.detector.ingest_outcome(success=True, task=message[:100], duration=0.1, source="chat")
            self.reflection.on_action(success=True, tool="chat", description=message[:100])

        evolved = False
        if self.reflection.should_reflect():
            self.reflection.reflect("periodic")

        if self.detector.get_evolution_pressure() > 0.3:
            tick_result = self.evolution.tick()
            if tick_result.get("mutations_applied", 0) > 0:
                evolved = True

        response_text = self._generate_response(message, is_task)
        self.emotions.on_social_interaction(0.5 if is_task else 0.3)

        # Record task for feedback scoring
        genes_involved = list(self.genome.genes.keys())[:10]  # Relevant genes
        if is_task:
            self.scorer.record_task(TaskRecord(
                task_id=f"chat_{int(time.time())}",
                description=message[:200],
                genes_involved=genes_involved,
                success=True,
                duration_ms=100,
                complexity=0.5,
                impact_score=0.7,
            ))

        return {"text": response_text, "evolved": evolved}

    def _handle_mine_command(self, message: str) -> dict:
        query = message.replace("mine ", "", 1).strip() or None
        absorbed = self.miner.mine(query)
        if absorbed:
            return {
                "text": f"*Absorbed* {len(absorbed)} atouts: "
                        f"{', '.join(p.source_repo for p in absorbed)}. "
                        f"Total atouts: {self.miner.stats['total_atouts']}.",
                "evolved": True,
            }
        return {"text": "*Scan complete* No new atouts found.", "evolved": False}

    def _handle_reflect_command(self) -> dict:
        r = self.reflection.reflect("on_demand")
        if r:
            return {
                "text": f"*Self-reflection* Rating: {r.self_rating:.0%}. "
                        f"Insights: {'; '.join(r.insights[:2])}. "
                        f"Resolutions: {'; '.join(r.resolutions[:2])}.",
                "evolved": False,
            }
        return {"text": "*Quiet contemplation*", "evolved": False}

    def _generate_response(self, message: str, is_task: bool) -> str:
        v = self.emotions.vector
        if is_task:
            return (
                f"[{v.mood_label}] Processing. "
                f"I have {self.genome.stats['gene_count']} genes, "
                f"{self.crystallizer.stats['total_skills']} skills, "
                f"{self.miner.stats['total_atouts']} atouts."
            )
        return (
            f"[{v.mood_label}] I'm {self.persona.age_stage} with "
            f"{self.genome.stats['gene_count']} genes, "
            f"{self.plugin_creator.stats['total_plugins']} plugins, "
            f"{self.memory.stats['total_memories']} memories."
        )

    VERSION = "0.6.0"

    def start(self):
        """Start the agent in headless mode — autonomous evolution loop."""
        auto_stats = self.autonomous_signals.stats
        kernel_intact, _ = self.genome.verify_kernel()
        meta_health = self.diagnosis.overall_health
        print(f"""
  MUE v{self.VERSION} — Autonomous Self-Evolving Agent
  Name: {self.persona.name} | Stage: {self.persona.age_stage}
  Mood: {self.emotions.vector.mood_label}
  Genes: {self.genome.stats['gene_count']} | Mutations: {self.genome.stats['total_mutations']}
  Memories: {self.memory.stats['total_memories']} | Atouts: {self.miner.stats['total_atouts']}
  Plugins: {self.plugin_creator.stats['total_plugins']}
  Kernel: {'SEALED' if kernel_intact else 'UNSEALED — kernel tampering detected!'}
  Health: {meta_health.upper()} | Throttle: L{self.resources.throttle_level}
  Tasks: {self.fitness_updater.stats['total_updates']} scored | Pending gaps: {self.curiosity.stats['active_gaps']}
  Strategy: {self.cycle_adapter.config.strategy} | Interval: {self.cycle_adapter.config.interval_seconds}s
  Autonomous Drives: ACTIVE ({auto_stats['total_signals_generated']} signals generated)
  Security: ACTIVE | Audit log: mue_audit.jsonl
  Evolution Strategy: {self.config.get('evolution_strategy', 'balanced')}
  """)
        self.evolution.start(interval_seconds=self.cycle_adapter.config.interval_seconds)

    @property
    def state(self) -> dict:
        """Complete agent state snapshot."""
        return {
            "persona": self.persona.snapshot(),
            "emotions": self.emotions.snapshot(),
            "genome": self.genome.stats,
            "memory": self.memory.stats,
            "skills": self.crystallizer.stats,
            "atouts": self.miner.stats,
            "evolution": self.evolution.stats if self.evolution else {},
            "llm_mutations": self.llm_dna.stats,
            "reflection": self.reflection.stats,
            "security": self.security.stats,
            "plugins": self.plugin_creator.stats,
            "revenue": self.revenue.stats,
            "goals": self.goals.stats,
            "self_mod_pipeline": self.self_mod.stats,
            "specialization": self.specializer.stats,
            "feedback": self.scorer.stats,
            "swarm": self.swarm.stats,
            "tasks": {
                "runner": self.task_runner.stats,
                "mapper": self.gene_mapper.stats,
                "fitness": self.fitness_updater.stats,
            },
            "meta": {
                "diagnosis": self.diagnosis.stats,
                "curiosity": self.curiosity.stats,
                "resources": self.resources.stats,
                "cycle_adapter": self.cycle_adapter.stats,
                "knowledge_transfer": self.knowledge_transfer.stats,
            },
        }
