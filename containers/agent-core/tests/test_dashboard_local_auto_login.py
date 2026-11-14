from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

_CORE_ROOT = Path(__file__).parent.parent
if str(_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORE_ROOT))

os.environ.setdefault("MEDIA_SIGNING_DEBUG", "true")

from src.dashboard.routes import auth_routes
from src.dashboard.auth import hash_password


class _DbSession:
    def __init__(self, user):
        self.user = user

    async def execute(self, _query):
        return SimpleNamespace(scalar_one_or_none=lambda: self.user)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def _request(client_host: str, host_header: str = "127.0.0.1:8080") -> Request:
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/login",
        "raw_path": b"/login",
        "query_string": b"",
        "headers": [(b"host", host_header.encode())],
        "client": (client_host, 43210),
        "server": ("127.0.0.1", 8080),
    })


@pytest.mark.asyncio
async def test_local_auto_login_uses_password_verification_and_normal_session(monkeypatch):
    user = SimpleNamespace(
        id="admin-id",
        username="preview-admin",
        password_hash=hash_password("test-only-password"),
    )
    monkeypatch.setenv("DASHBOARD_LOCAL_AUTO_LOGIN", "true")
    monkeypatch.setenv("DASHBOARD_USER", user.username)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-only-password")
    monkeypatch.setattr(auth_routes, "async_session", lambda: _DbSession(user))
    monkeypatch.setattr(auth_routes, "create_session", _fake_create_session)
    monkeypatch.setattr(auth_routes, "_audit", _fake_audit)

    response = await auth_routes._try_local_auto_login(_request("127.0.0.1"))

    assert response is not None
    assert response.status_code == 303
    assert response.headers["location"] == "/overview"
    assert "session=session-token" in response.headers["set-cookie"]
    assert "httponly" in response.headers["set-cookie"].lower()


@pytest.mark.asyncio
async def test_local_auto_login_is_rejected_for_non_loopback_or_wrong_password(monkeypatch):
    user = SimpleNamespace(
        id="admin-id",
        username="preview-admin",
        password_hash=hash_password("test-only-password"),
    )
    monkeypatch.setenv("DASHBOARD_LOCAL_AUTO_LOGIN", "true")
    monkeypatch.setenv("DASHBOARD_USER", user.username)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-only-password")
    monkeypatch.setattr(auth_routes, "async_session", lambda: _DbSession(user))
    monkeypatch.setattr(auth_routes, "create_session", _fake_create_session)
    monkeypatch.setattr(auth_routes, "_audit", _fake_audit)

    assert await auth_routes._try_local_auto_login(_request("192.0.2.10")) is None
    assert await auth_routes._try_local_auto_login(
        _request("127.0.0.1", "attacker.example")
    ) is None

    monkeypatch.setenv("DASHBOARD_PASSWORD", "incorrect-test-password")
    assert await auth_routes._try_local_auto_login(_request("127.0.0.1")) is None


@pytest.mark.asyncio
async def test_local_auto_login_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("DASHBOARD_LOCAL_AUTO_LOGIN", raising=False)
    assert await auth_routes._try_local_auto_login(_request("127.0.0.1")) is None


async def _fake_create_session(_request, _user_id):
    return "session-token"


async def _fake_audit(*_args, **_kwargs):
    return None
