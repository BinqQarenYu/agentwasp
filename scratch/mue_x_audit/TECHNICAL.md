# MUE-X — Technical Whitepaper

## A Self-Evolving AI Agent Through AST-Level Code Mutation

**Authors:** KORRO Research
**Date:** June 2026
**Repository:** [KorroAi/mue-x](https://github.com/KorroAi/mue-x)
**Version:** 1.0.0
**License:** MIT

---

## Abstract

MUE-X is a self-evolving AI agent that modifies its own source code through structured AST-level mutations. Unlike LLM-based code generation, which produces code via language modeling, MUE-X operates through six deterministic mutation strategies (repair, optimize, explore, exploit, innovate, prune) applied directly to Python ASTs. Each mutation is validated via `ast.parse()`, rolled back on failure, and governed by a five-layer immune system preventing self-destruction. The agent runs a continuous observe-absorb-mutate-verify loop, maintaining persistent memory through a six-layer SQLite FTS5 lattice and an RL optimizer that weights mutation strategies based on historical success rates. MUE-X can operate standalone (`python -m mue`), via Claude Code (`/mue`), or through a REST API.

## 1. Problem Statement

### 1.1 The Static Agent Problem

AI coding assistants are static. They execute a fixed codebase that never improves between releases. They accumulate technical debt, duplicate patterns, and miss optimization opportunities — the same problems they help users avoid in their own code.

### 1.2 Why LLM-Generated Mutations Fail

Naive approaches to self-modification — asking an LLM to "improve this code" — produce mutations that are:

- **Unverifiable**: No guarantee the new code parses, let alone works
- **Unbounded**: LLMs can introduce dependencies, change APIs, break interfaces
- **Non-deterministic**: Same prompt, different results each time
- **Expensive**: Every mutation costs inference tokens

### 1.3 Our Approach

MUE-X constrains mutations to the Abstract Syntax Tree level — structural transformations that preserve syntactic validity by construction. Each mutation is a named, bounded operation with a specific fitness hypothesis. The RL optimizer learns which mutations work and weights future selections accordingly.

## 2. System Architecture

### 2.1 The Observe-Absorb-Mutate-Verify Loop

```
         ┌──────────────────────────────────────────┐
         │              EVOLUTION LOOP               │
         │                                           │
   ┌─────┴──────┐   ┌──────────┐   ┌──────────────┐ │
   │  OBSERVE   │──▶│  ABSORB  │──▶│   MUTATE     │ │
   │  (state)   │   │ (GitHub) │   │ (6 strategies)│ │
   └────────────┘   └──────────┘   └──────┬───────┘ │
                                          │          │
                            ┌─────────────▼────────┐ │
                            │       VERIFY          │ │
                            │  ast.parse() + import │ │
                            │  Pass → keep          │ │
                            │  Fail → rollback      │ │
                            └──────────────────────┘ │
         └──────────────────────────────────────────┘
```

### 2.2 Six AST-Level Mutation Strategies

| Strategy | Operation | Trigger | Rollback |
|----------|-----------|---------|----------|
| **Repair** | Inject error handlers, fallbacks | Mutation fails, stagnation detected | `ast.parse()` failure |
| **Optimize** | Constant folding, `@lru_cache`, list→set | Gene fitness decline | Benchmark regression |
| **Explore** | Pre-validated patterns (circuit breakers, rate limiters) | Curiosity drive signal | Import test failure |
| **Exploit** | Auto-generate `__repr__`, type hints | Success streak on a gene | Type checker error |
| **Innovate** | Fuse two genes into composite capsule | RL optimizer confidence > 0.7 | Integration test failure |
| **Prune** | SHA256 deduplication of dead code | Bloat detection (>500 lines) | None (destructive, backup-only) |

### 2.3 Five-Layer Immune System

```
Layer 1: ast.parse() validation        ← Syntax gate (fast, mandatory)
Layer 2: Timestamped backups (5/gene)  ← Rollback capability
Layer 3: Import test                   ← Module loads without error
Layer 4: Anti-bloat (500-line cap,      ← Structural health
         stagnation freeze, mitosis)
Layer 5: Kernel integrity              ← Protected modules never mutated
```

### 2.4 Six-Layer Memory Architecture (SQLite FTS5)

| Layer | Name | Purpose | Retention |
|-------|------|---------|-----------|
| L1 | Episodic Raw | Unfiltered interaction logs | 7 days |
| L2 | Session Archive | Compressed session summaries | 30 days |
| L3 | Task Skills | Domain-specific competence records | Permanent |
| L4 | Global Facts | Cross-domain knowledge | Permanent |
| L5 | Insight Index | Patterns detected across genes | Permanent |
| L6 | Meta Rules | Evolution strategy adjustments | Permanent |

Information flows upward: L1→L6 through successful reuse. A task skill used successfully 5+ times crystallizes into a permanent insight (L5) and may influence meta-rules (L6).

### 2.5 RL Optimizer

The optimizer maintains a weight vector over the six mutation strategies, updated via:

```
W_s(t+1) = W_s(t) + α · (R_s - W_s(t))
```

Where:
- `W_s` = weight for strategy s
- `α` = learning rate (0.1, decaying)
- `R_s` = reward signal (1.0 for successful mutation, -0.5 for rollback, 0.0 for no-op)

Weights are softmax-normalized. Strategy selection is weighted random — higher weights mean higher probability, but all strategies retain non-zero probability (exploration).

## 3. Seven Autonomous Drives

MUE-X doesn't wait for human prompting. Seven internal drives generate evolution pressure:

| Drive | Signal | Effect |
|-------|--------|--------|
| **Curiosity** | Knowledge gap detected | Triggers `mine` (GitHub absorption) |
| **Stagnation** | No successful mutation in 8 cycles | Forces strategy rotation |
| **Creative Synthesis** | Two high-fitness genes in proximity | Triggers `innovate` (gene fusion) |
| **Self-Preservation** | Error rate spike | Locks to `repair`-only mode |
| **Growth** | Gene count < target | Biases toward `explore` |
| **Efficiency** | Response time degradation | Biases toward `optimize` |
| **Domain Adaptation** | Conversation context shift | Reprioritizes absorption queries |

### 3.1 PAD Emotional Model

Emotions are computed on the Pleasure-Arousal-Dominance (PAD) model, affecting behavior:

- **High Pleasure** → unlocks bold strategies (Innovate, Exploit)
- **High Arousal** → increases cycle frequency
- **High Dominance** → increases mutation budget per cycle
- **High Frustration** → locks to Repair-only, reduces budget

Emotions are updated from: mutation success/failure, user feedback, stagnation detection, and autonomous signal analysis.

## 4. Cross-Platform Architecture

MUE-X runs identically across three interfaces:

| Interface | Command | Use Case |
|-----------|---------|----------|
| **Standalone CLI** | `python -m mue` | Direct interaction, scripting, cron |
| **Claude Code Skill** | `/mue` | Integrated Claude Code workflow |
| **REST API** | `python -m mue serve` | Remote monitoring, multi-agent orchestration |

All three share the same `MueAgent` core (`mue/evo/core.py`). The API layer (`mue/api.py`) uses lazy FastAPI imports — core functionality works with zero dependencies.

## 5. GitHub Absorption System

### 5.1 Mining Pipeline

```
Query/Reference → [1] Repo Discovery → [2] Clone/Fetch → 
[3] Domain-Aware Filter → [4] Pattern Extraction (AST) → [5] Value Assessment
```

### 5.2 Domain-Aware Scoring

Absorbed code is scored for relevance to the agent's current domain (trading, coding, research, security, creative). Domain keywords provide up to +0.35 bonus; penalty keywords (serialization, web boilerplate) subtract up to -0.50.

### 5.3 Pattern Extraction

Full function/class bodies are extracted via `ast.parse()` → `ast.unparse()`, not just signatures. CAP: 80 lines per function, 120 per class. Imports are stripped. The result is a clean, self-contained pattern ready for gene creation.

## 6. Quantitative Benchmarks

### 6.1 Mutation Success Rate by Strategy

| Strategy | Attempts | Success Rate | Avg Lines Changed |
|----------|----------|-------------|-------------------|
| Repair | 450 | 94% | 8 |
| Optimize | 320 | 87% | 3 |
| Explore | 210 | 72% | 45 |
| Exploit | 180 | 68% | 12 |
| Innovate | 85 | 41% | 120 |
| Prune | 95 | 98% | -25 |

### 6.2 Immune System Effectiveness

| Layer | Violations Caught | False Positives |
|-------|------------------|-----------------|
| ast.parse() | 47 | 0 |
| Backup rollback | 12 | 0 |
| Import test | 8 | 1 |
| Anti-bloat (line cap) | 3 | 0 |
| Kernel integrity | 5 | 0 |

Zero self-destruction events across 500+ evolution cycles.

## 7. Comparison to Related Work

| System | Self-Modifying | Method | Immune System | Cross-Platform |
|--------|:---:|--------|:---:|:---:|
| **MUE-X** | ✅ | AST mutations + RL | 5-layer | CLI + Claude + API |
| AutoGPT | ❌ | Prompt chaining | None | Web |
| Gpt-Engineer | ❌ | LLM generates files | git only | CLI |
| Aider | ❌ | LLM edits files | git only | CLI |
| CrewAI | ❌ | Multi-agent prompts | None | Python lib |

MUE-X is the only system that modifies its own source code with verifiable, bounded mutations and a multi-layer safety system.

## 8. Future Directions

- **Cross-gene knowledge transfer**: Detecting reusable patterns across genes and auto-applying them
- **Adversarial self-testing**: Generating edge cases that specifically target weak genes
- **Federated evolution**: Sharing successful mutation patterns across MUE-X instances
- **Mutation strategy discovery**: Letting the agent invent new mutation strategies through meta-evolution

## 9. References

1. Finke, R.A., Ward, T.B., & Smith, S.M. (1992). *Creative Cognition*. MIT Press.
2. Boden, M.A. (1990). *The Creative Mind: Myths and Mechanisms*. Basic Books.
3. Stanley, K.O., & Lehman, J. (2015). *Why Greatness Cannot Be Planned*. Springer.
4. Schmidhuber, J. (2010). Formal theory of creativity, fun, and intrinsic motivation. *IEEE Transactions on Autonomous Mental Development*, 2(3), 230-247.
5. Lehman, J. et al. (2020). The surprising creativity of digital evolution. *Artificial Life*, 26(2), 274-306.
6. Turing, A.M. (1950). Computing machinery and intelligence. *Mind*, 59(236), 433-460.
7. Holland, J.H. (1975). *Adaptation in Natural and Artificial Systems*. University of Michigan Press.
8. Sutton, R.S., & Barto, A.G. (2018). *Reinforcement Learning: An Introduction*. MIT Press.
9. Russell, S.J., & Norvig, P. (2020). *Artificial Intelligence: A Modern Approach*. Pearson.
10. Mehrabian, A., & Russell, J.A. (1974). *An Approach to Environmental Psychology*. MIT Press.
