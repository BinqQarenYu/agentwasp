import json
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

def _get_graph_path() -> Path:
    # 1. Look in containers/agent-core/src/graphify-out/graph.json
    p1 = Path(__file__).resolve().parent.parent.parent / "src" / "graphify-out" / "graph.json"
    if p1.exists():
        return p1
    # 2. Look in root graphify-out
    p2 = Path(__file__).resolve().parent.parent.parent / "graphify-out" / "graph.json"
    if p2.exists():
        return p2
    # 3. Docker container /app path fallback
    p3 = Path("/app/graphify-out/graph.json")
    if p3.exists():
        return p3
    return p1

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def view_workspaces(request: Request):
    graph_path = _get_graph_path()
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

@router.get("/visual", response_class=HTMLResponse)
async def view_visual_graph(request: Request):
    return request.app.state.templates.TemplateResponse(request, "workspaces_visual.html", {})

@router.get("/graph.json")
async def get_graph_json():
    graph_path = _get_graph_path()
    if graph_path.exists():
        try:
            with open(graph_path, "r", encoding="utf-8") as f:
                return JSONResponse(json.load(f))
        except Exception:
            pass
    return JSONResponse({"nodes": [], "edges": []})
