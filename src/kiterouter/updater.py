"""Self-update: report how far behind the checkout is, and update it in place.

Reloading a detached daemon needs care. The original implementation sent SIGHUP
and reported success, but nothing installs a SIGHUP handler — uvicorn handles
only SIGINT/SIGTERM, and a plain Python process is killed by SIGHUP. So the
daemon died and the message was a lie. Here the process replaces itself with
``os.execv`` instead, which preserves the PID (so the pid file stays valid) and
needs no supervisor. New code is import-checked first, so a bad update reports
an error rather than taking the gateway down.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("kiterouter.updater")

REPO_DIR = Path(__file__).resolve().parent.parent.parent
UPSTREAM = "origin/main"

FETCH_TTL_SECONDS = 300  # don't hit the network on every dashboard poll
GIT_TIMEOUT_SECONDS = 30
SYNC_TIMEOUT_SECONDS = 300
IMPORT_CHECK_TIMEOUT_SECONDS = 60
RESTART_DELAY_SECONDS = 1.0

# Freshness of the last network fetch, shared by the status endpoint.
_fetch_cache: Dict[str, Any] = {"at": 0.0}


class UpdateError(Exception):
    """A user-reportable failure while checking for or applying an update."""


def _git(
    *args: str, timeout: int = GIT_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess:
    """Run git with prompts disabled so a missing credential cannot hang us."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "true")
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def is_git_checkout() -> bool:
    try:
        return _git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    except Exception:
        return False


def _current_branch() -> Optional[str]:
    result = _git("rev-parse", "--abbrev-ref", "HEAD")
    branch = result.stdout.strip()
    return branch or None


def _upstream_ref(branch: Optional[str]) -> Optional[str]:
    """Prefer the branch's own upstream; fall back to origin/main."""
    if branch and branch not in ("HEAD", "main", "master"):
        result = _git("rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    if _git("rev-parse", "--verify", "--quiet", UPSTREAM).returncode == 0:
        return UPSTREAM
    return None


def _count(left: str, right: str) -> int:
    result = _git("rev-list", "--count", f"{left}..{right}")
    try:
        return int(result.stdout.strip())
    except (TypeError, ValueError):
        return 0


def status(fetch: bool = False, force_fetch: bool = False) -> Dict[str, Any]:
    """Report how the checkout compares to its upstream.

    ``fetch`` performs a network fetch, but no more often than
    ``FETCH_TTL_SECONDS`` unless ``force_fetch`` is set, so a 5-second dashboard
    poll does not hammer the remote. ``fetched_at`` always says how old the
    comparison really is.
    """
    info: Dict[str, Any] = {
        "available": False,
        "behind": 0,
        "ahead": 0,
        "dirty": False,
        "changed_files": 0,
        "branch": None,
        "upstream": None,
        "local_sha": None,
        "remote_sha": None,
        "fetched_at": None,
        "fetch_error": None,
        "can_update": False,
    }

    if not is_git_checkout():
        info["fetch_error"] = "not a git checkout"
        return info

    info["available"] = True
    branch = _current_branch()
    upstream = _upstream_ref(branch)
    info["branch"] = branch
    info["upstream"] = upstream
    info["local_sha"] = _git("rev-parse", "--short", "HEAD").stdout.strip() or None

    should_fetch = fetch and (
        force_fetch or (time.time() - _fetch_cache.get("at", 0.0)) > FETCH_TTL_SECONDS
    )
    if should_fetch:
        try:
            result = _git("fetch", "--quiet", "--no-tags", "origin")
            if result.returncode != 0:
                info["fetch_error"] = (result.stderr or result.stdout).strip()[:300]
            else:
                _fetch_cache["at"] = time.time()
        except subprocess.TimeoutExpired:
            info["fetch_error"] = "git fetch timed out"
        except Exception as e:
            info["fetch_error"] = str(e)[:300]

    porcelain = _git("status", "--porcelain").stdout.strip()
    info["changed_files"] = len(porcelain.splitlines()) if porcelain else 0
    info["dirty"] = info["changed_files"] > 0

    if upstream:
        info["behind"] = _count("HEAD", upstream)
        info["ahead"] = _count(upstream, "HEAD")
        remote = _git("rev-parse", "--short", upstream)
        if remote.returncode == 0:
            info["remote_sha"] = remote.stdout.strip() or None

    info["fetched_at"] = int(_fetch_cache["at"]) or None
    info["can_update"] = bool(upstream) and info["behind"] > 0 and info["ahead"] == 0
    return info


def validate_import() -> Tuple[bool, str]:
    """Check the checkout still imports before we replace the running process."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import kiterouter.server"],
            cwd=str(REPO_DIR),
            capture_output=True,
            text=True,
            timeout=IMPORT_CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, "import check timed out"
    except Exception as e:
        return False, str(e)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()[-400:]
    return True, ""


def restart_command() -> List[str]:
    """Re-exec mirroring how the CLI launches the daemon."""
    from kiterouter.config import KiteConfig

    cfg = KiteConfig.load()
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "kiterouter.server:app",
        "--host",
        cfg.host,
        "--port",
        str(cfg.port),
    ]


def schedule_restart(delay: float = RESTART_DELAY_SECONDS) -> None:
    """Replace this process in place once the current response has flushed."""

    def _restart() -> None:
        time.sleep(delay)
        try:
            os.chdir(str(REPO_DIR))
            os.execv(sys.executable, restart_command())
        except Exception as e:  # pragma: no cover - only on a failed exec
            logger.error(
                "Self-restart failed (%s); restart KiteRouter manually", e
            )

    threading.Thread(target=_restart, daemon=True, name="kiterouter-restart").start()


def apply_update() -> Dict[str, Any]:
    """Fetch, fast-forward and sync dependencies — without reloading anything.

    Never raises for an ordinary failure so the caller can report it verbatim.
    """
    before = status(fetch=True, force_fetch=True)
    if not before["available"]:
        return {"status": "error", "message": "Not a git checkout; cannot self-update."}
    if not before["upstream"]:
        return {"status": "error", "message": "No upstream branch to pull from."}
    if before["ahead"] > 0:
        return {
            "status": "error",
            "message": (
                f"Local branch is {before['ahead']} commit(s) ahead of "
                f"{before['upstream']}; refusing to pull."
            ),
        }
    if before["behind"] == 0:
        return {"status": "up_to_date", "message": "Already up to date.", "before": before}

    try:
        pull = _git("pull", "--ff-only", "origin", before["upstream"].split("/")[-1])
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "git pull timed out."}

    if pull.returncode != 0:
        detail = (pull.stderr or pull.stdout).strip()[:400]
        return {
            "status": "error",
            "message": f"git pull failed: {detail}",
            "dirty": before["dirty"],
        }

    synced = False
    uv = shutil.which("uv")
    if uv:
        try:
            sync = subprocess.run(
                [uv, "sync"],
                cwd=str(REPO_DIR),
                capture_output=True,
                text=True,
                timeout=SYNC_TIMEOUT_SECONDS,
            )
            synced = sync.returncode == 0
        except Exception as e:
            logger.warning("uv sync failed: %s", e)

    after = status(fetch=False)
    ok, detail = validate_import()
    if not ok:
        # The code on disk is now suspect; say so instead of restarting into it.
        return {
            "status": "error",
            "message": (
                "Pulled the update, but the new code failed to import so the "
                f"gateway was not restarted: {detail}"
            ),
            "after": after,
        }

    return {
        "status": "updated",
        "message": f"Updated from {before['local_sha']} to {after['local_sha']}"
        + (" and synced dependencies" if synced else ""),
        "dependencies_synced": synced,
        "before": before,
        "after": after,
        "restart_scheduled": False,
    }


def run_update(restart: bool = True) -> Dict[str, Any]:
    """Apply an update and, when asked, reload this process in place.

    The reload replaces *this* process, so it is only correct when called from
    inside the server. The CLI delegates to the running server for that reason.
    """
    result = apply_update()
    if restart and result.get("status") == "updated":
        schedule_restart()
        result["restart_scheduled"] = True
        result["message"] += "; reloading now"
    return result
