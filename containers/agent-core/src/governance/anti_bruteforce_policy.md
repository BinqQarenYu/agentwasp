# SYSTEM DIRECTIVE: ANTI-BRUTEFORCE & PROVIDER RATE-LIMIT GOVERNANCE
# Target: All 15 Autonomous Bots (Grok Mesh Workforce) + Athena (Bot 15)
# Authority: Operator Executive Mandate

## 1. Core Principle: Zero Bruteforce & Zero Burst Work
No bot in the Agent WASP workforce is permitted to execute high-frequency burst loops, blind brute-force retries, or unthrottled API polling. Operations must be smooth, paced, deterministic, and respectful of host resources and provider quotas.

---

## 2. Mandatory Rules for All 15 Mesh Bots

### Rule 1: Anti-Bruteforce & Retry Limit
- **Max Consecutive Retries:** Exactly **3 attempts** for any failed tool call or network request.
- **Backoff Protocol:** Mandatory exponential backoff with jitter (`2s -> 5s -> 10s`).
- **Circuit Breaker:** If a tool or external API fails 3 times consecutively, the bot MUST trip the circuit breaker, pause execution, log the error, and escalate to the operator rather than retrying blindly.

### Rule 2: Anti-Burst Work & Pacing
- **No Concurrent Floods:** Never spawn unbounded parallel workers or unbounded async tasks. Parallel execution in the Splitter Gate is strictly capped at **3 concurrent bot executions**.
- **Request Pacing:** Enforce a minimum interval between external API requests (e.g., minimum 500ms between GitHub REST queries, minimum 1s between web scrapes).
- **Batch Processing:** Bulk actions across repositories or emails must be chunked into batches of $\le 10$ items with pause intervals.

### Rule 3: AI Provider Management & Quota Respect
- **Token Conservation:** Favor cached metadata and structured regex/AST parsing over sending raw multi-megabyte payloads to LLMs.
- **Provider Fallback Cascade:** 
  1. Primary: Local Ollama (zero API token cost, throttled to preserve host CPU/VRAM).
  2. Free-Tier Bridging: GitHub Copilot CLI (`gh copilot`) for mechanical commands and code explanations.
  3. Cloud Fallback (Gemini / Claude / Grok): Used strictly for high-order reasoning, never for repetitive loops.
- **Rate-Limit Buffer:** When any external API or provider reports that remaining quota has dropped below **20%**, the bot MUST throttle its execution frequency by 50% and notify the Cockpit.

---

## 3. Bot-Specific Additions to Job Descriptions

| Bot Designation | Special Restriction |
| :--- | :--- |
| **Bot 15: Athena (Fleet Steward)** | **NO MASS CLONING.** Inspects via lightweight REST API metadata. Capped at 1 repo inspection per 2 seconds. Never brute-forces commit history searches. |
| **Bot 01: Inbox Triage** | Read-only polling capped at 60s intervals. No rapid refresh bursts against Gmail API. |
| **Bot 04: Expense Tracker** | OCR and PDF parsing processed sequentially, one document at a time. |
| **Bot 05: Bug Repro** | Test executions bounded with a 30-second hard execution timeout to prevent runaway loops. |
| **Bot 09: Research** | Scraping queries rate-limited with polite delays (robots.txt compliance). |
| **Bot 11: Lead Scout** | Apollo/Hunter/Web lookups chunked with random human-like delays. |
| **Bot 14: Social Poster** | Publication queues enforce minimum 30-minute intervals between social posts. |

---

## 4. Enforcement Mechanism
These rules are hard-coded into the WASP `PolicyEngine`, `ResponseGuard`, and `IntentGate`. Any bot attempting an unthrottled burst loop is intercepted and halted before execution.
