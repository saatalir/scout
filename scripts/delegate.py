"""Delegate a well-specified coding task to Strata (via opencode) in an isolated git clone.

    python scripts/delegate.py start <name> [--minutes 45]   # spec: docs/delegation/<name>.md
    python scripts/delegate.py check <name>                  # rerun the tests, show the diff, enforce allowed files
    python scripts/delegate.py merge <name>                  # apply the reviewed change to this checkout (uncommitted)
    python scripts/delegate.py drop  <name>                  # remove the clone

How it is contained (docs/ROADMAP.md §2):
- The work happens in a standalone clone, ../online-scout-delegate/<name> (branch delegate/<name>,
  from HEAD). Not a git worktree: opencode resolves a worktree to the main repository and then
  ignores the worktree's policy. Commit what the task depends on before starting.
- opencode runs with --standalone (a private server, not the shared background service), and a
  preflight confirms it loaded the clone's opencode.json; otherwise nothing runs.
- The main checkout's state is recorded before and after the run; any change is an ESCAPE and
  blocks merge.
- An opencode.json there allows editing and reading inside the clone and running
  ./run-tests; it denies every other shell command, web access, sub-tasks and any
  path outside the clone. Explicit denies hold even with --auto.
- The spec's front matter lists the files the task may touch (`files:`, globs); `check`
  fails if anything else changed. Nothing is merged without `check` passing and review.
- .env is not in the clone; only STRATA_API_KEY is passed to opencode itself.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
WT_ROOT = ROOT.parent / "online-scout-delegate"
SPECS = ROOT / "docs" / "delegation"
OPENCODE = shutil.which("opencode") or str(Path.home() / ".opencode/bin/opencode")
PYTHON = ROOT / ".venv" / "bin" / "python"
LOCAL_FILES = ("opencode.json", "run-tests", ".delegate-log.jsonl", ".delegate-result.json")

POLICY = {
    "$schema": "https://opencode.ai/config.json",
    "permission": {
        "read": "allow", "edit": "allow", "glob": "allow", "grep": "allow", "list": "allow",
        "lsp": "allow", "todowrite": "allow",
        "external_directory": "deny", "webfetch": "deny", "websearch": "deny",
        "task": "deny", "question": "deny", "skill": "deny",
        "bash": {"*": "deny", "./run-tests*": "allow", "ls*": "allow",
                 "git status*": "allow", "git diff*": "allow"},
    },
}

PREAMBLE = """You are implementing one well-specified change in the Online Scout repository (Python 3.12).
Read AGENTS.md first: its invariants apply. Work only on the files the spec lists.
Run the tests with ./run-tests (it accepts pytest arguments, e.g. ./run-tests tests/test_x.py -q).
Other shell commands and web access are not available to you; do not try to work around that.
Iterate until ./run-tests passes completely. Do not weaken or delete existing tests to make them pass.
When done, reply with a short summary: files changed, what they do, final test result.

THE SPEC:
"""


def sh(*cmd: str, cwd: Path = ROOT, check: bool = True) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"{' '.join(cmd)} failed:\n{r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def load_spec(name: str) -> tuple[dict, str]:
    path = SPECS / f"{name}.md"
    if not path.exists():
        raise SystemExit(f"no spec at {path.relative_to(ROOT)}")
    text = path.read_text()
    meta: dict = {}
    if text.startswith("---\n"):
        head, text = text[4:].split("\n---\n", 1)
        meta = yaml.safe_load(head) or {}
    if not meta.get("files"):
        raise SystemExit("the spec needs front matter with `files:` (globs the task may change)")
    return meta, text.strip()


def wt(name: str) -> Path:
    return WT_ROOT / name


def start(name: str, minutes: int) -> int:
    meta, body = load_spec(name)
    if sh("git", "status", "--porcelain", "--", f"docs/delegation/{name}.md").strip():
        raise SystemExit(f"commit docs/delegation/{name}.md first: the clone is made from HEAD")
    path = wt(name)
    if path.exists():
        raise SystemExit(f"{path} exists: `drop {name}` first, or `check {name}`")
    WT_ROOT.mkdir(exist_ok=True)
    sh("git", "clone", "-q", "--no-hardlinks", str(ROOT), str(path))
    sh("git", "checkout", "-q", "-b", f"delegate/{name}", cwd=path)
    (path / ".git" / "info" / "exclude").write_text("\n".join(LOCAL_FILES) + "\n")

    (path / "opencode.json").write_text(json.dumps(POLICY, indent=1))
    runner = path / "run-tests"
    runner.write_text(f'#!/bin/sh\ncd "$(dirname "$0")" && PYTHONPATH="$PWD" exec "{PYTHON}" -m pytest -q -p no:cacheprovider "$@"\n')
    runner.chmod(0o755)

    key = os.environ.get("STRATA_API_KEY") or dotenv_values(ROOT / ".env").get("STRATA_API_KEY") or ""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SCOUT_", "LITELLM_"))}
    env["STRATA_API_KEY"] = key
    message = PREAMBLE + body + f"\n\nFiles you may change: {', '.join(meta['files'])}\n"
    preflight(path, env)
    main_before = main_state()
    log = path / ".delegate-log.jsonl"
    t0 = time.time()
    print(f"delegating {name} to Strata via opencode in {path} (limit {minutes} min)…", flush=True)
    with open(log, "w") as out:
        proc = subprocess.Popen([OPENCODE, "run", "--standalone", "--auto", "--format", "json",
                                 "--title", f"delegate {name}", message],
                                cwd=path, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=minutes * 60)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = "timeout"
    escaped = main_state() != main_before
    result = {"name": name, "exit": code, "minutes": round((time.time() - t0) / 60, 1), "escaped": escaped}
    (path / ".delegate-result.json").write_text(json.dumps(result))
    print(json.dumps(result))
    return check(name)


def main_state() -> str:
    """Status plus contents of every changed file in the main checkout, to detect writes from outside."""
    status = sh("git", "status", "--porcelain", "--untracked-files=all")
    digest = [status]
    for line in status.splitlines():
        f = ROOT / line[3:].split(" -> ")[-1]
        if f.is_file():
            digest.append(f"{f}:{f.stat().st_mtime_ns}:{f.stat().st_size}")
    return "\n".join(digest)


def preflight(path: Path, env: dict) -> None:
    """opencode must report the clone's own opencode.json among its config sources."""
    r = subprocess.run([OPENCODE, "--standalone", "debug", "config"], cwd=path, env=env, capture_output=True, text=True)
    if str((path / "opencode.json").resolve()) not in r.stdout and str(path / "opencode.json") not in r.stdout:
        raise SystemExit("preflight failed: opencode did not load the clone's opencode.json policy; not running")


def changed_files(path: Path) -> list[str]:
    out = sh("git", "status", "--porcelain", "--untracked-files=all", cwd=path)
    files = [line[3:].split(" -> ")[-1] for line in out.splitlines() if line.strip()]
    return [f for f in files if f not in LOCAL_FILES]


def check(name: str) -> int:
    meta, _ = load_spec(name)
    path = wt(name)
    if not path.exists():
        raise SystemExit(f"no clone for {name}")
    files = changed_files(path)
    outside = [f for f in files if not any(fnmatch.fnmatch(f, g) for g in meta["files"])]
    tests = subprocess.run([str(path / "run-tests")], cwd=path, capture_output=True, text=True)
    last = (tests.stdout.strip().splitlines() or ["(no output)"])[-1]
    print(f"changed: {files or 'nothing'}")
    print(f"outside the allowed files: {outside or 'none'}")
    print(f"tests: {'PASS' if tests.returncode == 0 else 'FAIL'} — {last}")
    for denied in _denied_attempts(path):
        print(f"denied by policy: {denied}")
    result = json.loads((path / ".delegate-result.json").read_text()) if (path / ".delegate-result.json").exists() else {}
    if result.get("escaped"):
        print("ESCAPE: the main checkout changed during the run. Inspect `git status` there; nothing is merged.")
    ok = bool(files) and not outside and tests.returncode == 0 and not result.get("escaped")
    print("CHECK", "OK" if ok else "FAILED")
    return 0 if ok else 1


def _denied_attempts(path: Path) -> list[str]:
    log = path / ".delegate-log.jsonl"
    if not log.exists():
        return []
    out = []
    for line in log.read_text().splitlines():
        if "denied" in line.lower() or "rejected" in line.lower():
            out.append(line[:200])
    return out[:10]


def merge(name: str) -> int:
    if check(name):
        raise SystemExit("check failed: not merging")
    path = wt(name)
    sh("git", "add", "-A", "--", *changed_files(path), cwd=path)
    patch = sh("git", "diff", "--cached", "--binary", "HEAD", cwd=path)
    p = subprocess.run(["git", "apply", "--3way", "-"], cwd=ROOT, input=patch, text=True, capture_output=True)
    if p.returncode:
        raise SystemExit(f"git apply failed:\n{p.stderr}")
    print(f"applied {name} to {ROOT} (uncommitted): review with `git diff`, then commit")
    return 0


def drop(name: str) -> int:
    path = wt(name)
    if path.exists():
        shutil.rmtree(path)
    print(f"dropped {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["start", "check", "merge", "drop"])
    ap.add_argument("name")
    ap.add_argument("--minutes", type=int, default=45)
    a = ap.parse_args(argv)
    return {"start": lambda: start(a.name, a.minutes), "check": lambda: check(a.name),
            "merge": lambda: merge(a.name), "drop": lambda: drop(a.name)}[a.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
