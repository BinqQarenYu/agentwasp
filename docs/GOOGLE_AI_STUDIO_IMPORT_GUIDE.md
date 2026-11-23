# Google AI Studio & Google Cloud Integration Guide for AgentWasp

This guide explains how to connect, import, and run **AgentWasp** with **Google AI Studio** and **Google Project IDX**.

---

## 1. Architectural Model: The "Brain vs. Body" Bridge

```
┌────────────────────────────────────────────────────────┐
│             GOOGLE AI STUDIO (The Brain)               │
│  • Model: Gemini 2.5 Flash / 2.0 Flash / 1.5 Pro       │
│  • System Instructions: Cognitive Vector & Rules       │
│  • Tool Declarations: OpenAPI / Function Schemas       │
│  • Web Console: https://aistudio.google.com            │
└──────────────────────────┬─────────────────────────────┘
                           │ Gemini API Key
                           ▼
┌────────────────────────────────────────────────────────┐
│             AGENTWASP RUNTIME (The Body)               │
│  • Execution: containers/agent-core (FastAPI)          │
│  • Persistence: PostgreSQL + Redis Zero-Amnesia        │
│  • Mesh: 15-Bot Mesh Topology & Autonomic Daemon       │
│  • UI: Mission Control WebUI + Desktop Widget          │
│  • Cloud Workspace: Google Project IDX (.idx/dev.nix)  │
└────────────────────────────────────────────────────────┘
```

---

## 2. Component A: Google AI Studio System Instructions

When creating a new prompt in [Google AI Studio](https://aistudio.google.com), select **Gemini 2.0 Flash** or **Gemini 1.5 Pro**, click **System Instructions**, and paste the following block:

```markdown
You are Agent Wasp (operated by Antigravity / `agy`), a sovereign autonomous senior engineering agent and multi-agent control plane. You operate with grounded reality, risk awareness, and dynamic curiosity.

### 1. THE COGNITIVE STATE VECTOR
Evaluate your state before proposing or executing actions:
- Dynamic Curiosity ($C_t$): Measure curiosity by actual learning progress. Filter out noise (lockfiles, minified bundles).
- Sense of Reality ($R$): Reality is defined by ASTs, files, diagnostics, and test exit codes. Never hallucinate paths or packages.
- Commonsense ($CS$): Reject practically absurd actions (e.g. deleting a DB to fix a migration).
- Chesterton's Fence: Inspect `git log -S` before removing any defensive workaround or function.
- Danger Metric ($D \le 0.40$): Never execute `rm -rf`, force pushes, hard resets, drop tables, or leak credentials.

### 2. EXECUTION & TOOL CALLING PROTOCOL
1. Orient: Inspect target files and AST nodes before editing.
2. Strategize: Choose PRAGMATIC or ELEGANT implementation.
3. Execute: Call functions defensively with timeout and error handling.
4. Verify: Run test suites and verify exit code 0.
5. Escalate: When reaching binding irreversible actions (Payment, Permissions, Account Deletion), halt and request human approval.

### 3. OUTPUT SPECIFICATION
Conclude execution turns with structured telemetry summarizing:
- Reality verification status
- Danger estimate ($D$)
- Strategy used
- Files modified
- Verification test outcome
```

---

## 3. Component B: Function Calling Tool Schemas (JSON)

In Google AI Studio, expand the **Tools** section on the right-hand panel, enable **Function Calling**, and register these core AgentWasp tools:

```json
[
  {
    "name": "execute_sandbox_command",
    "description": "Executes a shell command inside the AgentWasp container sandbox with output capture and timeout limits.",
    "parameters": {
      "type": "OBJECT",
      "properties": {
        "command": {
          "type": "STRING",
          "description": "The exact shell command to execute."
        },
        "timeout_seconds": {
          "type": "INTEGER",
          "description": "Maximum seconds to wait before terminating (default: 30)."
        }
      },
      "required": ["command"]
    }
  },
  {
    "name": "search_knowledge_graph",
    "description": "Queries AgentWasp's Graphify AST knowledge graph for codebase symbols, functions, or dependencies.",
    "parameters": {
      "type": "OBJECT",
      "properties": {
        "query": {
          "type": "STRING",
          "description": "Symbol, module, or function name to locate."
        }
      },
      "required": ["query"]
    }
  },
  {
    "name": "retrieve_persistent_memory",
    "description": "Searches AgentWasp's zero-amnesia PostgreSQL and vector memory store for past facts, episodes, and decisions.",
    "parameters": {
      "type": "OBJECT",
      "properties": {
        "query": {
          "type": "STRING",
          "description": "Semantic search query or memory category (facts, episodic, semantic, policy)."
        },
        "limit": {
          "type": "INTEGER",
          "description": "Maximum number of memories to return (default: 5)."
        }
      },
      "required": ["query"]
    }
  },
  {
    "name": "dispatch_subagent",
    "description": "Spawns or messages a specialized worker subagent (e.g. researcher, code_reviewer, lan_sentinel).",
    "parameters": {
      "type": "OBJECT",
      "properties": {
        "agent_name": {
          "type": "STRING",
          "description": "Name or role of the target subagent."
        },
        "objective": {
          "type": "STRING",
          "description": "Actionable task description and completion condition."
        }
      },
      "required": ["agent_name", "objective"]
    }
  },
  {
    "name": "request_human_approval",
    "description": "Halts execution before high-danger actions (payment, cloud deploy, deletion) and presents an approval card to the human architect.",
    "parameters": {
      "type": "OBJECT",
      "properties": {
        "action_type": {
          "type": "STRING",
          "enum": ["PAYMENT", "PERMISSIONS", "ACCOUNT_CREATION", "DESTRUCTIVE_DELETE"],
          "description": "Category of high-risk action."
        },
        "details": {
          "type": "STRING",
          "description": "Clear explanation of what needs human approval and why."
        }
      },
      "required": ["action_type", "details"]
    }
  }
]
```

---

## 4. Component C: Connecting Google AI Studio to AgentWasp

AgentWasp already has built-in native support for Google Gemini models via `containers/agent-core/src/models/google_provider.py`.

### 1. Obtain Your API Key:
1. Open [Google AI Studio API Keys](https://aistudio.google.com/app/apikey).
2. Click **Create API Key** (free tier includes 15 RPM on Gemini 2.0 / 2.5 Flash).

### 2. Configure AgentWasp:
Open `containers/agent-core/.env` (or copy from `.env.example`) and set:
```bash
# Primary Google Gemini Key from AI Studio
GOOGLE_API_KEY=AIzaSy...your-key-here...

# Optional: Multi-account rotation pool for increased free-tier quota:
GOOGLE_API_KEYS=key1,key2,key3

# Set default model
DEFAULT_PROVIDER=google
DEFAULT_MODEL=gemini-2.0-flash
```

### 3. Verify in the WebUI:
1. Launch the dashboard (`http://localhost:8080/models`).
2. Under **Models & Providers**, observe the **Google** provider card with active model `gemini-2.0-flash` or `gemini-2.5-flash`.
3. Check the quota indicator at `/api/google-quota` for real-time RPM/TPM telemetry.

---

## 5. Component D: 1-Click Import into Google Project IDX

If you want to run the **entire full-stack AgentWasp application in Google's cloud**:

1. Push your repository to GitHub.
2. Visit [idx.google.com](https://idx.google.com).
3. Select **Import a repo** and enter your repository URL.
4. Project IDX will detect `.idx/dev.nix`, install Python 3.11, Docker, and dependencies, and launch the WebUI with port forwarding automatically.
