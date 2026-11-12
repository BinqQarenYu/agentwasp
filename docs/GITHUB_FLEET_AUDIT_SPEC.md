# GitHub Fleet Audit Specification & Common Sense Heuristics

## 1. Executive Summary
This document establishes the official operational boundaries, parameters, and cognitive heuristics for Agent WASP's **Repository Overseer (Bot 15)**. 

Managing 81+ repositories across personal (`BinqQarenYu`) and organization (`bingkltech`) accounts requires strict delimitation to prevent disk exhaustion, API rate-limiting, and noisy cosmetic review overhead.

---

## 2. The 4 Bounded Audit Parameters

```
                              ┌────────────────────────────────────────┐
                              │      FLEET REPOSITORY AUDIT SPEC       │
                              └───────────────────┬────────────────────┘
                                                  │
          ┌───────────────────────┬───────────────┴───────┬────────────────────────┐
          ▼                       ▼                       ▼                        ▼
   [PARAMETER 1]           [PARAMETER 2]           [PARAMETER 3]            [PARAMETER 4]
TOOL & SKILL ASSETS     SECURITY & LEAK RISK    FLEET HEALTH & STATE     DUPLICATE OVERLAP
"What can WASP use?"    "Are tokens exposed?"   "Is it live or stale?"   "Are we repeating?"
```

### Parameter 1: Tool & Skill Ingestion (Asset Discovery)
- **Objective:** Identify repositories that can serve as immediate functional capabilities/skills for Agent WASP's 15-bot workforce.
- **Criteria:**
  - Presence of CLI entry points (`main.py`, `cli.py`, `package.json#bin`).
  - Presence of Agent skills / OpenClaw specifications (`SKILL.md`, `openclaw.json`).
  - Containerization ready (`Dockerfile`, `docker-compose.yml`).
- **Primary Targets in Fleet:** `bingkltech/openclaw`, `bingkltech/graphify`, `bingkltech/agentmemory`, `bingkltech/Skill_Seekers`.

### Parameter 2: Security & Leak Guard (Vulnerability Assessment)
- **Objective:** Prevent accidental credential leaks, unencrypted keys, or sensitive code exposure.
- **Criteria:**
  - Detect high-entropy files in repo trees: `.env`, `id_rsa`, `*.pem`, `service_account.json`.
  - Visibility audit: Flag repositories containing proprietary trading logic or personal keys that are configured as `PUBLIC` instead of `PRIVATE`.
  - Fail-closed alert: Immediately notify via Cockpit gate if a high-risk secret file is exposed in a public repository.

### Parameter 3: Fleet Health & Activity Classification
- **Objective:** Categorize all 81 repositories by lifecycle state to remove clutter.
- **Classification Tiers:**
  1. **Tier 1: Core Active** — Commits within last 30 days, actively driving operations (e.g., `agentwasp`, `agency-agents`).
  2. **Tier 2: Tool Repositories** — Standalone utilities imported on demand (e.g., `graphify`, `colibri`, `OmniParser`).
  3. **Tier 3: Upstream Forks / Research** — Upstream references kept for study or tracking (e.g., `crewAI`, `AutoGPT`, `MetaGPT`, `claude-code`).
  4. **Tier 4: Stale / Archived** — Zero activity for >180 days with no active dependencies.

### Parameter 4: Duplicate & Fork Divergence (Deduplication)
- **Objective:** Resolve duplicate repositories between personal (`BinqQarenYu`) and organization (`bingkltech`).
- **Target Repositories with Dual Presence:**
  - `crewAI`
  - `hermes-agent`
  - `immich`
  - `FreeToken`
  - `Vibe-Trading`
  - `TheBrainMaker`
  - `quolas`
- **Criteria:** Determine commit distance (ahead/behind upstream) to identify which fork holds custom commits and which is a stale clone.

---

## 3. Strict Anti-Parameters (Hard Guardrails)

To preserve host resources and system stability, the following actions are **strictly forbidden** during an audit:

1. **No Bulk Cloning:** Never execute mass `git clone` across the fleet. Repositories must be inspected via GitHub REST API / GraphQL metadata.
2. **No Autonomous Remote Writes:** Audit is 100% read-only. No autonomous pushes, no branch creation, no branch deletion, no PR merging.
3. **No Cosmetic Linter Spam:** Never fail an audit or burn LLM tokens over PEP8 whitespace, formatting styles, or missing docstrings in third-party or archived code.
4. **No Unbounded LLM Context:** Do not feed whole codebases into LLM prompts. Only pass structured JSON summaries to the models.

---

## 4. The "Common Sense" Heuristics in Quantized Cognition

Alongside **Curiosity** (Active Inference) and **Intuition** (Pattern Recognition), **Common Sense** functions as the grounded reality filter:

```
┌─────────────────┬───────────────────────────────────┬───────────────────────────────────────────┐
│ Vector          │ Cognitive Role                    │ Formal Mathematical / Heuristic Rule      │
├─────────────────┼───────────────────────────────────┼───────────────────────────────────────────┤
│ 1. Curiosity    │ Exploratory Information Gain      │ Maximizes epistemic value:                │
│                 │ (Teleology & Active Inference)    │ G(π) = E[ln P(o|s,π) - ...]               │
├─────────────────┼───────────────────────────────────┼───────────────────────────────────────────┤
│ 2. Intuition    │ Fast Heuristic Pattern Match      │ Vector similarity against MEMORY.md       │
│                 │                                   │ and verified past workflows.              │
├─────────────────┼───────────────────────────────────┼───────────────────────────────────────────┤
│ 3. Common Sense │ Grounded Safety & Restraint Gate  │ Multilateral feasibility check:           │
│                 │                                   │ 1. Irreversibility risk < Threshold       │
│                 │                                   │ 2. Resource cost proportional to output   │
│                 │                                   │ 3. Rate-limit headroom > 20%              │
│                 │                                   │ 4. Reusability > Reinventing              │
└─────────────────┴───────────────────────────────────┴───────────────────────────────────────────┘
```

### The 4 Heuristic Rules of Common Sense:
1. **Destructive Restraint:** If an operation cannot be undone with `git checkout` or an undo command, it MUST require human approval via the Cockpit Gate.
2. **Resource Proportionality:** The computational and token expense of an operation must not exceed the economic value of the result.
3. **Rate-Limit Politeness:** Monitor API quota and auto-throttle when quota drops below 20%.
4. **Reuse Over Reinvention:** Query existing tools in the fleet before writing duplicate scaffolding.
