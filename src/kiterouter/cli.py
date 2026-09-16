"""KiteRouter CLI and on-the-fly update manager."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import httpx

from kiterouter.config import CONFIG_DIR, KiteConfig

PID_FILE = CONFIG_DIR / "kiterouter.pid"
REPO_DIR = Path(__file__).resolve().parent.parent.parent


def get_running_pid() -> int | None:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, OSError):
            PID_FILE.unlink(missing_ok=True)
    return None


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
    """Zero-build on-the-fly update like Hermes Agent."""
    print("🪁 Checking for updates (zero-build git pull)...")
    try:
        # 1. Pull latest commits
        res = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=str(REPO_DIR),
            capture_output=True,
            text=True,
        )
        print(res.stdout.strip() or res.stderr.strip())

        # 2. Update editable dependencies with uv
        print("Syncing dependencies with uv...")
        subprocess.run(
            ["uv", "sync"],
            cwd=str(REPO_DIR),
            check=True,
            capture_output=True,
        )

        # 3. Reload running daemon if active
        pid = get_running_pid()
        if pid:
            print(f"Hot-reloading running daemon (PID {pid})...")
            os.kill(pid, signal.SIGHUP)
            print("✅ KiteRouter successfully updated and hot-reloaded on the fly!")
        else:
            print("✅ KiteRouter updated successfully!")
    except Exception as e:
        print(f"❌ Update failed: {e}")


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
