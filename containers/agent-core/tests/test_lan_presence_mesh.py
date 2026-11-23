import json
import pytest
from starlette.testclient import TestClient
from fastapi import FastAPI
from src.dashboard.routes.mesh import router as mesh_router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(mesh_router, prefix="/mesh")
    return TestClient(app)


def test_mesh_api_lan_endpoint(client):
    res = client.get("/mesh/api/lan")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "fleet_devices" in data
    assert "subnet" in data
    assert isinstance(data["fleet_devices"], list)


def test_mesh_api_state_includes_lan_fleet(client):
    res = client.get("/mesh/api/state")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "lan_fleet" in data
    assert "total_nodes" in data["lan_fleet"]
    assert "nodes" in data["lan_fleet"]
