# Master Technical Documentation: Agent WASP Operations, Threat Sentinel & Teleological Loop

> **Operator Profile:** Architecture & Design • Full-Stack AI Engineer (Jules / Google Coding) • Quantitative / Algorithmic Trader • Online Marketplace & E-Commerce Operator  
> **Primary Connected Account:** `benjohncarino@gmail.com`  
> **Release:** v2.8.0  
> **Date:** March 2026  

---

## 1. Executive Summary of All Engineering Upgrades

Agent WASP is architected as a sovereign, multi-agent autonomous cockpit uniting human intent with self-stabilizing intelligence:

1. **Grok-Style OLED Cockpit Interface (`chat.html` & `style.css`):**
   - Translucent glassmorphic hero omni-bar with `#f5c542` focus glow.
   - Three-way Mode Switcher: 🚀 **15-Bot Mesh**, 🧠 **DeepSearch**, ⚡ **Direct**.
   - Live collapsible execution trace and real-time streaming state engine.
2. **Altair AI 15-Bot Agent Mesh & Splitter Visualizer:**
   - Decomposes high-level commands (*"Run my week"*) into micro-units across specialized personas (Inbox, Leads, Invoices, Content, Code Review, etc.).
   - Multi-color progress track: 🟩 Passed, 🟨 Active, 🟥 Rejected.
   - Rejection-to-Rule memory auto-learner.
3. **Human-in-the-Loop Financial Safeguard Card:**
   - Detects irreversible transactions (wire transfers, external bulk emails, file deletions).
   - Suspends execution and awaits explicit operator authorization (`✓ Authorize & Execute` vs. `✕ Reject with Rule`).
4. **Free-Tier AI Quota & RPM Limiter (`quota_guard.py`):**
   - Enforces a 12 RPM / 1,400 RPD safety ceiling on Google Gemini Free Tier with 4.0s minimum spacing.
   - Prevents `429 Rate Limit Exceeded` lockouts and cost overruns.
5. **Hardened Tiered Model Router (`router.py`):**
   - **Math & Financial Trap Defense:** Reconciles numbers via deterministic Python execution tools rather than hallucinated LLM mental arithmetic.
   - **Domain Escalation:** Security audits and code refactoring automatically escalate to High-End Cloud Tier (Gemini Pro/Flash, Grok-2, GPT-4o).
   - **Safe Local Offloading:** Only trivial, read-only classification tasks route to local Ollama (8B).
   - **Hardware Safety:** Pings Cognitive Pressure Index (CPI); aborts local execution if host RAM exceeds 80%.
6. **Multi-Account Gmail Management (`gmail.py`):**
   - Upgraded to support multiple accounts by alias (`personal`, `work`, `billing`).
   - Hardened with `GMAIL_RECIPIENT_ALLOWLIST` so outgoing communication is strictly locked to approved destinations.
7. **Email Threat Sentinel (`email_threat_sentinel.py` & `mail_sentinel_job.py`):**
   - 4-Vector Inspection Engine: Flags malicious links (raw IP hosts, shorteners, punycode/IDN attacks), brand impersonation/spoofed domains, psychological urgency/scam heuristics, and dangerous attachment payloads (`.exe`, `.scr`, `.bat`, `.xlsm`).
   - Automated 30-minute background triage and quarantine.
8. **Curiosity & Goal Teleological Integration (`src/curiosity/`):**
   - Elevates **Curiosity** ($\mathcal{C}_t$) as the engine of epistemic discovery and **Goal** ($\vec{G}$) as the teleological boundary attractor.
   - Prevents infinite loops or curiosity drift via mathematical vector alignment ($\cos(\vec{v}, \vec{G}) \ge \tau$).

---

## 2. The Curiosity-Goal Teleological Loop Architecture

### 2.1 The Two Elevated Words: Defined
* **Curiosity ($\mathcal{C}_t$):** The intrinsic cognitive discomfort produced by an awareness of an Information Gap between what is known and what needs to be known, coupled with an active inference drive to resolve that gap.
* **Goal ($\vec{G}$):** The teleological boundary and gravitational attractor defined by the human operator's strategic intent (e.g. trading edge, clean architecture, operational security).

### 2.2 Mathematical Engine
Curiosity is computed dynamically via the Intrinsic Curiosity Index:

$$C_t = \mathcal{H}(X) \times \mathcal{D}_{\text{KL}}(P_{\text{prior}} \parallel P_{\text{evidence}}) \times S(X) \times (1 - \Lambda_t)$$

The candidate curiosity task $\vec{v}$ is evaluated against the active operator goal graph:

$$\mathcal{A}_t = \cos(\vec{v}_{\text{curiosity}}, \vec{G}_{\text{operator}}) \ge \tau \quad (\tau = 0.65)$$

* **$\mathcal{A}_t \ge 0.65$ (APPROVED):** Ingested into `GoalOrchestrator` as an active task step.
* **$0.40 \le \mathcal{A}_t < 0.65$ (DORMANT_QUEUED):** Buffered into Dream Reflection for consolidation.
* **$\mathcal{A}_t < 0.40$ (REJECTED):** Pruned instantly without spending API tokens.

### 2.3 Integrated Code Structure
* `containers/agent-core/src/curiosity/evaluator.py`: Implements $C_t$ calculation with domain salience and RAM throttling.
* `containers/agent-core/src/curiosity/teleology.py`: Implements semantic similarity, ontology clustering, and strict vector alignment.
* `containers/agent-core/src/scheduler/autonomous.py`: Ingests active Redis goals every 30m, filters candidate actions through `TeleologicalAligner`, and logs pruned/approved decisions.
* `containers/agent-core/src/agent/context.py`: Top personality trait sets WASP to act with epistemic drive rather than passive compliance.

---

## 3. Live Email Audit & Threat Scan Results

* **Active Primary Account:** `benjohncarino@gmail.com`
* **Live IMAP/SSL Status:** Verified Active (`imap.gmail.com:993`)
* **Inbox Scan (Latest 60 Messages):** **0 High-Risk Threats** detected (Clean).
* **Quarantined Spam Folder (`[Gmail]/Spam`):** Cleaned and purged (44 spam messages permanently removed).
* **Automated Sentinel Frequency:** Runs every 30 minutes via `mail_sentinel_job.py`.

---

## 4. High-Leverage 5-Channel Email Workflow

Your daily email stream is mapped into **5 operational channels**:

1. **Trading & Financial Sentinel:** Exchange fills (Binance, Bybit, HTX), funding rates, bank deposits. Immediate Telegram alert on margin or liquidation warnings.
2. **Jules / Google & AI Dev Ops:** Google Cloud/Jules agent notifications, GitHub PR reviews, CI/CD build fails.
3. **Architecture & Spatial Design:** Client RFPs, CAD/BIM revisions, contractor submittals. Generates pre-drafted replies for 1-click authorization.
4. **Online Marketplace & E-Commerce:** Order confirmations, inventory supplier alerts, customer dispute shields.
5. **Infrastructure Watchdog:** API credit thresholds (Gemini, Supabase, Neon, Render) to prevent service dropouts.
