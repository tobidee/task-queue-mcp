"""
Tests for retention: sweep_archive / cleanup_locks / the POST /tasks/{id}/archive
control route. The invariants pinned here:

- Only TERMINAL tasks ever leave the main directory, and only when their last
  activity (max history timestamp, else created) is older than the configured
  days — open work never ages out of placement, however old (vikunja#395,
  applied to placement).
- Archiving is placement, not state: the file moves unchanged, and every
  existing mutation route keeps refusing archived tasks (regression-pinned via
  the approve route).
- days=0 disables the sweep entirely.
- Lock files follow their task: archived tasks lose theirs, orphans older than
  the age guard are collected, live and young ones survive.
"""

import importlib
import os
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from starlette.testclient import TestClient

from src.tools.queue import (
    archive_task_handler,
    cleanup_locks,
    submit_task_handler,
    sweep_archive,
)

SECRET = "test-secret-value"
AUTH = {"X-Task-Queue-Secret": SECRET}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Reload src.server with QUEUE_DIR -> tmp and a known API secret."""
    import src.server as srv

    importlib.reload(srv)
    monkeypatch.setattr(srv, "QUEUE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_QUEUE_API_SECRET", SECRET)
    return srv, tmp_path


@pytest.fixture
def client(env):
    srv, _ = env
    with TestClient(srv.mcp.http_app()) as c:
        yield c


def _seed(tmp_path, status="submitted", age_days=0):
    """Submit a task, then rewrite its status and push created + every history
    timestamp age_days into the past — the same direct-YAML shaping the control
    API tests use."""
    r = submit_task_handler(
        source_agent="research",
        target_agent="developer",
        task_type="build",
        summary="s",
        description="d",
        queue_dir=str(tmp_path),
    )
    path = tmp_path / r["filename"]
    with open(path) as f:
        data = yaml.safe_load(f)
    data["status"] = status
    if age_days:
        old = datetime.now(UTC) - timedelta(days=age_days)
        data["created"] = old
        for entry in data.get("history") or []:
            entry["timestamp"] = old
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    # submit_task_handler writes fresh files without taking the per-task lock;
    # a lock file only appears once something mutates the task. Create it here
    # so lock-lifecycle assertions reflect a task that has been touched.
    lock = _lock_path(tmp_path, r["task_id"])
    lock.parent.mkdir(exist_ok=True)
    lock.touch()
    return r["task_id"], path


def _lock_path(tmp_path, task_id):
    return tmp_path / ".locks" / f"{task_id}.lock"


# ── sweep_archive ──────────────────────────────────────────────────────────


def test_sweep_moves_old_terminal_task_and_removes_lock(tmp_path):
    task_id, path = _seed(tmp_path, status="completed", age_days=10)
    assert _lock_path(tmp_path, task_id).exists()  # created by _seed

    moved = sweep_archive(str(tmp_path), days=7)

    assert moved == 1
    assert not path.exists()
    assert (tmp_path / "archive" / path.name).exists()
    assert not _lock_path(tmp_path, task_id).exists()


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_sweep_covers_all_terminal_statuses(tmp_path, status):
    _, path = _seed(tmp_path, status=status, age_days=10)
    assert sweep_archive(str(tmp_path), days=7) == 1
    assert (tmp_path / "archive" / path.name).exists()


def test_sweep_leaves_young_terminal_task(tmp_path):
    _, path = _seed(tmp_path, status="completed", age_days=3)
    assert sweep_archive(str(tmp_path), days=7) == 0
    assert path.exists()


@pytest.mark.parametrize(
    "status", ["submitted", "approved", "in-progress", "parked", "routing-failed"]
)
def test_sweep_never_touches_open_work_however_old(tmp_path, status):
    _, path = _seed(tmp_path, status=status, age_days=400)
    assert sweep_archive(str(tmp_path), days=7) == 0
    assert path.exists()
    assert not (tmp_path / "archive").exists()


def test_sweep_days_zero_is_a_noop(tmp_path):
    _, path = _seed(tmp_path, status="completed", age_days=400)
    assert sweep_archive(str(tmp_path), days=0) == 0
    assert path.exists()


def test_sweep_uses_last_activity_not_created(tmp_path):
    """A task created long ago but touched recently stays: max(history) wins."""
    task_id, path = _seed(tmp_path, status="completed", age_days=30)
    with open(path) as f:
        data = yaml.safe_load(f)
    data["history"].append(
        {
            "timestamp": datetime.now(UTC) - timedelta(days=1),
            "status": "completed",
            "actor": "operator",
            "note": "late sweep note",
        }
    )
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    assert sweep_archive(str(tmp_path), days=7) == 0
    assert path.exists()


def test_sweep_tolerates_naive_timestamps(tmp_path):
    """Files shaped by other direct-YAML writers may carry naive datetimes —
    the sweep must compare, not crash."""
    _, path = _seed(tmp_path, status="completed")
    with open(path) as f:
        data = yaml.safe_load(f)
    naive_old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)
    data["created"] = naive_old
    for entry in data["history"]:
        entry["timestamp"] = naive_old
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    assert sweep_archive(str(tmp_path), days=7) == 1


# ── cleanup_locks ──────────────────────────────────────────────────────────


def test_cleanup_locks_removes_only_old_orphans(tmp_path):
    task_id, _ = _seed(tmp_path, status="submitted")
    live_lock = _lock_path(tmp_path, task_id)
    assert live_lock.exists()

    lock_dir = tmp_path / ".locks"
    old_orphan = lock_dir / "00000000-0000-0000-0000-000000000000.lock"
    old_orphan.touch()
    two_days_ago = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(old_orphan, (two_days_ago, two_days_ago))
    young_orphan = lock_dir / "11111111-1111-1111-1111-111111111111.lock"
    young_orphan.touch()

    removed = cleanup_locks(str(tmp_path))

    assert removed == 1
    assert not old_orphan.exists()
    assert young_orphan.exists()  # age guard: may belong to a submit in flight
    assert live_lock.exists()  # its task still lives in the main directory


# ── archive_task_handler / HTTP route ──────────────────────────────────────


def test_archive_handler_refuses_non_terminal(tmp_path):
    task_id, path = _seed(tmp_path, status="in-progress")
    result = archive_task_handler(task_id=task_id, actor="operator", queue_dir=str(tmp_path))
    assert result["ok"] is False
    assert "non-terminal" in result["error"]
    assert path.exists()


def test_http_archive_route_moves_terminal_task(client, env):
    _, tmp_path = env
    task_id, path = _seed(tmp_path, status="completed")

    r = client.post(f"/tasks/{task_id}/archive", headers=AUTH)

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert not path.exists()
    assert (tmp_path / "archive" / path.name).exists()
    assert not _lock_path(tmp_path, task_id).exists()

    # Idempotence surface: a second archive is a clean refusal, not a 500.
    r2 = client.post(f"/tasks/{task_id}/archive", headers=AUTH)
    assert r2.status_code == 400
    assert "already archived" in r2.json()["error"]


def test_http_archive_route_requires_secret(client, env):
    _, tmp_path = env
    task_id, path = _seed(tmp_path, status="completed")

    assert client.post(f"/tasks/{task_id}/archive").status_code == 401
    assert client.post(
        f"/tasks/{task_id}/archive", headers={"X-Task-Queue-Secret": "wrong"}
    ).status_code == 401
    assert path.exists()


def test_http_archive_route_refuses_non_terminal(client, env):
    _, tmp_path = env
    task_id, _ = _seed(tmp_path, status="submitted")
    r = client.post(f"/tasks/{task_id}/archive", headers=AUTH)
    assert r.status_code == 400


def test_archived_task_still_readable_but_immutable(client, env):
    """Regression pin: get_task finds archived tasks, approve refuses them."""
    _, tmp_path = env
    task_id, _ = _seed(tmp_path, status="completed")
    assert client.post(f"/tasks/{task_id}/archive", headers=AUTH).status_code == 200

    r_get = client.get(f"/tasks/{task_id}", headers=AUTH)
    assert r_get.status_code == 200
    assert r_get.json()["id"] == task_id

    r_approve = client.post(f"/tasks/{task_id}/approve", headers=AUTH)
    assert r_approve.status_code == 400


def test_archive_days_env_parsing(monkeypatch):
    import src.server as srv

    monkeypatch.setenv("TASK_QUEUE_ARCHIVE_DAYS", "14")
    assert importlib.reload(srv).ARCHIVE_DAYS == 14

    monkeypatch.setenv("TASK_QUEUE_ARCHIVE_DAYS", "")
    assert importlib.reload(srv).ARCHIVE_DAYS == 0

    monkeypatch.setenv("TASK_QUEUE_ARCHIVE_DAYS", "vierzehn")
    with pytest.raises(SystemExit):
        importlib.reload(srv)
    # Leave the module in a sane state for later tests.
    monkeypatch.delenv("TASK_QUEUE_ARCHIVE_DAYS")
    importlib.reload(srv)
