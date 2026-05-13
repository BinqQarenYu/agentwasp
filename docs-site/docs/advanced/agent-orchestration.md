---
id: agent-orchestration
title: Agent Orchestration
description: Multi-agent system patterns, lifecycle, and inter-agent communication.
---

# Agent Orchestration

WASP supports running multiple autonomous sub-agents simultaneously, each with independent objectives, goal queues, and execution contexts.

## Use Cases

- **Parallel research**: Run 5 agents researching different topics simultaneously
- **Monitoring agents**: Long-running agents that watch prices, RSS feeds, or system metrics
- **Specialized agents**: Agents optimized for specific domains (coding, web scraping, data analysis)
- **Hierarchical teams**: Meta-agent supervises a team of specialists

## Creating Agents

Via Telegram/Dashboard:

```
agent_manager(
    action="create",
    name="btc_monitor",
    objective="Check BTC price every hour and alert if it changes by more than 3%",
    priority=6
)
```

### Agent Name Extraction (v2.6)

When creating agents from natural language, the handler extracts the name via three regex patterns in `_AGENT_NAME_PATTERNS`. The patterns match `named X` and equivalent constructions in supported languages. As of v2.6, these patterns are **non-greedy** with a lookahead stop-set:

```python
re.compile(
    r'\b(?:named?)\s+["\']([\w\s-]{1,40})["\']'
    r'|\b(?:named?)\s+'
    r'([\w-]+(?:\s+[\w-]+){0,4}?)(?=\s+(?:that|to|with|for|on|in)\b|[,.!?]|$)',
    re.IGNORECASE,
)
```

The non-greedy `{0,4}?` modifier and lookahead clause connectors ensure the **shortest valid match wins**:

| User input | Extracted name |
|-----------|----------------|
| `create an agent named Bob to track news` | `Bob` |
| `create an agent named "Crypto Watcher" for BTC alerts` | `Crypto Watcher` |
| `create an agent named News Watcher that monitors RSS feeds every hour` | `News Watcher` |

Quoted forms (`named "Foo Bar"`) take priority via the first alternation group — ideal when the agent name itself contains common stop-words.

Via API:

```bash
curl -b cookies.txt -X POST https://agentwasp.com/api/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "research_agent",
    "objective": "Daily: check top AI news and summarize to memory",
    "priority": 5
  }'
```

## Agent vs. Goal

Key distinction:
- A **Goal** is a single objective with a defined end state
- An **Agent** is a persistent entity that can pursue multiple goals over time

An agent can be:
- Restarted with new goals after completing each one
- Paused and resumed
- Given recurring reminders to trigger new goal cycles

## Recurring Agent Patterns

The most powerful pattern: create an agent, then set a reminder to restart it periodically:

```
# Create the agent
agent_manager(action="create", name="daily_digest", objective="Compile daily news digest")

# Then set a recurring reminder linked to the agent
create_reminder(
    message="Compile daily news digest",
    time="09:00",
    recurring="daily",
    agent_id="<agent-id-from-above>"
)
```

When the reminder fires, `ReminderCheckerJob` calls `agent_orchestrator.create_agent_goal()`, restarting the agent's execution cycle.

## Agent Priority

| Source | Priority | Notes |
|--------|----------|-------|
| User-created agent | 8 | High priority |
| Agent-created agent | 6 | Medium |
| Autonomous agent | 3 | Low priority |

Higher-priority agents get their goal ticked first in each cycle.

## Managing Agents

```
# List all agents
agent_manager(action="list")

# Pause an agent
agent_manager(action="pause", agent_id="...")

# Resume a paused agent
agent_manager(action="resume", agent_id="...")

# Archive (delete) an agent
agent_manager(action="archive", agent_id="...")
```

## Resource Limits

The global token budget prevents agents from consuming all LLM capacity:

```python
agents_global_token_budget_per_minute = 100_000
```

When this budget is exceeded, agent ticks are paused until the next minute window.

Individual agents can also be given their own token budgets via the goal orchestrator.

## Multi-Agent State Isolation

Each agent has isolated:
- Goal queue (separate goal IDs in Redis)
- Execution context (separate `chat_id` for notifications)
- Memory access (agents can read shared memory but write to their own namespace)

Agents do NOT have isolated:
- Skill registry (all agents use the same skills)
- Model manager (all agents share the same LLM providers)
- PostgreSQL (all agents write to the same database)

## Multi-Brain Sub-Agents (Backend Ready)

The `Agent` type has `model_provider` and `model_name` fields, and `AgentRuntime` honors them via a temporary provider switch when an agent ticks. This means sub-agents can run on different LLM brains than the parent operator agent — useful for cost-optimized monitoring agents (Gemini Flash) alongside heavyweight analysis agents (Claude Opus).

**Status:** the backend (`Agent` type, `AgentRuntime` switching) is fully operational. The `AgentManagerSkill._create()` does not yet expose `model_provider`/`model_name` as parameters from natural-language commands — operators can spawn multi-brain agents only via direct DB insert or the dashboard agent-creation form.

This is a UX gap, not a security or correctness gap. A future patch will expose these parameters via the skill API.

## Meta-Agent Supervisor (Advanced)

When `META_AGENT_ENABLED=true`, the `MetaSupervisor` can coordinate agent teams:

```
meta_orchestrate(
    objective="Research and compare the top 5 Python web frameworks",
    team_size=5,
    strategy="parallel_then_synthesize"
)
```

The supervisor:
1. Decomposes the objective into specialized sub-tasks
2. Creates 5 specialized agents (one per framework)
3. Monitors progress across all agents
4. When all complete, synthesizes the results into a unified report

Currently disabled by default (`META_AGENT_ENABLED=false`).

## Debugging Agents

```bash
# View all agent state in Redis
docker exec agent-redis redis-cli HGETALL agents

# View a specific agent
docker exec agent-redis redis-cli HGET agents <agent-id>

# View agent in PostgreSQL (survives Redis flush)
docker exec agent-postgres psql -U agent -d agent -c \
  "SELECT id, name, status, priority, created_at FROM agents ORDER BY created_at DESC;"

# View goals for an agent
docker exec agent-redis redis-cli HGETALL goals | python3 -c "
import sys, json
data = sys.stdin.read().split('\n')
goals = [json.loads(data[i+1]) for i in range(0, len(data)-1, 2)]
for g in goals:
    if g.get('source') == 'agent':
        print(f'{g[\"id\"]}: {g[\"objective\"]} ({g[\"status\"]})')
"
```

## Performance Considerations

With many active agents:
- Each agent ticks every 15 seconds
- Each tick can execute up to 3 steps
- LLM calls are the bottleneck (each step = 1 LLM call minimum)

For 5 agents running simultaneously:
- Minimum: 5 LLM calls per 15s = 20 calls/minute
- With `AGENTS_GLOBAL_TOKEN_BUDGET_PER_MINUTE=100000`, this is easily within budget for fast models
