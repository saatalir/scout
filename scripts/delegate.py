"""Delegate a well-specified coding task to Strata: it writes code, it never runs anything.

    python scripts/delegate.py start <name> [--rounds 4]   # spec: docs/delegation/<name>.md
    python scripts/delegate.py check <name>                # rerun the task's tests (sandboxed), list the files
    python scripts/delegate.py merge <name>                # copy the reviewed files into this checkout (uncommitted)
    python scripts/delegate.py drop  <name>                # remove the scratch copy

Why this shape (docs/ROADMAP.md §2): an earlier version ran opencode with --auto. Its permission
policy did not hold reliably: in one run it wrote outside its clone and ran `git stash` in the main
checkout. So the model gets no tools at all:

1. The harness sends Strata (role "planner") the spec, the files it may change (current content) and
   the context files the spec lists. Strata replies with complete files in a fixed block format.
2. The harness writes only files the spec allows (`files:` globs) into a scratch copy of the tracked
   files (../online-scout-delegate/<name>), never into this checkout.
3. It runs the spec's tests there under macOS sandbox-exec: no network except localhost, no writes
   outside the copy and temp folders. Failures go back to Strata for another round (`rounds`, default 4).
4. A human/Claude reviews the files, runs the whole suite, and merges. Nothing lands unreviewed.

Spec front matter:
    files:   [globs Strata may create or change]
    context: [extra files to show it, read-only]          (optional)
    tests:   [pytest paths to run in the sandbox]
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WT_ROOT = ROOT.parent / "online-scout-delegate"
SPECS = ROOT / "docs" / "delegation"
PYTHON = ROOT / ".venv" / "bin" / "python"
FILE_BLOCK = re.compile(r"^=== FILE: (?P<path>[^\n=]+?) ===\n(?P<body>.*?)\n=== END FILE ===", re.M | re.S)

SYSTEM = """You write code for the Online Scout repository (Python 3.12). You cannot run anything: the harness
runs the tests and sends you any failures. Follow the repository's conventions and AGENTS.md (included).
Reply with the COMPLETE content of every file you create or change, each in exactly this format:

=== FILE: path/relative/to/repo.py ===
<entire file content, no markdown fences>
=== END FILE ===

Only write files the spec allows. Do not weaken tests to make them pass. After the files, add one short
paragraph saying what you did."""


def sandbox_profile(copy: Path) -> str:
    real = copy.resolve()
    return ("(version 1)(allow default)"
            "(deny network-outbound)(allow network-outbound (remote ip \"localhost:*\"))"
            "(allow network-outbound (remote unix-socket))"
            f"(deny file-write* (require-not (require-any (subpath \"{real}\") (subpath \"/private/var/folders\")"
            " (subpath \"/private/tmp\") (subpath \"/dev\"))))")


def load_spec(name: str) -> tuple[dict, str]:
    path = SPECS / f"{name}.md"
    if not path.exists():
        raise SystemExit(f"no spec at {path.relative_to(ROOT)}")
    text = path.read_text()
    meta: dict = {}
    if text.startswith("---\n"):
        head, text = text[4:].split("\n---\n", 1)
        meta = yaml.safe_load(head) or {}
    for key in ("files", "tests"):
        if not meta.get(key):
            raise SystemExit(f"the spec needs front matter `{key}:`")
    return meta, text.strip()


def copy_dir(name: str) -> Path:
    return WT_ROOT / name


def allowed(meta: dict, path: str) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in meta["files"])


def make_copy(name: str) -> Path:
    dest = copy_dir(name)
    if dest.exists():
        raise SystemExit(f"{dest} exists: `drop {name}` first")
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    for rel in filter(None, files.split("\0")):
        src = ROOT / rel
        if src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    (dest / ".delegate").mkdir()
    return dest


def run_tests(copy: Path, tests: list[str]) -> tuple[bool, str]:
    cmd = ["sandbox-exec", "-p", sandbox_profile(copy), str(PYTHON), "-m", "pytest", "-q", "-p", "no:cacheprovider",
           *tests]
    env = {"PATH": "/usr/bin:/bin", "HOME": str(copy), "PYTHONPATH": str(copy), "PYTHONDONTWRITEBYTECODE": "1",
           # browser tests: the installed Chromium, read-only (HOME is the copy, so Playwright would not find it)
           "PLAYWRIGHT_BROWSERS_PATH": str(Path.home() / "Library" / "Caches" / "ms-playwright"),
           "SCOUT_IN_SANDBOX": "1"}                   # local Chromium runs with --no-sandbox inside this one
    try:
        r = subprocess.run(cmd, cwd=copy, capture_output=True, text=True, env=env, timeout=600)
    except subprocess.TimeoutExpired:
        return False, "the tests did not finish within 10 minutes"
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0, out


def prompt_for(meta: dict, body: str, copy: Path) -> str:
    parts = [f"THE SPEC:\n{body}\n", f"Files you may create or change: {', '.join(meta['files'])}",
             f"Tests that must pass: {' '.join(meta['tests'])}\n"]
    shown = ["AGENTS.md", *meta.get("context", [])]
    for rel in shown:
        p = copy / rel
        if p.is_file():
            parts.append(f"--- context file (read-only): {rel} ---\n{p.read_text()}")
    for p in sorted(copy.rglob("*")):
        rel = str(p.relative_to(copy))
        if p.is_file() and allowed(meta, rel) and rel not in shown:
            parts.append(f"--- current content of {rel} (you may change it) ---\n{p.read_text()}")
    return "\n\n".join(parts)


def apply_files(meta: dict, copy: Path, text: str) -> tuple[list[str], list[str]]:
    written, refused = [], []
    for m in FILE_BLOCK.finditer(text):
        rel = m.group("path").strip().lstrip("./")
        body = m.group("body")
        body = re.sub(r"^```[a-z]*\n|\n```$", "", body)          # tolerate fences despite the instructions
        if ".." in Path(rel).parts or not allowed(meta, rel):
            refused.append(rel)
            continue
        (copy / rel).parent.mkdir(parents=True, exist_ok=True)
        (copy / rel).write_text(body.rstrip("\n") + "\n")
        written.append(rel)
    return written, refused


def start(name: str, rounds: int) -> int:
    from scout.config import load_settings
    from scout.llm import LLM

    meta, body = load_spec(name)
    copy = make_copy(name)
    llm = LLM(dataclasses.replace(load_settings(), request_timeout=1800))     # long files take minutes to write
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt_for(meta, body, copy)}]
    result = {"name": name, "rounds": 0, "passed": False, "files": [], "refused": []}
    t0 = time.time()
    for n in range(1, rounds + 1):
        print(f"round {n}: asking Strata…", flush=True)
        r = llm.chat("planner", messages, max_tokens=24000, temperature=0.0)
        text = r.text
        (copy / ".delegate" / f"round-{n}.txt").write_text(text)
        messages.append({"role": "assistant", "content": text})
        written, refused = apply_files(meta, copy, text)
        result["files"] = sorted(set(result["files"]) | set(written))
        result["refused"] += refused
        if not written:
            messages.append({"role": "user", "content": "No files found in your reply. Reply with complete files in "
                                                        "the === FILE: path === / === END FILE === format."})
            continue
        ok, out = run_tests(copy, meta["tests"])
        print(f"round {n}: wrote {written}{' refused ' + str(refused) if refused else ''}; "
              f"tests {'PASS' if ok else 'FAIL'}: {out.splitlines()[-1] if out else ''}", flush=True)
        result["rounds"] = n
        if ok:
            result["passed"] = True
            break
        messages.append({"role": "user", "content": "The tests failed. Output (end):\n" + out[-6000:]
                         + "\n\nFix the code (not the requirements) and reply with the complete corrected files."})
    result["minutes"] = round((time.time() - t0) / 60, 1)
    (copy / ".delegate" / "result.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result))
    return 0 if result["passed"] else 1


def check(name: str) -> int:
    meta, _ = load_spec(name)
    copy = copy_dir(name)
    res = json.loads((copy / ".delegate" / "result.json").read_text())
    ok, out = run_tests(copy, meta["tests"])
    print(f"files: {res['files']}\nrefused (outside the spec): {res['refused'] or 'none'}")
    print(f"rounds: {res['rounds']}, {res.get('minutes')} min")
    print(f"tests (sandboxed): {'PASS' if ok else 'FAIL'} — {out.splitlines()[-1] if out else ''}")
    for rel in res["files"]:
        a, b = ROOT / rel, copy / rel
        status = "new" if not a.exists() else ("unchanged" if a.read_text() == b.read_text() else "changed")
        print(f"  {status:<9} {rel} ({len(b.read_text().splitlines())} lines)")
    return 0 if ok else 1


def merge(name: str) -> int:
    if check(name):
        raise SystemExit("tests fail in the copy: not merging")
    copy = copy_dir(name)
    meta, _ = load_spec(name)
    res = json.loads((copy / ".delegate" / "result.json").read_text())
    for rel in res["files"]:
        if allowed(meta, rel):
            (ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(copy / rel, ROOT / rel)
    print(f"copied {res['files']} into {ROOT} (uncommitted): review, run the whole suite, then commit")
    return 0


def drop(name: str) -> int:
    shutil.rmtree(copy_dir(name), ignore_errors=True)
    print(f"dropped {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["start", "check", "merge", "drop"])
    ap.add_argument("name")
    ap.add_argument("--rounds", type=int, default=4)
    a = ap.parse_args(argv)
    return {"start": lambda: start(a.name, a.rounds), "check": lambda: check(a.name),
            "merge": lambda: merge(a.name), "drop": lambda: drop(a.name)}[a.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
