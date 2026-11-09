"""End-to-end plumbing test: every suite runs against the mock server without crashing."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mock_url():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mock_server.py"), "--port", str(port)])
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait(5)


def _env(mock_url):
    env = dict(os.environ, STRATA_BASE_URL=f"{mock_url}/v1", STRATA_MODEL="auto",
               SCOUT_ROUTER="direct", SCOUT_REQUEST_TIMEOUT="30")
    env["PYTHONPATH"] = str(ROOT)
    return env


def test_check_connection(mock_url):
    r = subprocess.run([sys.executable, "scripts/check_connection.py"], cwd=ROOT,
                       env=_env(mock_url), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "All checks passed" in r.stdout


def test_all_suites_run(mock_url, tmp_path):
    r = subprocess.run([sys.executable, "-m", "evals.run", "--out", str(tmp_path)], cwd=ROOT,
                       env=_env(mock_url), capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    run_dir = next(tmp_path.iterdir())
    rows = [json.loads(l) for l in (run_dir / "results.jsonl").read_text().splitlines()]
    suites = {row["suite"] for row in rows}
    assert suites == {"tool_calling", "extraction", "agent_loop", "vision", "speed"}
    # Harness errors (exceptions) only where the dumb mock gives unparseable answers, never crashes.
    assert all(row["suite"] != "speed" or row["passed"] for row in rows)
    summary = (run_dir / "summary.md").read_text()
    assert "## Gate" in summary and "| planner |" in summary
