"""Tests for PID verification before signalling.

`kiterouter stop` must never SIGTERM a process it cannot prove is KiteRouter:
a recycled PID could otherwise belong to OmniRoute or anything else.
"""
from __future__ import annotations

import argparse
import os
from subprocess import CompletedProcess

import pytest

from kiterouter import cli


@pytest.fixture
def pid_file(tmp_path, monkeypatch):
    path = tmp_path / "kiterouter.pid"
    monkeypatch.setattr(cli, "PID_FILE", path)
    return path


def fake_ps(command: str | None, returncode: int = 0):
    """Stub `ps -p <pid> -o command=`."""
    def _run(cmd, **kwargs):
        if command is None:
            return CompletedProcess(cmd, returncode, "", "no such process")
        return CompletedProcess(cmd, 0, command + "\n", "")

    return _run


# ── pid_command ──────────────────────────────────────────────────────────────

def test_pid_command_returns_the_command_line(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", fake_ps("/usr/bin/python -m uvicorn kiterouter.server:app"))
    assert cli.pid_command(4242) == "/usr/bin/python -m uvicorn kiterouter.server:app"


def test_pid_command_returns_none_for_a_dead_pid(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", fake_ps(None, returncode=1))
    assert cli.pid_command(4242) is None


def test_pid_command_returns_none_when_ps_is_unavailable(monkeypatch):
    def boom(cmd, **kwargs):
        raise FileNotFoundError("ps not found")

    monkeypatch.setattr(cli.subprocess, "run", boom)
    assert cli.pid_command(4242) is None


# ── is_kiterouter_process ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/python3 -m uvicorn kiterouter.server:app --host 127.0.0.1 --port 3001",
        "/Users/x/.venv/bin/python3 -m uvicorn kiterouter.server:app",
        "kiterouter start --daemon",
    ],
)
def test_a_real_gateway_process_is_recognised(monkeypatch, command):
    monkeypatch.setattr(cli.subprocess, "run", fake_ps(command))
    assert cli.is_kiterouter_process(4242) is True


@pytest.mark.parametrize(
    "command",
    [
        "node /Users/x/.local/bin/omniroute",
        "python3 -m uvicorn someotherapp.server:app",
        "/bin/zsh",
    ],
)
def test_an_unrelated_process_is_not_recognised(monkeypatch, command):
    """This is the case that could have taken down OmniRoute."""
    monkeypatch.setattr(cli.subprocess, "run", fake_ps(command))
    assert cli.is_kiterouter_process(4242) is False


def test_an_unreadable_command_is_not_treated_as_ours(monkeypatch):
    """Fail closed: refuse rather than guess when the command cannot be read."""
    monkeypatch.setattr(cli.subprocess, "run", fake_ps(None, returncode=1))
    assert cli.is_kiterouter_process(4242) is False


# ── stop ─────────────────────────────────────────────────────────────────────

def test_stop_refuses_to_signal_an_unverified_pid(pid_file, monkeypatch, capsys):
    pid_file.write_text("4242")
    monkeypatch.setattr(cli, "get_running_pid", lambda: 4242)
    monkeypatch.setattr(cli.subprocess, "run", fake_ps("node /Users/x/.local/bin/omniroute"))

    signalled = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    cli.cmd_stop(argparse.Namespace())

    assert signalled == [], "nothing may be signalled when the PID is not ours"
    out = capsys.readouterr().out
    assert "Refusing to stop" in out
    assert "omniroute" in out, "the refusal should show what the PID actually is"
    assert pid_file.exists(), "the stale PID file is left for the operator to inspect"


def test_stop_signals_a_verified_pid(pid_file, monkeypatch, capsys):
    pid_file.write_text("4242")
    monkeypatch.setattr(cli, "get_running_pid", lambda: 4242)
    monkeypatch.setattr(cli.subprocess, "run", fake_ps("python3 -m uvicorn kiterouter.server:app"))

    signalled = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)

    cli.cmd_stop(argparse.Namespace())

    assert signalled == [(4242, cli.signal.SIGTERM)]
    assert "✅ KiteRouter stopped." in capsys.readouterr().out
    assert not pid_file.exists(), "the PID file should be cleared after stopping"


def test_stop_with_nothing_running_is_a_no_op(pid_file, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_running_pid", lambda: None)
    cli.cmd_stop(argparse.Namespace())
    assert "not running" in capsys.readouterr().out


# ── status ───────────────────────────────────────────────────────────────────

def test_status_flags_a_stale_pid_file(pid_file, monkeypatch, capsys):
    pid_file.write_text("4242")
    monkeypatch.setattr(cli, "get_running_pid", lambda: 4242)
    monkeypatch.setattr(cli.subprocess, "run", fake_ps("node /Users/x/.local/bin/omniroute"))

    cli.cmd_status(argparse.Namespace())

    out = capsys.readouterr().out
    assert "not KiteRouter" in out
    assert "omniroute" in out


def test_get_running_pid_clears_a_dead_pid(pid_file, monkeypatch):
    pid_file.write_text("999999")

    def dead(pid, sig):
        raise OSError("No such process")

    monkeypatch.setattr(cli.os, "kill", dead)
    assert cli.get_running_pid() is None
    assert not pid_file.exists()


def test_get_running_pid_ignores_garbage(pid_file, monkeypatch):
    pid_file.write_text("not-a-pid")
    monkeypatch.setattr(cli.os, "kill", lambda *a: None)
    assert cli.get_running_pid() is None
    assert not pid_file.exists()
