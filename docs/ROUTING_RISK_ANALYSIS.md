# Critical Risk Analysis: Tiered Semantic AI Model Routing

> **Focus:** Preventing false logic, arithmetic errors, classification failures, and systemic bottlenecks when dividing tasks between High-End Cloud AI and Local Ollama.

---

## 1. Deep Dive into the Hidden Flaws & Failure Modes

When an autonomous agent attempts to decide *"use local 8B for simple, use cloud 70B for complex"*, several non-obvious failure modes arise:

### Flaw 1: The "Simple Task" Optical Illusion (The Math & Logic Trap)
* **The Failure:** 
  A prompt looks syntactically short and simple, so the router classifies it as "Quick / Simple" and routes it to an 8B local model.  
  *Example:* `"Is 17% of $4,850 greater than 3.2% of $26,000, and by how much?"` or `"If Alice is twice Bob's age in 4 years, who is older today?"`
* **Why Small Local Models Fail:** 
  Small language models (3B–8B) are notoriously poor at mental arithmetic and multi-step symbolic deduction without Chain-of-Thought (CoT). They will confidently output a completely hallucinated number or reversed logic.
* **The Mitigation (Strict Tool Enforcement):**
  **Never let ANY LLM do raw math in its head.** All mathematical operations, financial reconciliations, and numerical comparisons must be routed through deterministic tools (`calculate` skill or Python execution sandbox), not pure text generation.

---

### Flaw 2: The Classifier Paradox (Who Classifies the Classifier?)
* **The Failure:** 
  If you use a regex or keyword matcher to classify tasks, it is brittle:
  - If a user asks: `"Can you quickly review this 500-line smart contract for security exploits?"`
  - A naive keyword matcher sees the word `"quickly"` and flags it as a `QUICK` task, assigning it to a lightweight local model. The local model skims it and misses critical re-entrancy bugs!
* **The Mitigation (Multi-Dimensional Feature Vector):**
  Classification must NOT rely on single keywords. It must evaluate three hard metrics:
  $$\text{Complexity Score} = w_1 \cdot \text{Token Length} + w_2 \cdot \text{Tool Count} + w_3 \cdot \text{Domain Risk Level}$$
  - Any task involving **Security, Code Review, Financial Calculation, or System Shells** automatically escalates to the **High-End Cloud Tier**, regardless of prompt length.

---

### Flaw 3: Incompatible Tool-Calling Dialects
* **The Failure:** 
  - **Gemini / OpenAI / Claude:** Emit standardized, schema-validated JSON function calls with strict typing (`tool_calls: [...]`).
  - **Local Ollama (8B/7B):** Frequently hallucinate tool call syntax, output broken JSON, invent non-existent arguments, or output tool calls as conversational text instead of structured payloads.
* **The Impact:** 
  If the router assigns a tool-heavy step (e.g. `scrape(url) -> extract_fields() -> sql_insert()`) to an 8B model, the model may return invalid JSON, crashing the pipeline or triggering endless retry loops.
* **The Mitigation (Role Boundary):**
  - **Local Ollama:** Restricted strictly to **read-only summarization, classification, and conversational drafting**.
  - **High-End Cloud Tier:** Sole owner of **multi-step Tool Orchestration, TaskGraph generation, and System Actions**.

---

### Flaw 4: VRAM Swapping / OS Thrashing on Low-RAM Machines
* **The Mathematical Reality:**
  - An 8B parameter model at 4-bit quantization (Q4_K_M) requires **~4.8 GB of VRAM**.
  - If the machine has an 8GB or 16GB shared-memory GPU/CPU, having Docker (Postgres + Redis + Chromium browser) running simultaneously means VRAM can saturate.
  - When memory saturates, the OS swaps to NVMe disk, causing execution time to spike from 1 second to **45+ seconds**, freezing the dashboard UI.
* **The Mitigation (Hardware Pre-Flight Check):**
  The router must ping system resource telemetry (`CPI monitor` in WASP). If host memory is $>80\%$, the agent **must disallow local execution** and stay on the cloud tier with rate pacing.

---

## 2. Hardened Architecture Matrix

| Task Category | Route Destination | Justification & Safeguards |
| :--- | :---: | :--- |
| **Arithmetic & Financials** | **Python Tool + Cloud Tier** | Deterministic Python calculation; zero pure-LLM math hallucinations. |
| **Code Review & Audits** | **High-End Cloud (Gemini/Grok)** | Small models miss subtle logic flaws; require high parameter reasoning. |
| **Tool Orchestration / Multi-step** | **High-End Cloud Tier** | Guaranteed JSON schema adherence for tool execution. |
| **Inbox Triage / Text Categorization** | **Local Ollama (8B)** | Simple classification with low risk; saves 80% of daily cloud quota. |
| **Chit-chat / Casual Summaries** | **Local Ollama (8B)** | Zero impact on reasoning; perfect for free local execution. |
| **Done-Checker & Plan Critic** | **High-End Cloud Tier (Mandatory)** | The evaluator MUST be smarter than the workers it audits. |

---

## 3. Conclusion & Recommendation

Dynamic routing is mathematically viable and highly cost-effective, **provided the router enforces deterministic tool delegation for math and reserves all critic/tool-calling steps for the high-end tier.**


---

## 4. Implementation & Verification Status

The hardened routing engine has been implemented and verified in `containers/agent-core/src/models/router.py`.

### Verified Test Cases:
1. **Math & Financial Trap:** `"Is 17% of $4,850 greater than 3.2% of $26,000?"`  
   - **Verdict:** Correctly classified as `financial_math_high_risk`.  
   - **Action:** Mandatory escalation to Cloud Tier with deterministic tool calculation.
2. **Keyword Classifier Exploit:** `"Can you quickly review this Solidity contract for vulnerability re-entrancy?"`  
   - **Verdict:** Correctly classified as `deep_engineering_high_risk` despite the presence of the word *"quickly"*.  
   - **Action:** Escalated to High-End Cloud Tier (Gemini Pro / GPT-4o / Grok-2).
3. **Safe Local Trivial Task:** `"Classify if this spam email is valid or junk"`  
   - **Verdict:** Correctly classified as `trivial_local_tier`.  
   - **Action:** Executed on Local Ollama (zero cloud quota consumed).
4. **Hardware Safety Guard:** Pings system memory via Cognitive Pressure Index (CPI). If host memory > 80%, local VRAM execution is automatically suspended in favor of cloud rate-paced calls.
