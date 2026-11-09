"""Dashboard route for Altair AI / Grok Bot Mesh Visualizer."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def mesh_cockpit_view(request: Request):
    return request.app.state.templates.TemplateResponse(request, "mesh_cockpit.html", {
        "title": "GROK BOT MESH — Altair One-Person-Company",
    })
