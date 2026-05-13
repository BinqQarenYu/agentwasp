"""Command palette API — returns models + jobs for frontend command palette."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/commands")
async def get_commands(request: Request):
    """Return available models and scheduler jobs for the command palette."""
    mm = request.app.state.model_manager
    scheduler = request.app.state.scheduler

    models = []
    try:
        status = mm.get_status()
        active = status.get("active_model", "")
        providers = status.get("providers", {})
        for provider_name, pinfo in providers.items():
            for m in (pinfo.get("models") or []):
                models.append({
                    "name": m,
                    "provider": provider_name,
                    "active": m == active,
                })
    except Exception:
        pass

    jobs = []
    try:
        job_list = scheduler.list_jobs() if scheduler else []
        jobs = [j.get("name", str(j)) if isinstance(j, dict) else str(j) for j in job_list]
    except Exception:
        pass

    return JSONResponse({"models": models, "jobs": jobs})
