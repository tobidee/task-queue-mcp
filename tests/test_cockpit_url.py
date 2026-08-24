"""
cockpit_url in tool/control responses: when TASK_QUEUE_COCKPIT_URL is set,
submit/get/list answers carry a ready-made deep link so agents can always
hand the human the direct approval/status URL. Unset = field absent (additive,
no client has to know it). Trailing slashes must not double.
"""

import importlib

import pytest
from starlette.testclient import TestClient

SECRET = "test-secret-value"
AUTH = {"X-Task-Queue-Secret": SECRET}
COCKPIT = "https://apps.example.test/cockpit/"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_QUEUE_COCKPIT_URL", COCKPIT)
    import src.server as srv

    importlib.reload(srv)
    monkeypatch.setattr(srv, "QUEUE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_QUEUE_API_SECRET", SECRET)
    yield srv, tmp_path
    monkeypatch.delenv("TASK_QUEUE_COCKPIT_URL")
    importlib.reload(srv)


@pytest.fixture
def client(env):
    srv, _ = env
    with TestClient(srv.mcp.http_app()) as c:
        yield c


def _submit(client):
    r = client.post("/tasks/submit", headers=AUTH, json={
        "source_agent": "doctor", "target_agent": "developer",
        "task_type": "fix", "summary": "s", "description": "d",
    })
    assert r.status_code == 200
    return r.json()


def test_submit_returns_deep_link(client):
    data = _submit(client)
    assert data["cockpit_url"] == f"https://apps.example.test/cockpit/#task={data['task_id']}"


def test_get_returns_deep_link(client):
    data = _submit(client)
    r = client.get(f"/tasks/{data['task_id']}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["cockpit_url"] == data["cockpit_url"]


def test_unset_env_means_no_field(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_QUEUE_COCKPIT_URL", raising=False)
    import src.server as srv

    importlib.reload(srv)
    monkeypatch.setattr(srv, "QUEUE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_QUEUE_API_SECRET", SECRET)
    with TestClient(srv.mcp.http_app()) as c:
        r = c.post("/tasks/submit", headers=AUTH, json={
            "source_agent": "doctor", "target_agent": "developer",
            "task_type": "fix", "summary": "s", "description": "d",
        })
        assert r.status_code == 200
        assert "cockpit_url" not in r.json()


def test_error_responses_stay_untouched(client):
    r = client.get("/tasks/00000000-0000-0000-0000-000000000000", headers=AUTH)
    assert r.status_code == 404
    assert "cockpit_url" not in r.json()
