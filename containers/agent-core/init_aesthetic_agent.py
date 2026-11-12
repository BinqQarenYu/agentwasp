
import asyncio
import os
import redis.asyncio as aioredis
from uuid import uuid4
from datetime import datetime, timezone

# Adjust sys.path to include src so imports work standalone
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env variables for standalone execution
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env'))

# Set default local URLs if not in docker
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("DATABASE_URL", f"postgresql+asyncpg://agent:{os.environ.get('POSTGRES_PASSWORD')}@localhost:5432/agent")

from src.db.session import init_db
from src.agent_manager.types import Agent, AgentStatus
from src.agent_manager.store import save_agent
from src.goal_orchestrator.types import Goal, GoalState, AutonomyMode
from src.goal_orchestrator.store import save_goal

async def main():
    redis_url = os.environ.get("REDIS_URL", "redis://agent-redis:6379/0")
    r = aioredis.from_url(redis_url, decode_responses=True)
    
    # Initialize DB first so async_session works
    await init_db()
    
    # 1. Create Agent
    agent = Agent(
        name="Aesthetic Researcher",
        description="Continuously research what makes a modern app beautiful. Extract core parameters, principles, and psychological meanings. Save findings to the knowledge base for app presentation requests.",
        autonomy_mode=AutonomyMode.FULL,
        status=AgentStatus.RUNNING
    )
    
    # 2. Create Goal
    goal = Goal(
        objective="Research and define the parameters of aesthetic beauty in modern web applications. Search for design trends, color psychology, and UI/UX best practices. Store findings in the knowledge base.",
        title="Aesthetic Research Phase 1",
        state=GoalState.ACTIVE,
        agent_id=agent.id,
        autonomy_mode=AutonomyMode.FULL,
        priority=3,
        source="agent"
    )
    
    # Link goal to agent
    agent.active_goal_ids.append(goal.id)
    
    # 3. Save both
    await save_agent(r, agent)
    await save_goal(r, goal)
    
    print(f"Successfully created Agent '{agent.name}' (ID: {agent.id})")
    print(f"Successfully created Goal '{goal.title}' (ID: {goal.id})")
    
    await r.aclose()

if __name__ == "__main__":
    asyncio.run(main())
