"""Tests for the self-update path.

Nothing here may touch the real repository or replace the test process, so
``updater._git`` and ``updater.schedule_restart`` are always stubbed.
"""
from __future__ import annotations

import subprocess

import pytest

from kiterouter import updater


@pytest.fixture(autouse=True)
def _reset_fetch_cache():
    updater._fetch_cache["at"] = 0.0
    yield
    updater._fetch_cache["at"] = 0.0


@pytest.fixture
def no_restart(monkeypatch):
    """Fail loudly if a test ever schedules a real process replacement."""
    calls = []
    monkeypatch.setattr(updater, "schedule_restart", lambda *a, **k: calls.append(True))
    return calls


def fake_git(responses: dict, default=("", 0, "")):
    """Stub ``_git``; responses maps 'arg arg' to (stdout, returncode, stderr)."""
    calls = []

    def _fake(*args, timeout=updater.GIT_TIMEOUT_SECONDS):
        key = " ".join(args)
        calls.append(key)
        stdout, rc, stderr = responses.get(key, default)
        return subprocess.CompletedProcess(["git", *args], rc, stdout, stderr)

    return _fake, calls


CLEAN_REPO = {
    "rev-parse --is-inside-work-tree": ("true", 0, ""),
    "rev-parse --abbrev-ref HEAD": ("main", 0, ""),
    "rev-parse --verify --quiet origin/main": ("deadbeef", 0, ""),
    "rev-parse --short HEAD": ("abc1234", 0, ""),
    "rev-parse --short origin/main": ("def5678", 0, ""),
    "status --porcelain": ("", 0, ""),
    "rev-list --count HEAD..origin/main": ("0", 0, ""),
    "rev-list --count origin/main..HEAD": ("0", 0, ""),
    "fetch --quiet --no-tags origin": ("", 0, ""),
}


def test_status_reports_a_clean_up_to_date_checkout(monkeypatch):
    fake, _ = fake_git(CLEAN_REPO)
    monkeypatch.setattr(updater, "_git", fake)

    info = updater.status()

    assert info["available"] is True
    assert info["branch"] == "main"
    assert info["upstream"] == "origin/main"
    assert info["behind"] == 0 and info["ahead"] == 0
    assert info["dirty"] is False
    assert info["local_sha"] == "abc1234"
    assert info["can_update"] is False


def test_status_counts_commits_behind_and_local_changes(monkeypatch):
    responses = dict(CLEAN_REPO)
    responses["rev-list --count HEAD..origin/main"] = ("3", 0, "")
    responses["status --porcelain"] = (" M a.py\n?? b.py\n", 0, "")
    fake, _ = fake_git(responses)
    monkeypatch.setattr(updater, "_git", fake)

    info = updater.status()

    assert info["behind"] == 3
    assert info["changed_files"] == 2
    assert info["dirty"] is True
    assert info["can_update"] is True


def test_status_outside_a_repo_is_reported_not_raised(monkeypatch):
    fake, _ = fake_git({"rev-parse --is-inside-work-tree": ("", 1, "fatal: not a repo")})
    monkeypatch.setattr(updater, "_git", fake)

    info = updater.status()

    assert info["available"] is False
    assert info["can_update"] is False
    assert "git checkout" in info["fetch_error"]


def test_status_does_not_fetch_unless_asked(monkeypatch):
    fake, calls = fake_git(CLEAN_REPO)
    monkeypatch.setattr(updater, "_git", fake)

    updater.status(fetch=False)

    assert not any(c.startswith("fetch") for c in calls)


def test_fetch_is_throttled_by_the_ttl(monkeypatch):
    fake, calls = fake_git(CLEAN_REPO)
    monkeypatch.setattr(updater, "_git", fake)

    updater.status(fetch=True)
    updater.status(fetch=True)

    assert sum(1 for c in calls if c.startswith("fetch")) == 1, "fetch should be cached"


def test_force_fetch_ignores_the_ttl(monkeypatch):
    fake, calls = fake_git(CLEAN_REPO)
    monkeypatch.setattr(updater, "_git", fake)

    updater.status(fetch=True)
    updater.status(fetch=True, force_fetch=True)

    assert sum(1 for c in calls if c.startswith("fetch")) == 2


def test_a_failed_fetch_is_surfaced_but_keeps_local_numbers(monkeypatch):
    responses = dict(CLEAN_REPO)
    responses["fetch --quiet --no-tags origin"] = ("", 1, "Could not resolve host")
    responses["rev-list --count HEAD..origin/main"] = ("2", 0, "")
    fake, _ = fake_git(responses)
    monkeypatch.setattr(updater, "_git", fake)

    info = updater.status(fetch=True)

    assert "Could not resolve host" in info["fetch_error"]
    assert info["behind"] == 2, "local comparison should still be reported"


# ── applying an update ───────────────────────────────────────────────────────

def test_apply_update_reports_up_to_date_without_pulling(monkeypatch):
    fake, calls = fake_git(CLEAN_REPO)
    monkeypatch.setattr(updater, "_git", fake)

    result = updater.apply_update()

    assert result["status"] == "up_to_date"
    assert not any(c.startswith("pull") for c in calls), "nothing to pull, nothing to run"


def test_apply_update_refuses_when_local_is_ahead(monkeypatch):
    responses = dict(CLEAN_REPO)
    responses["rev-list --count HEAD..origin/main"] = ("0", 0, "")
    responses["rev-list --count origin/main..HEAD"] = ("2", 0, "")
    fake, calls = fake_git(responses)
    monkeypatch.setattr(updater, "_git", fake)

    result = updater.apply_update()

    assert result["status"] == "error"
    assert "ahead" in result["message"]
    assert not any(c.startswith("pull") for c in calls)


def test_apply_update_reports_a_failed_pull(monkeypatch):
    responses = dict(CLEAN_REPO)
    responses["rev-list --count HEAD..origin/main"] = ("1", 0, "")
    responses["pull --ff-only origin main"] = ("", 1, "error: Your local changes would be overwritten")
    fake, _ = fake_git(responses)
    monkeypatch.setattr(updater, "_git", fake)

    result = updater.apply_update()

    assert result["status"] == "error"
    assert "would be overwritten" in result["message"]


def test_apply_update_pulls_then_syncs_and_reports_the_new_sha(monkeypatch):
    responses = dict(CLEAN_REPO)
    responses["rev-list --count HEAD..origin/main"] = ("1", 0, "")
    responses["pull --ff-only origin main"] = ("Fast-forward", 0, "")
    fake, calls = fake_git(responses)
    monkeypatch.setattr(updater, "_git", fake)
    monkeypatch.setattr(updater, "validate_import", lambda: (True, ""))
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/usr/bin/uv")

    synced = []
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda cmd, **kw: (synced.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")),
    )

    result = updater.apply_update()

    assert result["status"] == "updated"
    assert "abc1234" in result["message"]
    assert any("uv" in c[0] for c in synced), "dependencies should be synced"


def test_apply_update_does_not_reload_code_that_fails_to_import(monkeypatch, no_restart):
    responses = dict(CLEAN_REPO)
    responses["rev-list --count HEAD..origin/main"] = ("1", 0, "")
    responses["pull --ff-only origin main"] = ("Fast-forward", 0, "")
    fake, _ = fake_git(responses)
    monkeypatch.setattr(updater, "_git", fake)
    monkeypatch.setattr(updater.shutil, "which", lambda _: None)
    monkeypatch.setattr(updater, "validate_import", lambda: (False, "ImportError: boom"))

    result = updater.run_update(restart=True)

    assert result["status"] == "error"
    assert "failed to import" in result["message"]
    assert no_restart == [], "must not restart into code that does not import"


def test_run_update_schedules_a_restart_when_asked(monkeypatch, no_restart):
    responses = dict(CLEAN_REPO)
    responses["rev-list --count HEAD..origin/main"] = ("1", 0, "")
    responses["pull --ff-only origin main"] = ("Fast-forward", 0, "")
    fake, _ = fake_git(responses)
    monkeypatch.setattr(updater, "_git", fake)
    monkeypatch.setattr(updater, "validate_import", lambda: (True, ""))
    monkeypatch.setattr(updater.shutil, "which", lambda _: None)

    result = updater.run_update(restart=True)

    assert result["status"] == "updated"
    assert result["restart_scheduled"] is True
    assert no_restart == [True]


def test_run_update_without_restart_leaves_the_process_alone(monkeypatch, no_restart):
    responses = dict(CLEAN_REPO)
    responses["rev-list --count HEAD..origin/main"] = ("1", 0, "")
    responses["pull --ff-only origin main"] = ("Fast-forward", 0, "")
    fake, _ = fake_git(responses)
    monkeypatch.setattr(updater, "_git", fake)
    monkeypatch.setattr(updater, "validate_import", lambda: (True, ""))
    monkeypatch.setattr(updater.shutil, "which", lambda _: None)

    result = updater.run_update(restart=False)

    assert result["status"] == "updated"
    assert result["restart_scheduled"] is False
    assert no_restart == []


def test_restart_command_mirrors_how_the_daemon_is_launched(monkeypatch):
    from kiterouter.config import KiteConfig

    monkeypatch.setattr(KiteConfig, "load", classmethod(lambda cls: KiteConfig(host="127.0.0.1", port=3001)))
    cmd = updater.restart_command()

    assert cmd[1:] == ["-m", "uvicorn", "kiterouter.server:app", "--host", "127.0.0.1", "--port", "3001"]
    assert cmd[0].endswith("python3") or "python" in cmd[0]


def test_validate_import_uses_a_subprocess(monkeypatch):
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "ImportError: nope"),
    )
    ok, detail = updater.validate_import()
    assert ok is False
    assert "nope" in detail


# ── endpoints ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_status_endpoint(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from kiterouter import server
    from kiterouter.server import app

    monkeypatch.setattr(
        server.updater,
        "status",
        lambda fetch=False, force_fetch=False: {
            "available": True, "behind": 4, "ahead": 0, "dirty": True,
            "changed_files": 1, "branch": "main", "upstream": "origin/main",
            "local_sha": "aaa1111", "remote_sha": "bbb2222",
            "fetched_at": 1700000000, "fetch_error": None, "can_update": True,
        },
    )
    server._update_status_cache["at"] = 0.0
    server._update_status_cache["data"] = None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/update/status?fetch=true")

    assert resp.status_code == 200
    body = resp.json()
    assert body["behind"] == 4
    assert body["dirty"] is True


@pytest.mark.asyncio
async def test_update_endpoint_reports_the_result(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from kiterouter import server
    from kiterouter.server import app

    monkeypatch.setattr(
        server.updater,
        "run_update",
        lambda restart=True: {"status": "up_to_date", "message": "Already up to date."},
    )
    server._update_status_cache["at"] = 0.0
    server._update_status_cache["data"] = None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/update", json={"restart": True})

    assert resp.status_code == 200
    assert resp.json()["status"] == "up_to_date"


@pytest.mark.asyncio
async def test_update_endpoint_surfaces_failure(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from kiterouter import server
    from kiterouter.server import app

    monkeypatch.setattr(
        server.updater,
        "run_update",
        lambda restart=True: {"status": "error", "message": "git pull failed: conflict"},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/update", json={"restart": True})

    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
