"""Runs the dashboard's JavaScript behaviour checks under node.

The dashboard is vanilla JS with no bundler or test runner, so its logic (rail
rendering, the collapse state machine, topology layout and zoom) is exercised by
running the inline script against a DOM stub in tests/js/dashboard_ui_check.js.

Skipped when node is unavailable rather than failing the suite.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "js" / "dashboard_ui_check.js"
DASHBOARD = Path(__file__).resolve().parents[1] / "src" / "kiterouter" / "static" / "dashboard.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_dashboard_javascript_behaviour():
    result = subprocess.run(
        ["node", str(SCRIPT), str(DASHBOARD)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, "dashboard JS checks failed:\n" + output
    assert "ALL CHECKS PASSED" in output
