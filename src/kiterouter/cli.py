"""KiteRouter CLI and on-the-fly update manager."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from kiterouter.config import CONFIG_DIR, KiteConfig

PID_FILE = CONFIG_DIR / "kiterouter.pid"
REPO_DIR = Path(__file__).resolve().parent.parent.parent


def get_running_pid() -> int | None:
    """PID recorded in the PID file, if something is alive there."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, OSError):
            PID_FILE.unlink(missing_ok=True)
    return None


def pid_command(pid: int) -> Optional[str]:
    """Command line for a PID, or None when it cannot be read."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_kiterouter_process(pid: int) -> bool:
    """True only when this PID really is the gateway.

    A bare liveness check (``os.kill(pid, 0)``) proves *something* is alive at
    that PID, not what it is. If KiteRouter died and the PID was recycled, an
    unverified stop would SIGTERM whatever inherited it — and OmniRoute runs on
    this machine, so that could take the neighbour down.
    """
    command = pid_command(pid)
    return bool(command) and "kiterouter" in command.lower()


def cmd_start(args: argparse.Namespace) -> None:
    pid = get_running_pid()
    if pid:
        print(f"KiteRouter is already running (PID {pid}).")
        return

    config = KiteConfig.load()
    port = args.port or config.port
    host = args.host or config.host

    print(f"🪁 Starting KiteRouter on {host}:{port}...")

    if args.daemon:
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "kiterouter.server:app",
            "--host",
            host,
            "--port",
            str(port),
        ]
        log_file = open(CONFIG_DIR / "kiterouter.log", "a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
            cwd=str(REPO_DIR),
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        print(f"✅ KiteRouter started in background (PID {proc.pid}).")
        print(f"📊 Dashboard available at: http://{host}:{port}/dashboard")
    else:
        import uvicorn
        from kiterouter.server import app
        uvicorn.run(app, host=host, port=port)


def cmd_stop(args: argparse.Namespace) -> None:
    pid = get_running_pid()
    if not pid:
        print("KiteRouter is not running.")
        return

    if not is_kiterouter_process(pid):
        command = pid_command(pid) or "unknown (could not read its command line)"
        print(f"⚠️  Refusing to stop PID {pid}: that process is not KiteRouter.")
        print(f"    Found: {command}")
        print("    Nothing was signalled. If KiteRouter really did die, remove the")
        print(f"    stale PID file yourself: {PID_FILE}")
        return

    print(f"Stopping KiteRouter (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
    except OSError:
        pass
    PID_FILE.unlink(missing_ok=True)
    print("✅ KiteRouter stopped.")


def cmd_status(args: argparse.Namespace) -> None:
    pid = get_running_pid()
    if not pid:
        print("Status: ⭕ Stopped")
        return

    if not is_kiterouter_process(pid):
        command = pid_command(pid) or "unknown (could not read its command line)"
        print(f"Status: ⚠️  PID {pid} is alive but is not KiteRouter — stale PID file.")
        print(f"Found: {command}")
        print(f"Remove it with: rm {PID_FILE}")
        return

    config = KiteConfig.load()
    url = f"http://{config.host}:{config.port}/health"
    try:
        resp = httpx.get(url, timeout=2.0)
        if resp.status_code == 200:
            data = resp.json()
            print(f"Status: 🟢 Running (PID {pid}) on http://{config.host}:{config.port}")
            print(f"RTK Compressor: {'Active' if data.get('rtk_enabled') else 'Disabled'}")
            print(f"Available Providers: {', '.join(data.get('available_providers', []))}")
            metrics = data.get("metrics", {})
            print(f"Total Requests: {metrics.get('total_requests', 0)}")
            print(f"Approx Tokens Saved: {metrics.get('saved_tokens_approx', 0)}")
            return
    except Exception:
        pass

    print(f"Status: 🟡 Process running (PID {pid}) but endpoint not responding yet.")


def cmd_update(args: argparse.Namespace) -> None:
    """Zero-build on-the-fly update.

    When the daemon is running it performs the update itself so the reload
    happens in the right process: ``os.execv`` replaces that process in place and
    keeps its PID. (The previous implementation sent SIGHUP, which nothing
    handles — uvicorn only traps SIGINT/SIGTERM — so it killed the daemon while
    reporting a successful hot-reload.)
    """
    from kiterouter import updater

    config = KiteConfig.load()
    pid = get_running_pid()

    if pid:
        print(f"🪁 Updating via the running gateway (PID {pid})...")
        url = f"http://{config.host}:{config.port}/api/update"
        try:
            resp = httpx.post(url, json={"restart": True}, timeout=600.0)
            result = resp.json()
            print(result.get("message", ""))
            if result.get("status") == "error":
                raise SystemExit(1)
            if result.get("restart_scheduled"):
                print("♻️  Gateway reloading — its PID is preserved, code reloaded.")
            return
        except SystemExit:
            raise
        except Exception as e:
            print(f"⚠️  Could not update through the gateway ({e}); updating files only.")

    print("🪁 Checking for updates (zero-build git pull)...")
    result = updater.apply_update()
    status = result.get("status")
    print(result.get("message", ""))
    if status == "error":
        raise SystemExit(1)
    if status == "updated":
        print("✅ Files updated. No gateway was running, so nothing needed reloading.")


def main() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(prog="kiterouter", description="KiteRouter CLI")
    subparsers = parser.add_subparsers(dest="command")

    start_p = subparsers.add_parser("start", help="Start the KiteRouter gateway")
    start_p.add_argument("--host", default=None, help="Host to bind (default: 127.0.0.1)")
    start_p.add_argument("--port", type=int, default=None, help="Port to bind (default: 3001)")
    start_p.add_argument("-d", "--daemon", action="store_true", help="Run in background daemon mode")

    subparsers.add_parser("stop", help="Stop the KiteRouter gateway")
    subparsers.add_parser("status", help="Check gateway status and metrics")
    subparsers.add_parser("update", help="Update KiteRouter on the fly (git pull + uv sync)")

    args = parser.parse_args()
    if args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "update":
        cmd_update(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
