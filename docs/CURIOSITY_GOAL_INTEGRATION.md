# Curiosity & Goal Teleological Integration Architecture

> **System Designation:** Agent WASP Autonomous Intelligence Core  
> **Core Concepts Elevated:** **Curiosity** (Epistemic Exploration & Information Gap Engine) and **Goal** (Teleological Alignment & Gravitational Attractor)  
> **Date:** March 2026 / Version 2.0  

---

## 1. Executive Summary & Philosophical Grounding

Traditional AI agents alternate between two defective extremes:
1. **The Static Reactive Tool:** Sits idle waiting for user prompts; fragile when encountering unmodeled edge cases.
2. **The Unconstrained Autonomous Agent:** Runs wild in infinite loops, burning tokens and compute on irrelevant tangents (the "curiosity trap").

Agent WASP solves this fundamental dilemma by elevating and coupling two complementary concepts into a **Teleological Dynamic Equilibrium**:
* **Curiosity ($\mathcal{C}_t$):** The *Engine of Exploration*. Driven by Information-Gap Theory (Loewenstein) and Active Inference (Friston). It detects entropy, anomalies, knowledge voids, and unexpected test outcomes.
* **Goal ($\vec{G}$):** The *Gravitational Orbit and Boundary*. Defined by the human operator's strategic intent (Software Architecture, Algorithmic Trading, Secure Operations, E-Commerce).

By enforcing **Vector Alignment ($\cos(\vec{v}_{\text{curiosity}}, \vec{G}) \ge \tau$)**, curiosity is prevented from devolving into noise. Instead, it continuously uncovers edge cases, tests hypotheses, and generates proactive micro-tasks that directly advance the operator's mission.

```
       [ OPERATOR MASTER GOAL: G ]
                  |
     (Gravitational Field / Vector)
                  |
                  v
       +----------------------+
       |   CURIOSITY ENGINE   | <--- Active Inference & Surprise (D_KL)
       |  Entropy Gap H(X)    | <--- Knowledge Graph Missing Links
       +----------------------+
                  |
         Spawns Candidate Tasks
                  |
                  v
       +----------------------+
       | TELEOLOGICAL FILTER  |  cos(v_curiosity, G) >= 0.75 ?
       +----------------------+
             /         \
          [PASS]     [PRUNE / ARCHIVE]
            |
            v
       +----------------------+
       | GOAL ORCHESTRATOR    | ---> Attached to Active Goal Graph
       | (Done-Checker & Gov) | ---> Controlled Skill Execution
       +----------------------+
            |
      Execution & Verified Outcome
            |
            v
       +----------------------+
       | MEMORY & SELF-MODEL  | ---> Updates Knowledge Graph, reduces H(X),
       |  (Learning Loop)     |      sharpens next curiosity cycle.
       +----------------------+
```

---

## 2. Mathematical Formalization

### 2.1 The Intrinsic Curiosity Index ($C_t$)
Curiosity is not a stochastic dice roll; it is a quantified value bounded between $0.0$ and $1.0$:

$$C_t = \underbrace{\mathcal{H}(X)}_{\text{Entropy / Ignorance}} \times \underbrace{\mathcal{D}_{\text{KL}}(P_{\text{prior}} \parallel P_{\text{evidence}})}_{\text{Bayesian Surprise}} \times \underbrace{S(X)}_{\text{Domain Salience}} \times \underbrace{(1 - \Lambda_t)}_{\text{System Load / Quota Governor}}$$

* **$\mathcal{H}(X) \in [0, 1]$:** The information deficit in the Knowledge Graph regarding entity or code component $X$.
* **$\mathcal{D}_{\text{KL}} \in [0, 1]$:** Epistemic surprise when an observed result deviates from predicted state (e.g. unexpected API failure or sudden orderbook imbalance).
* **$S(X) \in [0, 1]$:** Domain Salience weights assigned to operator priorities:
  * Algorithmic Trading & Finance: $1.0$
  * Codebase Architecture & Unit Integrity: $1.0$
  * Cybersecurity & Threat Sentinel: $1.0$
  * General Inquiries / Ambient Noise: $0.2$
* **$\Lambda_t \in [0, 1]$:** Dynamic governor penalty based on host RAM usage ($>80\%$), CPU thermal throttle, and sliding-window API quotas. If $\Lambda_t \to 1$, $C_t \to 0$.

### 2.2 Teleological Goal Alignment ($\mathcal{A}_t$)
Let $\vec{G}$ be the embedding vector of the active operator goal, and $\vec{v}_{\text{task}}$ be the vector embedding of the candidate curiosity task:

$$\mathcal{A}_t = \cos(\vec{v}_{\text{task}}, \vec{G}) = \frac{\vec{v}_{\text{task}} \cdot \vec{G}}{\|\vec{v}_{\text{task}}\| \|\vec{G}\|}$$

* **Condition for Execution:** $\mathcal{A}_t \ge \tau$, where default strict threshold $\tau = 0.75$.
* **Condition for Dormant Queuing:** $0.50 \le \mathcal{A}_t < 0.75$ (Saved to Dream Reflection backlog for nightly evaluation).
* **Condition for Immediate Rejection:** $\mathcal{A}_t < 0.50$ (Discarded immediately to protect resources).

---

## 3. Structural Integration in Agent WASP

The Curiosity-Goal architecture integrates cleanly into existing modules without disrupting standard reactive chat workflows:

| Module | Location | Role in Curiosity-Goal Loop |
| :--- | :--- | :--- |
| **Curiosity Evaluator** | `containers/agent-core/src/curiosity/evaluator.py` | Calculates $C_t$, measures epistemic surprise, checks domain salience $S(X)$, and enforces quota penalty $\Lambda_t$. |
| **Teleological Filter** | `containers/agent-core/src/curiosity/teleology.py` | Computes semantic cosine similarity against active goal vectors and evaluates task candidate viability. |
| **Autonomous Generator** | `containers/agent-core/src/scheduler/autonomous.py` | Background runner (every 30m) that gathers world state, calls the Curiosity Evaluator, and safely triggers aligned goals. |
| **Goal Orchestrator** | `containers/agent-core/src/goal_orchestrator/orchestrator.py` | Ingests curiosity goals, builds dependency graphs, applies the cognitive budget, and verifies step outcomes. |
| **Memory Manager** | `containers/agent-core/src/memory/manager.py` | Closes the loop: stores verified lessons, updates knowledge graph edges, and reduces future entropy $\mathcal{H}(X)$. |

---

## 4. Operational Behaviors & Scenarios

### Scenario A: Coding & Codebase Evolution
1. **Observation:** A test in `agentwasp` fails intermittently due to a race condition in Redis state loading.
2. **Curiosity Trigger:** High epistemic surprise ($\mathcal{D}_{\text{KL}}$) — deterministic test failed unexpectedly.
3. **Goal Check:** Active goal is "Maintain 100% Agent WASP platform stability". Alignment score $\mathcal{A}_t = 0.94 \ge 0.75$.
4. **Autonomous Action:** WASP does not just log the failure. It spins up a test runner, isolates the race condition, verifies the fix in a sandbox, and presents the minimal diff with proof of fix.

### Scenario B: Trading & Market Signals
1. **Observation:** Background Perception detects a 5.8% price divergence between Binance and Bybit on an asset tracked by the user.
2. **Curiosity Trigger:** Anomaly detected; high salience ($S(\text{Trading}) = 1.0$).
3. **Goal Check:** Operator goal is "Automated Quantitative Market Monitoring". Alignment score $\mathcal{A}_t = 0.88 \ge 0.75$.
4. **Autonomous Action:** Queries orderbook depth on both venues, verifies funding rate impact, and flags the arb/divergence opportunity in the user dashboard.

### Scenario C: Noise Rejection (Preventing the Curiosity Trap)
1. **Observation:** A web scraper encounters an article detailing celebrity gossip or irrelevant tech trivia.
2. **Curiosity Trigger:** Entropy is present, but Domain Salience is low ($S(X) = 0.05$).
3. **Goal Check:** Alignment with active development/trading goals yields $\mathcal{A}_t = 0.12 \ll 0.75$.
4. **Autonomous Action:** Hard rejection. Zero tokens or background tasks allocated.

---

## 5. Configuration & Governance

Operators can tune curiosity dynamics via `.env` or the Dashboard Configuration panel:

```bash
# --- Curiosity & Teleological Loop Controls ---
CURIOSITY_ENABLED=true
CURIOSITY_THRESHOLD=0.65            # Min Ct required to generate curiosity probe
TELEOLOGY_ALIGNMENT_THRESHOLD=0.75   # Strict cosine threshold tau
CURIOSITY_MAX_TASKS_PER_DAY=5        # Hard limit on autonomously spawned tasks
CURIOSITY_GOVERNOR_RAM_LIMIT=80.0    # RAM % cutoff (Lambda_t -> 1.0)
```

With this architecture, Agent WASP transitions from a tool you command to an **intellectually engaged, goal-aligned collaborator** that works alongside you.
