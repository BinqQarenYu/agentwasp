import json
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def view_workspaces(request: Request):
    graph_path = Path("/app/graphify-out/graph.json")
    graph_meta = None
    graph_size = 0
    if graph_path.exists():
        graph_size = round(graph_path.stat().st_size / (1024 * 1024), 2)
        try:
            with open(graph_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                graph_meta = data.get("metadata")
        except Exception:
            pass
            
    return request.app.state.templates.TemplateResponse(request, "workspaces.html", {
        "graph_meta": graph_meta,
        "graph_size": graph_size
    })

from fastapi.responses import HTMLResponse, JSONResponse

@router.get("/visual", response_class=HTMLResponse)
async def view_visual_graph(request: Request):
    return request.app.state.templates.TemplateResponse(request, "workspaces_visual.html", {})

@router.get("/graph.json")
async def get_graph_json():
    graph_path = Path("/app/graphify-out/graph.json")
    if graph_path.exists():
        try:
            with open(graph_path, "r", encoding="utf-8") as f:
                return JSONResponse(json.load(f))
        except Exception:
            pass
    return JSONResponse({"nodes": [], "edges": []})
