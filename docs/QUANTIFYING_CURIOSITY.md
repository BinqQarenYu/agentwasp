# Mathematical Formulation: Quantifying Human Curiosity in Autonomous Agents

> **Theoretical Foundation:** Information Theory (Shannon Entropy) • Loewenstein's Information-Gap Theory • Friston's Active Inference (Free Energy Principle)  
> **Application:** Transforming Agent WASP from a passive executor into an intrinsically motivated, intellectually curious partner.

---

## 1. Defining Human Curiosity

In cognitive neuroscience and psychology (George Loewenstein, 1994; Karl Friston, 2010), **human curiosity** is not random wandering. It is defined as:

> *"The innate cognitive discomfort produced by an awareness of an Information Gap between what one knows and what one wants or needs to know, coupled with an intrinsic drive to resolve that gap."*

Human curiosity exhibits two distinct dimensions:
1. **Perceptual / Diversive Curiosity:** The drive to explore novelty and unexpected changes in the environment (e.g. noticing an unexpected market spike or anomalous build failure).
2. **Epistemic Curiosity:** The systematic desire for intellectual mastery, deep causal explanations, and filling logical holes in mental models (e.g. asking *"Why did this component break?"* or *"What is the economic connection between these two events?"*).

---

## 2. Quantifying Curiosity (The Mathematical Formula)

To implement curiosity into software without causing erratic behavior or infinite loops, we can compute an **Intrinsic Curiosity Index ($C_t$)**:

$$C_t = \underbrace{\mathcal{H}(X)}_{\text{Uncertainty}} \times \underbrace{\mathcal{D}_{\text{KL}}(P_{\text{prior}} \parallel P_{\text{evidence}})}_{\text{Information Gain / Surprise}} \times \underbrace{S(X)}_{\text{Domain Salience}} \times \underbrace{(1 - \Lambda_t)}_{\text{Safety Governor}}$$

Where:
* **$\mathcal{H}(X)$ (Entropy / Uncertainty Gap):** How incomplete the agent's knowledge graph is regarding the current topic ($0.0 \le \mathcal{H} \le 1.0$).
* **$\mathcal{D}_{\text{KL}}$ (Kullback-Leibler Divergence / Epistemic Surprise):** The gap between what the agent expected to happen versus what actually happened in a test or market signal.
* **$S(X)$ (Domain Salience / Operator Relevance):** A multiplier based on your active domains (Architecture $= 0.9$, Trading $= 1.0$, AI Coding $= 1.0$, Random Trivia $= 0.1$). This prevents the agent from being curious about useless distractions.
* **$(1 - \Lambda_t)$ (Safety & Resource Governor):** If host RAM is saturated ($>80\%$) or daily API quotas are near limits, curiosity automatically throttles to $0$ to preserve resources.

---

## 3. Operationalizing Curiosity in Agent WASP

When $C_t \ge 0.65$, the agent's **Curiosity Persona Engine** triggers three concrete behaviors:

### Behavior A: Deep Follow-Up Inquiries (Proactive Alignment)
Instead of giving flat, robotic answers, WASP identifies missing logical links in your designs or trading setups and asks targeted questions:
* *Example:* *"You asked to track this token's orderbook, but I noticed its liquidity pool has a 72% imbalance on Uniswap. Should I monitor the LP contract health as well?"*

### Behavior B: Unprompted Hypothesis Testing
When code or tests fail in your workspace:
* Rather than simply printing `"Build failed"`, it autonomously executes a minimal sandbox probe to isolate the causal root:  
  *e.g. "I tested whether this failure was due to the network timeout or an invalid schema; the mock passed, meaning the bug is specifically in line 42's parser."*

### Behavior C: The "Serendipitous Discovery" Morning Drop
During its nightly consolidation:
* If it detects an emerging trend or research preprint relevant to your active projects (e.g., in algorithmic orderflow or generative architecture), it attaches a 1-paragraph curiosity note to your 07:30 AM briefing:  
  *💡 Curiosity Note: "While indexing your workspace, I noticed a new research approach for faster AST parsing that could reduce our graphify time by 40%."*


---

## 4. The Goal-Aligned Curiosity Loop: Teleological Stability

### The Concept: Teleological Equilibrium
Unconstrained curiosity is chaos (infinite rabbit holes). Pure goal-execution is rigid (fragile scripts that break on edge cases).

When **Curiosity** is mathematically constrained to **Goal Vector Alignment**, we achieve **Teleological Equilibrium**:

$$\mathcal{L}_{\text{curiosity}}(t) = \min_{\theta} \left[ \underbrace{\mathcal{D}_{\text{KL}}(P_{\text{world}} \parallel P_{\text{model}})}_{\text{Intrinsic Curiosity}} \right] \quad \text{subject to} \quad \cos(\vec{v}_{\text{curiosity}}, \vec{G}_{\text{operator}}) \ge \tau$$

Where:
* $\vec{G}_{\text{operator}}$ is the operator's core goal vector (e.g. profitable trading, clean architecture, automated e-commerce).
* $\cos(\vec{v}_{\text{curiosity}}, \vec{G}_{\text{operator}})$ measures the directional alignment between the agent's exploration and the operator's mission.
* $\tau \approx 0.75$ is the **Strict Alignment Threshold**. Any curious deviation below this cosine similarity is pruned before generating an execution task.

### The Self-Stabilizing Natural Loop:
1. **Goal Defines the Boundary (The Gravitational Orbit):** The operator sets the mission (e.g. *"Run my week / Expand trading pipeline"*).
2. **Curiosity Explores Within the Boundary (The Engine):** Discovers friction, edge cases, pricing discrepancies, and code bugs.
3. **TaskGraph Consolidates & Resolves:** The Done-Checker evaluates the discovery against pass conditions.
4. **Learning Tightens the Vector:** Every resolved curiosity task updates the behavioral rules and knowledge graph, making the *next* curious inquiry 10x more targeted and efficient.
