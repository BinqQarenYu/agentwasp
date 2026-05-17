import asyncio
import json
import sys

sys.path.insert(0, "/app")
from src.goal_orchestrator.store import list_goals

async def main():
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url('redis://agent-redis:6379/0', decode_responses=True)
        goals = await list_goals(r)
        
        # Sort by created_at
        goals.sort(key=lambda g: g.created_at if g.created_at else "")
        
        for idx, g in enumerate(goals):
            print(f"Goal {idx+1}: {g.id} | {g.state.value} | {g.objective}")
            if g.error:
                print(f"   -> ERROR: {g.error}")
            failed_tasks = g.task_graph.get_failed_tasks() if getattr(g, 'task_graph', None) else []
            for t in failed_tasks:
                print(f"      -> TASK ERROR ({t.skill_name}): {t.error}")
        
        await r.aclose()
    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    asyncio.run(main())
